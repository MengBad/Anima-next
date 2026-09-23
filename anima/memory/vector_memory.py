"""向量记忆 —— 长期语义记忆层。

设计（§14）：
- 使用 aiosqlite + sqlite-vec 做本地向量库
- Embedding 后端：远程 OpenAI 兼容 /embeddings（默认 SiliconFlow BAAI/bge-m3, 1024 维）
- Reranker 后端：远程 /v1/rerank（默认 SiliconFlow BAAI/bge-reranker-v2-m3）
- 两阶段检索：向量召回候选 → reranker 重排取 top_k
- 带 hash→向量缓存，避免重复计费
- Embedding/Rerank 失败时降级为 BM25 关键词检索
"""

from __future__ import annotations
import array
import asyncio
import base64
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

import aiohttp
import aiosqlite

try:
    import sqlite_vec
    _SQLITE_VEC_AVAILABLE = True
except ImportError:
    sqlite_vec = None
    _SQLITE_VEC_AVAILABLE = False

logger = logging.getLogger("anima.memory.vector")


# ---------------------------------------------------------------------------
# Embedding 后端
# ---------------------------------------------------------------------------

class Embedder:
    """远程 OpenAI 兼容 embedding 客户端（默认 SiliconFlow BAAI/bge-m3）。

    bge-m3 原生输出 1024 维，调用时不传 dimensions 参数。
    """

    _MAX_CACHE_ENTRIES = 8000  # LRU 上限，超限淘汰最旧 20%

    #: 缓存落盘格式（2026-09-23 起为 v2）。
    #:   v1 = ``{hash: [float, ...]}``  —— 裸 float 数组
    #:   v2 = ``{hash: base64(float32 bytes)}``
    #:
    #: 🔴 为什么必须换格式：生产实测 60.6MB／2788 条 × 1024 维，`json.dumps`
    #: 单次 **1.48–1.52s**；而 CPython 的 json **C 加速编码器全程不释放 GIL**
    #: （C 里没有 eval-loop 边界 ⇒ 收不到 gil_drop_request）⇒ 把它丢进
    #: `asyncio.to_thread` **救不了**事件循环（实测：`to_thread(json.dumps)` 期间
    #: loop 推进 **0** 次 / 1.293s；同样时长换成 `time.sleep` 则推进 37 次）。
    #: 唯一有效的手段是**把要序列化的字节数压下来**：
    #:   v2 体积 15.3MB（3.8x↓）⇒ `json.dumps` 0.055s（**23x↓**）。
    #: 另外 float32 也正是向量库实际检索的精度（sqlite-vec `float[1024]`），
    #: 故相对检索语义没有额外损失（往返 |1-cos| ≈ 6e-16）。
    #: 读路径同时兼容 v1（见 `_decode_vec`），保证升级不丢已有缓存。
    _CACHE_FORMAT = 2

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        model: str = "BAAI/bge-m3",
        timeout: float = 30.0,
        data_dir: str = "data",
        cache: bool = True,
    ):
        self.endpoint = endpoint.rstrip("/") + "/embeddings"
        self.api_key = api_key
        self.model = model
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.cache_enabled = cache
        self._cache: dict[str, list[float]] = {}      # 真源：hash → 向量（检索用）
        self._cache_b64: dict[str, str] = {}          # 落盘镜像：hash → base64(float32)
        self._cache_path = Path(data_dir) / "embedding_cache.json"
        self._session: Optional[aiohttp.ClientSession] = None
        # 落盘合并用（2026-09-23，详见 `_save_cache`）
        self._cache_version = 0     # 插入计数：每次缓存未命中 +1
        self._saved_version = 0     # 已成功落盘的版本号
        self._save_lock: Optional[asyncio.Lock] = None   # 懒建（构造时可能无 loop）
        self._save_loop: Optional[asyncio.AbstractEventLoop] = None
        if self.cache_enabled:
            self._load_cache()

    # ------------------------------------------------------------------
    # v2 缓存编解码（float32 打包 + base64）
    # ------------------------------------------------------------------

    @staticmethod
    def _encode_vec(vec: list[float]) -> str:
        """向量 → base64(float32 bytes)。1024 维 ⇒ 5464 字符（裸 float 约 20K）。"""
        return base64.b64encode(array.array("f", vec).tobytes()).decode("ascii")

    @staticmethod
    def _decode_vec(raw: Any) -> Optional[list[float]]:
        """v2（str）或 v1（list）→ list[float]；无法解析返回 None。"""
        if isinstance(raw, str):
            try:
                buf = base64.b64decode(raw, validate=True)
                arr = array.array("f")
                arr.frombytes(buf)
                return list(arr)
            except Exception:
                return None
        if isinstance(raw, list):
            return raw  # v1 向后兼容
        return None

    def _load_cache(self) -> None:
        if self._cache_path.exists():
            try:
                payload = json.loads(self._cache_path.read_text("utf-8"))
                cache: dict[str, list[float]] = {}
                dropped = 0
                legacy = 0
                for key, raw in (payload or {}).items():
                    if isinstance(raw, list):
                        legacy += 1
                    vec = self._decode_vec(raw)
                    if vec is None:
                        dropped += 1
                        continue
                    cache[key] = vec
                self._cache = cache
                if dropped:
                    logger.warning("embedding 缓存丢弃 %d 条不可解析条目", dropped)
                if legacy:
                    logger.info(
                        "embedding 缓存 v1→v%d 迁移：%d 条裸 float 条目将按 base64 重写",
                        self._CACHE_FORMAT, legacy,
                    )
            except Exception:
                logger.exception("embedding 缓存加载失败，按空缓存启动")
                self._cache = {}
            # 建好落盘镜像；v1 条目在此升级为 v2（首次落盘即完成迁移）。
            try:
                self._cache_b64 = {k: self._encode_vec(v) for k, v in self._cache.items()}
            except Exception:
                logger.exception("embedding 缓存镜像构建失败（将按需惰性编码）")
                self._cache_b64 = {}
        # 启动时截断到上限（淘汰最旧 20%）
        self._evict_if_over_limit()

    def _evict_if_over_limit(self) -> None:
        """超限时按插入顺序淘汰最旧 20%（dict 保序）。"""
        limit = self._MAX_CACHE_ENTRIES
        if len(self._cache) <= limit:
            return
        evict_count = max(1, len(self._cache) - limit + limit // 5)  # 淘汰到 80% 水位
        keys = list(self._cache.keys())[:evict_count]
        for k in keys:
            del self._cache[k]
            self._cache_b64.pop(k, None)
        logger.info("embedding 缓存淘汰 %d 条（上限 %d）", evict_count, limit)

    def _write_cache_file(self, snapshot: dict[str, str]) -> bool:
        """纯 CPU/IO：把**快照**落盘。成功返回 True。

        🔴 本方法**可在工作线程执行** ⇒ 不得触碰 `self._cache`：worker 遍历
        字典的同时 loop 线程可能正在插入新键，会撞
        `RuntimeError: dictionary changed size during iteration`。
        快照由调用方（`_save_cache`，在 loop 线程上）拍好后再传进来。

        🔴 也**不得在此做编解码**：`array`/`base64` 同样是 C 且不放 GIL，
        2788 条 × 32µs ≈ 90ms 会原样搬进 worker 并冻结 loop。镜像在插入时
        就编好（`_cache_b64`），这里只剩 `json.dumps` 一个动作。
        """
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(json.dumps(snapshot, ensure_ascii=False), "utf-8")
            return True
        except Exception:
            logger.exception("embedding 缓存保存失败")
            return False

    async def _save_cache(self) -> None:
        """整文件重写 —— **必须 await**（2026-09-23 由同步改为协程）。

        缺陷（生产实测）：`embedding_cache.json` = 60.6MB／2788 条 × 1024 维，
        `json.dumps` 单次 1.48–1.52s。原实现由 `embed()` 在**事件循环线程上同步**
        调用本方法 ⇒ 每次缓存未命中都把整个 loop 冻住约 1.5s，期间所有并发
        会话一起停转（观察面：`事件循环延迟` 的 2–3.4s 族）。且 LRU 上限
        8000 条 ⇒ 满额约 174MB ⇒ `dumps` 外推约 4.3s，属会恶化的缺陷。

        现机制（三层，缺一不可）：
        1. **格式 v2**：落盘镜像 `_cache_b64` 在插入时编好 ⇒ 序列化对象从
           「2788 × 1024 个 float」变成「2788 个短字符串」，字节数 3.8x↓
           ⇒ `json.dumps` 23x↓。这一步才是关键：C 编码器不放 GIL，
           光靠 `to_thread` 只是换了个线程独占 GIL（实测 loop 仍推进 0 次）。
        2. **离线程**：`json.dumps` + `write_text` 交 `asyncio.to_thread`。
        3. **同批合并**：`_cache_version` 记录插入次数，同批并发未命中只落盘
           1–2 次，而不是 N 次。
        loop 线程只做 `_evict_if_over_limit()` ＋ 快照（实测 31µs）。

        缓存只是加速层 ⇒ 任何失败都**不得冒泡**：`embed()` 的重试 `try` 会把
        异常误判成「Embedding 请求失败」并白跑一次 HTTP。
        """
        try:
            loop = asyncio.get_running_loop()
            if self._save_lock is None or self._save_loop is not loop:
                # asyncio.Lock 绑定首次使用的 loop，跨 loop 复用会 RuntimeError；
                # Embedder 又可能在无运行 loop 时构造（工厂/测试），故懒建 + 重建。
                self._save_lock = asyncio.Lock()
                self._save_loop = loop
            async with self._save_lock:
                version = self._cache_version
                if version == self._saved_version:
                    return  # 已被同批的前一次落盘覆盖
                self._evict_if_over_limit()
                snapshot = self._build_mirror()
                if await asyncio.to_thread(self._write_cache_file, snapshot):
                    self._saved_version = version
        except Exception:
            logger.exception("embedding 缓存保存失败（已隔离）")

    def _build_mirror(self) -> dict[str, str]:
        """构造落盘快照（loop 线程，O(n) 单趟）。

        镜像缺失的条目当场补编（自愈：真源被外部直接写过也不会落盘残缺），
        并返回**新 dict** ⇒ worker 线程拿到的是与 `self._cache` 脱钩的对象，
        不会与 loop 线程的插入互相干扰。
        """
        mirror: dict[str, str] = {}
        for key, vec in self._cache.items():
            packed = self._cache_b64.get(key)
            if packed is None:
                packed = self._encode_vec(vec)
                self._cache_b64[key] = packed
            mirror[key] = packed
        return mirror

    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]

    async def embed(self, text: str) -> Optional[list[float]]:
        """获取文本的 embedding 向量；失败返回 None。"""
        text = text.strip()
        if not text:
            return None

        key = self._hash(text)
        if self.cache_enabled and key in self._cache:
            return self._cache[key]

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)

        last_err: Optional[Exception] = None
        for attempt in range(2):  # 瞬时网络抖动重试一次
            try:
                async with self._session.post(
                    self.endpoint,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={"model": self.model, "input": text},
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error("Embedding API 错误 %d: %.200s", resp.status, body)
                        return None
                    data = await resp.json()
                    vec = data["data"][0]["embedding"]
                    if self.cache_enabled:
                        self._cache[key] = vec
                        # 落盘镜像当场编好（32µs/条）——把编解码从「整文件落盘时」
                        # 挪到「单条插入时」，见 _write_cache_file 的说明。
                        self._cache_b64[key] = self._encode_vec(vec)
                        self._cache_version += 1
                        await self._save_cache()
                    return vec
            except Exception as ex:
                last_err = ex
                if attempt == 0:
                    logger.warning("Embedding 请求失败，重试一次: %s", ex)
                    await asyncio.sleep(0.5)
        logger.exception("Embedding 请求失败（已重试）")
        return None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None


# ---------------------------------------------------------------------------
# Reranker 后端
# ---------------------------------------------------------------------------

class Reranker:
    """远程 rerank 客户端（默认 SiliconFlow BAAI/bge-reranker-v2-m3）。

    两阶段检索的第二步：对向量召回的候选文本按查询相关性重排。
    """

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        model: str = "BAAI/bge-reranker-v2-m3",
        top_n: int = 5,
        timeout: float = 30.0,
    ):
        self.endpoint = endpoint
        self.api_key = api_key
        self.model = model
        self.top_n = top_n
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None

    async def rerank(self, query: str, documents: list[str]) -> Optional[list[tuple[int, float]]]:
        """对 (query, documents) 打分，返回 [(doc_index, relevance_score), ...]（按分数降序）。

        失败时返回 None（调用方应降级为向量相似度排序）。
        """
        if not documents:
            return []
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        try:
            async with self._session.post(
                self.endpoint,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "query": query,
                    "documents": documents,
                    "top_n": min(self.top_n, len(documents)),
                },
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error("Rerank API 错误 %d: %.200s", resp.status, body)
                    return None
                data = await resp.json()
                results = data.get("results") or []
                return [(int(r["index"]), float(r.get("relevance_score", 0.0))) for r in results]
        except Exception:
            logger.exception("Rerank 请求失败")
            return None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None


# ---------------------------------------------------------------------------
# BM25 关键词降级
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> set[str]:
    """简单分词：中文按字，英文按词，过滤单字/短词。"""
    text = text.lower()
    words = set(re.findall(r"[a-z0-9]{2,}", text))
    chs = re.findall(r"[\u4e00-\u9fff]{2,}", text)
    for ch in chs:
        for i in range(len(ch) - 1):
            words.add(ch[i:i+2])
    return words


def _bm25_score(query: str, text: str) -> float:
    """返回 BM25 简化分数（关键词重叠度）。"""
    q_tokens = _tokenize(query)
    t_tokens = _tokenize(text)
    if not q_tokens:
        return 0.0
    overlap = len(q_tokens & t_tokens)
    return overlap / len(q_tokens)


# ---------------------------------------------------------------------------
# 向量记忆库
# ---------------------------------------------------------------------------

class VectorMemory:
    """长期语义记忆库（两阶段检索：向量召回 + rerank 重排）。"""

    def __init__(
        self,
        db_path: str = "data/anima.db",
        embedding: Optional[Embedder] = None,
        embedding_dim: int = 1024,
        reranker: Optional[Reranker] = None,
    ):
        self.db_path = db_path
        self.embedding = embedding
        self.embedding_dim = embedding_dim
        self.reranker = reranker
        self._ready = False
        # P0-B2：遗忘导出分页轮转偏移——保证超 limit 行后仍能覆盖全体，而非反复评同一批
        self._dump_offset = 0

    async def _init_db(self) -> None:
        if self._ready:
            return
        self._ready = True
        async with aiosqlite.connect(self.db_path) as db:
            # 深审 P1：WAL——并发读写不再互相阻塞/SQLITE_BUSY 丢记忆
            await db.execute("PRAGMA journal_mode=WAL")
            # meta 表始终创建
            await db.execute(
                """CREATE TABLE IF NOT EXISTS memory_meta (
                    rowid INTEGER PRIMARY KEY,
                    text TEXT NOT NULL,
                    meta TEXT,
                    ts REAL,
                    salience REAL,
                    source_uin INTEGER,
                    source_gid INTEGER
                )"""
            )
            await db.commit()

            # 向量扩展仅在需要 embedding 且 sqlite-vec 可用时创建
            if self.embedding and _SQLITE_VEC_AVAILABLE:
                try:
                    await db.enable_load_extension(True)
                    await db.load_extension(sqlite_vec.loadable_path())
                    # 维度不匹配时（如 embedding 模型切换 1536→1024）自动重建向量表，
                    # 避免 "Dimension mismatch ... Expected N but received M" 写入崩溃。
                    cur = await db.execute(
                        "SELECT sql FROM sqlite_master WHERE name='memories'"
                    )
                    row = await cur.fetchone()
                    if row and row[0] and f"float[{self.embedding_dim}]" not in row[0]:
                        # P0-B3：维度不匹配不再静默 DROP+DELETE 清空全部长期记忆。
                        # 改为把旧 meta 整表改名备份，向量表重建；owner 可事后决定恢复或清理。
                        import time as _t
                        suffix = _t.strftime("%Y%m%d%H%M%S")
                        logger.warning(
                            "向量表维度不匹配 (%s)：旧记忆 meta 备份为 memory_meta_dim_backup_%s 后重建向量表",
                            row[0], suffix,
                        )
                        await db.execute("DROP TABLE IF EXISTS memories")
                        await db.execute(
                            f"ALTER TABLE memory_meta RENAME TO memory_meta_dim_backup_{suffix}"
                        )
                        # 以原名建空表，运行时继续可用；旧数据完整保留在备份表中
                        await db.execute(
                            """CREATE TABLE IF NOT EXISTS memory_meta (
                                rowid INTEGER PRIMARY KEY,
                                text TEXT NOT NULL,
                                meta TEXT,
                                ts REAL,
                                salience REAL,
                                source_uin INTEGER,
                                source_gid INTEGER
                            )"""
                        )
                    await db.execute(
                        f"CREATE VIRTUAL TABLE IF NOT EXISTS memories USING vec0(embedding float[{self.embedding_dim}])"
                    )
                    await db.commit()
                except Exception as e:
                    logger.warning("sqlite-vec 加载失败，将使用 BM25 降级: %s", e)

    async def add(
        self,
        text: str,
        meta: Optional[dict] = None,
        salience: float = 0.5,
        source_uin: Optional[int] = None,
        source_gid: Optional[int] = None,
    ) -> bool:
        """添加一条记忆。返回是否成功。"""
        await self._init_db()
        if not text.strip():
            return False

        # P0-B2：精确文本去重——同一全文已存在则跳过（原实现每条消息无脑入库，
        # 长期记忆被重复项稀释且无上限）。近重复仍允许入库（交由遗忘机制衰减）。
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT 1 FROM memory_meta WHERE text = ? LIMIT 1", (text,)
            )
            if await cur.fetchone():
                logger.debug("向量记忆去重跳过（相同全文已存在）")
                return True

        vec = None
        if self.embedding:
            vec = await self.embedding.embed(text)

        if vec is None or not _SQLITE_VEC_AVAILABLE:
            async with aiosqlite.connect(self.db_path) as db:
                # 深审 P1：embed await 窗口内并发消息可能同时通过首个去重，
                # 插入前同一连接复检（窗口从网络级收窄到即时）
                cur = await db.execute(
                    "SELECT 1 FROM memory_meta WHERE text = ? LIMIT 1", (text,)
                )
                if await cur.fetchone():
                    return True
            async with aiosqlite.connect(self.db_path) as db:
                # 深审 P1：embed await 窗口内并发消息可能同时通过首个去重，
                # 插入前同一连接复检（窗口从网络级收窄到即时）
                cur = await db.execute(
                    "SELECT 1 FROM memory_meta WHERE text = ? LIMIT 1", (text,)
                )
                if await cur.fetchone():
                    return True
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT INTO memory_meta(text, meta, ts, salience, source_uin, source_gid) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        text,
                        json.dumps(meta or {}, ensure_ascii=False),
                        time.time(),
                        salience,
                        source_uin,
                        source_gid,
                    ),
                )
                await db.commit()
            return True

        async with aiosqlite.connect(self.db_path) as db:
            # 先写 memory_meta，让 SQLite 自动分配 rowid（避免并发时 vec0 rowid 冲突）
            cur = await db.execute(
                "INSERT INTO memory_meta(text, meta, ts, salience, source_uin, source_gid) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    text,
                    json.dumps(meta or {}, ensure_ascii=False),
                    time.time(),
                    salience,
                    source_uin,
                    source_gid,
                ),
            )
            meta_rowid = cur.lastrowid
            # 再用该 rowid 写入 vec0 虚表
            if _SQLITE_VEC_AVAILABLE:
                await db.enable_load_extension(True)
                await db.load_extension(sqlite_vec.loadable_path())
            await db.execute(
                "INSERT INTO memories(rowid, embedding) VALUES (?, ?)",
                (meta_rowid, json.dumps(vec)),
            )
            await db.commit()
        return True

    async def search(
        self,
        query: str,
        top_k: int = 5,
        threshold: float = 0.75,
        source_uin: Optional[int] = None,
        source_gid: Optional[int] = None,
        scoped: bool = False,
    ) -> list[dict[str, Any]]:
        """检索记忆：向量召回候选 → reranker 重排；失败降级 BM25。"""
        await self._init_db()
        if not query.strip():
            return []

        # 两阶段：向量宽松召回候选 + reranker 重排
        if self.embedding and _SQLITE_VEC_AVAILABLE:
            vec = await self.embedding.embed(query)
            if vec is not None:
                # 宽松召回更多候选，供 reranker 二次精排
                candidates = await self._vector_search(
                    vec, top_k=max(top_k * 4, 20), threshold=0.0,
                    source_uin=source_uin, source_gid=source_gid, scoped=scoped,
                )
                if candidates:
                    if self.reranker:
                        reranked = await self.reranker.rerank(query, [c["text"] for c in candidates])
                        if reranked:
                            ordered: list[dict[str, Any]] = []
                            for idx, score in reranked:
                                if 0 <= idx < len(candidates):
                                    # P1-1：rerank 后仍需按 threshold 过滤低相关记忆，
                                    # 否则会召回与用户提问无关的旧记忆污染上下文。
                                    if threshold and threshold > 0 and score < threshold:
                                        continue
                                    c = candidates[idx]
                                    c["similarity"] = score
                                    ordered.append(c)
                            return ordered[:top_k]
                    # 无 reranker 或 rerank 失败：向量相似度排序，按 threshold 过滤
                    return [c for c in candidates if c["similarity"] >= threshold][:top_k]

        return await self._bm25_search(query, top_k, source_uin, source_gid, scoped)

    async def _vector_search(
        self, vec: list[float], top_k: int, threshold: float,
        source_uin: Optional[int] = None, source_gid: Optional[int] = None,
        scoped: bool = False,
    ) -> list[dict[str, Any]]:
        """向量 KNN 召回（返回最近的 top_k 条）。

        threshold <= 0 时不做相似度过滤（供 rerank 宽松召回使用）。
        vec0 的 KNN 查询不能带 JOIN 且必须带 LIMIT，故分两步：
        先查 vec0 取 rowid+distance，再 IN 查 memory_meta。
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.enable_load_extension(True)
            await db.load_extension(sqlite_vec.loadable_path())
            vec_json = json.dumps(vec)
            cursor = await db.execute(
                "SELECT rowid, distance FROM memories WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
                (vec_json, top_k),
            )
            rows = await cursor.fetchall()
            if not rows:
                return []
            rowids = [r[0] for r in rows]
            dist_map = {r[0]: r[1] for r in rows}
            qmarks = ",".join("?" for _ in rowids)
            sql = f"SELECT rowid, text, meta, ts, salience FROM memory_meta WHERE rowid IN ({qmarks})"
            args: list[Any] = list(rowids)
            if scoped:
                sql += " AND source_uin = ?"
                args.append(source_uin)
                if source_gid is None:
                    sql += " AND source_gid IS NULL"
                else:
                    sql += " AND source_gid = ?"
                    args.append(source_gid)
            cursor = await db.execute(sql, args)
            meta_rows = await cursor.fetchall()
        results = []
        for rowid, text, meta_str, ts, salience in meta_rows:
            distance = dist_map.get(rowid, 2.0)
            similarity = 1 - distance / 2
            if threshold and threshold > 0 and similarity < threshold:
                continue
            results.append({
                "rowid": rowid,
                "text": text,
                "similarity": similarity,
                "meta": json.loads(meta_str or "{}"),
                "ts": ts,
                "salience": salience,
            })
        # 按向量距离升序（IN 查询不保序）
        results.sort(key=lambda x: dist_map.get(x["rowid"], 2.0))
        return results

    async def _bm25_search(self, query: str, top_k: int,
                           source_uin: Optional[int] = None,
                           source_gid: Optional[int] = None,
                           scoped: bool = False) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            sql = "SELECT rowid, text, meta, ts, salience FROM memory_meta"
            args: list[Any] = []
            if scoped:
                sql += " WHERE source_uin = ?"
                args.append(source_uin)
                if source_gid is None:
                    sql += " AND source_gid IS NULL"
                else:
                    sql += " AND source_gid = ?"
                    args.append(source_gid)
            cursor = await db.execute(sql, args)
            rows = await cursor.fetchall()
        scored = []
        for rowid, text, meta_str, ts, salience in rows:
            score = _bm25_score(query, text)
            if score > 0:
                scored.append((score, rowid, text, meta_str, ts, salience))
        scored.sort(key=lambda x: -x[0])
        results = []
        for score, rowid, text, meta_str, ts, salience in scored[:top_k]:
            results.append({
                "rowid": rowid,
                "text": text,
                "similarity": score,
                "meta": json.loads(meta_str or "{}"),
                "ts": ts,
                "salience": salience,
            })
        return results

    async def recent(self, top_k: int = 5) -> list[dict[str, Any]]:
        """按时间最近返回记忆。"""
        await self._init_db()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT rowid, text, meta, ts, salience FROM memory_meta ORDER BY ts DESC LIMIT ?",
                (top_k,),
            )
            rows = await cursor.fetchall()
        return [
            {
                "rowid": rowid,
                "text": text,
                "similarity": 1.0,
                "meta": json.loads(meta_str or "{}"),
                "ts": ts,
                "salience": salience,
            }
            for rowid, text, meta_str, ts, salience in rows
        ]

    async def dump_for_forgetting(self, limit: int = 5000) -> list[dict[str, Any]]:
        """按 rowid 分页导出记忆供遗忘评分。

        P0-B2：原实现固定 LIMIT 无排序无偏移——行数超过 limit 时每次都评估同
        一批最新/最旧行，其余永不进入遗忘流程。改为 rowid 排序 + 实例级偏移
        轮转：每次调用返回下一页；不足一页说明已到末尾，下一轮回绕从头覆盖。
        """
        await self._init_db()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT rowid, text, meta, ts, salience FROM memory_meta "
                "ORDER BY rowid LIMIT ? OFFSET ?",
                (limit, max(0, int(getattr(self, "_dump_offset", 0)))),
            )
            rows = await cursor.fetchall()
        fetched = len(rows)
        if fetched < limit:
            self._dump_offset = 0          # 已到末尾，下轮从头再覆盖
        else:
            self._dump_offset = getattr(self, "_dump_offset", 0) + fetched
        return [
            {
                "rowid": rowid,
                "text": text,
                "ts": ts,
                "salience": salience,
                "meta": json.loads(meta_str or "{}"),
            }
            for rowid, text, meta_str, ts, salience in rows
        ]

    async def delete(self, rowid: int) -> bool:
        """删除指定记忆。"""
        await self._init_db()
        async with aiosqlite.connect(self.db_path) as db:
            if _SQLITE_VEC_AVAILABLE and self.embedding:
                await db.enable_load_extension(True)
                await db.load_extension(sqlite_vec.loadable_path())
                await db.execute("DELETE FROM memories WHERE rowid = ?", (rowid,))
            await db.execute("DELETE FROM memory_meta WHERE rowid = ?", (rowid,))
            await db.commit()
        return True

    async def delete_by_source_uin(self, user_id: int) -> int:
        """按来源 QQ 删除长期记录；用于用户自助遗忘。"""
        await self._init_db()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT rowid FROM memory_meta WHERE source_uin = ?", (int(user_id),))
            rows = await cursor.fetchall()
        deleted = 0
        for (rowid,) in rows:
            await self.delete(int(rowid))
            deleted += 1
        return deleted

    async def export_by_source_uin(self, user_id: int) -> list[dict[str, Any]]:
        """导出来源为该 QQ 的长期记录，不包含其他用户数据。"""
        await self._init_db()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT rowid, text, meta, ts, salience, source_gid FROM memory_meta WHERE source_uin = ?",
                (int(user_id),),
            )
            rows = await cursor.fetchall()
        return [{"rowid": rowid, "text": text, "meta": json.loads(meta or "{}"),
                 "ts": ts, "salience": salience, "source_gid": source_gid}
                for rowid, text, meta, ts, salience, source_gid in rows]

    async def health(self) -> dict:
        """健康检查：返回向量后端与 reranker 状态。

        用于 doctor 自检与运维监控。不抛异常。
        """
        result = {
            "status": "degraded",
            "sqlite_vec": _SQLITE_VEC_AVAILABLE,
            "embedding": self.embedding is not None,
            "rerank": self.reranker is not None,
            "semantic": False,
        }
        if _SQLITE_VEC_AVAILABLE and self.embedding:
            try:
                async with aiosqlite.connect(":memory:") as db:
                    await db.enable_load_extension(True)
                    await db.load_extension(sqlite_vec.loadable_path())
                    await db.execute(
                        f"CREATE VIRTUAL TABLE IF NOT EXISTS _health_check "
                        f"USING vec0(embedding float[{self.embedding_dim}])"
                    )
                result["semantic"] = True
                result["status"] = "ok"
            except Exception as e:
                logger.warning("向量后端健康检查失败（降级为 BM25）: %s", e)
        return result

    async def close(self) -> None:
        """关闭底层资源（Embedder / Reranker 的 HTTP 会话）。"""
        if self.embedding is not None:
            await self.embedding.close()
        if self.reranker is not None:
            await self.reranker.close()


# ---------------------------------------------------------------------------
# 工厂函数
# ---------------------------------------------------------------------------

def create_embedder(config, data_dir: str = "data") -> Optional[Embedder]:
    """根据 vector 配置创建 Embedder（独立 endpoint/key，不依赖 llm 配置）。"""
    vc = config.vector
    key = vc.embedding_api_key.get_secret_value() if vc.embedding_api_key else ""
    if not key:
        return None
    return Embedder(
        endpoint=vc.embedding_endpoint,
        api_key=key,
        model=vc.embedding_model,
        data_dir=data_dir,
        cache=vc.cache,
    )


def create_reranker(config) -> Optional[Reranker]:
    """根据 vector 配置创建 Reranker。"""
    vc = config.vector
    if not vc.rerank_enabled:
        return None
    key = vc.rerank_api_key.get_secret_value() if vc.rerank_api_key else ""
    if not key:
        return None
    return Reranker(
        endpoint=vc.rerank_endpoint,
        api_key=key,
        model=vc.rerank_model,
        top_n=vc.rerank_top_n,
    )


def create_vector_memory(config) -> Optional[VectorMemory]:
    """根据配置创建 VectorMemory（含 embedding + reranker）。"""
    if not config.vector.enabled:
        return None
    embedding = create_embedder(config, config.persistence.data_dir)
    reranker = create_reranker(config)
    return VectorMemory(
        db_path=config.vector.db_path,
        embedding=embedding,
        embedding_dim=config.vector.embedding_dim,
        reranker=reranker,
    )
