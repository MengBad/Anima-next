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
import asyncio
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
        self._cache: dict[str, list[float]] = {}
        self._cache_path = Path(data_dir) / "embedding_cache.json"
        self._session: Optional[aiohttp.ClientSession] = None
        if self.cache_enabled:
            self._load_cache()

    def _load_cache(self) -> None:
        if self._cache_path.exists():
            try:
                self._cache = json.loads(self._cache_path.read_text("utf-8"))
            except Exception:
                self._cache = {}

    def _save_cache(self) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(json.dumps(self._cache, ensure_ascii=False), "utf-8")
        except Exception:
            logger.exception("embedding 缓存保存失败")

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
                        self._save_cache()
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

    async def _init_db(self) -> None:
        if self._ready:
            return
        self._ready = True
        async with aiosqlite.connect(self.db_path) as db:
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
                        logger.warning(
                            "向量表维度不匹配 (%s)，重建 memories 表并清空孤儿 meta",
                            row[0],
                        )
                        await db.execute("DROP TABLE IF EXISTS memories")
                        await db.execute("DELETE FROM memory_meta")
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

        vec = None
        if self.embedding:
            vec = await self.embedding.embed(text)

        if vec is None or not _SQLITE_VEC_AVAILABLE:
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
            if _SQLITE_VEC_AVAILABLE:
                await db.enable_load_extension(True)
                await db.load_extension(sqlite_vec.loadable_path())
            cur = await db.execute(
                "INSERT INTO memories(embedding) VALUES (?)",
                (json.dumps(vec),),
            )
            rowid = cur.lastrowid
            await db.execute(
                "INSERT INTO memory_meta(rowid, text, meta, ts, salience, source_uin, source_gid) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    rowid,
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
        """导出所有记忆用于遗忘评分。"""
        await self._init_db()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT rowid, text, meta, ts, salience FROM memory_meta LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
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
