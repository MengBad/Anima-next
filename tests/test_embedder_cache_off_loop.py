"""Embedder 缓存落盘不得阻塞事件循环（2026-09-23）。

**缺陷**：`data/embedding_cache.json` 生产实测 **60.6MB / 2788 条 × 1024 维**，
`json.dumps` 单次 **1.48–1.52s**。而 `Embedder.embed()` 在**事件循环线程上同步**
调用 `_save_cache()`（`vector_memory.py:131`）⇒ 每一次缓存未命中都把整个 loop
冻住约 1.5s，期间所有并发会话一起停转。

**观察面**：`事件循环延迟` 告警的 **2–3.4s 族**（12:11:45 2.076s /
12:13:47 2.185s / 12:14:50 2.300s），且该窗口 `工具调用=0 受治理=0 ReAct轮次=0`
⇒ 与「受治理工具同步 join」那族（≥5s，已单独修）无关。

🔴 **`asyncio.to_thread` 单独不够**（实测否掉了这个假说）：
`json.dumps` 走 CPython 的 **C 加速编码器，全程不释放 GIL**（C 里没有 eval-loop
边界 ⇒ 收不到 gil_drop_request）——把它丢进线程池只是换了个线程独占 GIL：

    在 to_thread 里执行            loop 推进   耗时
    time.sleep(0.6)（释放 GIL）      37 次     0.606s
    json.dumps(裸 float，57.9MB)      0 次     1.293s
    json.dumps(float32+base64，15.3MB) 1 次     0.055s

⇒ 真正的修法是把**要序列化的字节数**压下来（v2 = float32 打包 + base64，
体积 3.8x↓、dumps 23x↓），再叠加「离线程 + 快照 + 同批合并」。

本文件锁八条契约：
  ① 写盘期间事件循环继续推进（不得停转）；
  ② 同批并发未命中的整文件重写被合并（而不是 N 次）；
  ③ 落盘格式为 v2（base64(float32)），条目数与插入数一致；
  ④ 新条目必须触发新落盘、缓存命中不得重写；
  ⑤ 跨 event loop 复用同一实例时锁必须重建（`asyncio.Lock` 绑定 loop）；
  ⑥ 体积代理：v2 必须比裸 float JSON 小 ≥3 倍（耗时 ∝ 输出字节数）；
  ⑦ v1（裸 float 数组）文件必须仍可读 —— 否则上线即丢 60MB 已有缓存；
  ⑧ v2 写盘必须能被新实例读回（读不回 = 重复调 embedding API = 花钱）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import pathlib
import time

import pytest

from anima.memory.vector_memory import Embedder

#: 量级取自生产实测（60.6MB 缓存的 json.dumps = 1.48–1.52s）。
#: 测试里缩短到 0.35s，足以让「同步」在 ticker 上留下 0–1 次推进。
_SLOW_WRITE_SECONDS = 0.35
_DIM = 8


# ---------------------------------------------------------------------------
# 替身：只覆盖 embed() 真正用到的 aiohttp 契约
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status = status

    async def json(self) -> dict:
        return self._payload

    async def text(self) -> str:
        return ""

    async def __aenter__(self) -> "_FakeResp":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakeSession:
    """最小 aiohttp.ClientSession 替身（post → async context manager）。"""

    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self._status = status
        self.closed = False
        self.posts = 0

    def post(self, *args, **kwargs) -> _FakeResp:
        self.posts += 1
        return _FakeResp(self._payload, self._status)


def _make_embedder(tmp_path, dim: int = _DIM, vec_value: float = 0.25) -> Embedder:
    """构造 Embedder 并塞入假 session（不触网）。"""
    embedder = Embedder("http://x", "k", data_dir=str(tmp_path), cache=True)
    embedder._session = _FakeSession({"data": [{"embedding": [vec_value] * dim}]})
    return embedder


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _patch_slow_write_text(monkeypatch, seconds: float = _SLOW_WRITE_SECONDS) -> None:
    """把落盘变慢 —— 模拟 60MB 缓存真实开销。**新老实现都会走到这里**。"""
    real = pathlib.Path.write_text

    def slow_write_text(self, data, *args, **kwargs):
        time.sleep(seconds)
        return real(self, data, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "write_text", slow_write_text)


def _count_cache_writes(monkeypatch) -> dict:
    """统计 `embedding_cache.json` 的整文件重写次数。"""
    real = pathlib.Path.write_text
    counter = {"n": 0}

    def counting_write_text(self, data, *args, **kwargs):
        if self.name == "embedding_cache.json":
            counter["n"] += 1
        return real(self, data, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "write_text", counting_write_text)
    return counter


async def _loop_ticks_during(coro, interval: float = 0.01) -> int:
    """执行 `coro` 期间事件循环推进了多少次（≥5 表示 loop 没被冻住）。"""
    ticks = 0

    async def _tick():
        nonlocal ticks
        while True:
            await asyncio.sleep(interval)
            ticks += 1

    task = asyncio.create_task(_tick())
    try:
        await coro
    finally:
        task.cancel()
    return ticks


# ---------------------------------------------------------------------------
# ① 核心契约：写盘期间 loop 必须继续推进
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cache_write_does_not_block_event_loop(tmp_path, monkeypatch):
    """缓存整文件重写期间，事件循环不得停转。"""
    embedder = _make_embedder(tmp_path)
    _patch_slow_write_text(monkeypatch)

    ticks = await _loop_ticks_during(embedder.embed("一条必然缓存未命中的文本"))

    assert ticks >= 5, (
        f"缓存写盘期间事件循环停转（ticks={ticks}，期望 ≥5）"
        f"—— 整文件重写仍在 loop 线程上同步执行"
    )
    assert (tmp_path / "embedding_cache.json").exists(), "修复不得破坏持久化"


# ---------------------------------------------------------------------------
# ② 同批并发未命中：合并重写
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_cache_writes_are_coalesced(tmp_path, monkeypatch):
    """8 条并发未命中不得重写 8 次整文件。"""
    embedder = _make_embedder(tmp_path)
    writes = _count_cache_writes(monkeypatch)

    await asyncio.gather(*[embedder.embed(f"并发文本-{i}") for i in range(8)])

    assert writes["n"] >= 1, "8 条未命中一次都没落盘"
    assert writes["n"] <= 2, (
        f"同批并发未命中重写了 {writes['n']} 次整文件（期望 ≤2）"
        f"—— 缺 dirty/合并，N 条消息 = N × 60MB dumps"
    )


# ---------------------------------------------------------------------------
# ③ 落盘格式与完整性
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cache_file_is_complete_and_valid(tmp_path):
    """落盘必须是合法 JSON、条目数与插入数一致、值为 float32+base64（v2）。"""
    embedder = _make_embedder(tmp_path)
    for i in range(5):
        await embedder.embed(f"文本-{i}")

    data = json.loads((tmp_path / "embedding_cache.json").read_text("utf-8"))

    assert len(data) == 5, f"落盘条目数 {len(data)} != 插入数 5"
    for key, packed in data.items():
        assert len(key) == 32 and all(c in "0123456789abcdef" for c in key)
        assert isinstance(packed, str), (
            f"落盘值应为 base64 字符串（v2），实为 {type(packed).__name__}"
            f"—— 裸 float JSON 的 dumps 全程独占 GIL"
        )
        assert len(base64.b64decode(packed)) == _DIM * 4, "float32 ⇒ 每维 4 字节"


# ---------------------------------------------------------------------------
# ④ 合并不得过度：新条目必写、命中不写
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_new_entry_writes_again_and_cache_hit_does_not(tmp_path, monkeypatch):
    embedder = _make_embedder(tmp_path)
    writes = _count_cache_writes(monkeypatch)

    await embedder.embed("第一条")
    assert writes["n"] == 1

    await embedder.embed("第二条")  # 未命中 → 必须落盘
    assert writes["n"] == 2, "新文本未触发落盘（版本合并过度，会丢缓存）"

    await embedder.embed("第二条")  # 命中缓存
    assert writes["n"] == 2, "缓存命中不应重写整文件"


# ---------------------------------------------------------------------------
# ⑤ 跨 event loop 复用同一实例
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_save_cache_rebinds_when_event_loop_changes(tmp_path):
    """`asyncio.Lock` 一旦绑定某 loop 就不能跨 loop 复用 ⇒ 必须重建。

    pytest-asyncio 每个用例一个新 loop，同一实例被跨用例复用是现实场景；
    锁不重建会 `RuntimeError: ... is bound to a different event loop`，
    而该异常会被 `embed()` 的 HTTP 重试 `try` 吞成「Embedding 请求失败」。
    """
    embedder = _make_embedder(tmp_path)
    embedder._cache["a" * 32] = [0.1] * _DIM
    embedder._cache_version = 1

    await embedder._save_cache()
    first_loop = embedder._save_loop

    embedder._cache["b" * 32] = [0.2] * _DIM
    embedder._cache_version = 2

    def _save_in_own_loop() -> None:
        asyncio.run(embedder._save_cache())  # 独立线程 + 独立 loop

    await asyncio.to_thread(_save_in_own_loop)

    assert embedder._save_loop is not first_loop, "跨 loop 复用未重建锁"
    data = json.loads((tmp_path / "embedding_cache.json").read_text("utf-8"))
    assert "b" * 32 in data, "换 loop 后未能落盘"


# ---------------------------------------------------------------------------
# ⑥ 体积代理：序列化耗时 ∝ 输出字节数（同一个 C 编码器）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cache_payload_is_packed_not_raw_float_json(tmp_path):
    """v2 必须比裸 float JSON 小 ≥3 倍 —— 体积就是 GIL 被占的时长。

    对照必须用**真实精度**的向量值（`repr(1/3)` = 18 字符），否则 v1 也会因为
    值短（如 `0.25`）而显得紧凑，判据会被混淆。
    """
    dim = 64
    vec_value = 1 / 3
    embedder = _make_embedder(tmp_path, dim=dim, vec_value=vec_value)
    for i in range(10):
        await embedder.embed(f"体积文本-{i}")

    raw = (tmp_path / "embedding_cache.json").read_text("utf-8")
    data = json.loads(raw)
    naive = json.dumps({k: [vec_value] * dim for k in data}, ensure_ascii=False)

    assert len(raw) * 3 <= len(naive), (
        f"缓存体积 {len(raw)}B vs 裸 float JSON {len(naive)}B（期望 ≥3x 缩减）"
        f"—— json.dumps 的 C 编码器全程不释放 GIL，体积正比于冻结时长"
    )


# ---------------------------------------------------------------------------
# ⑦ v1 向后兼容
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_legacy_float_array_cache_still_loads(tmp_path):
    """v1（裸 float 数组）文件必须仍可读。

    读不了 = 上线后丢掉生产已有的 60MB 缓存 = 全量重新嵌入（真金白银）。
    """
    key = Embedder._hash("历史文本")
    (tmp_path / "embedding_cache.json").write_text(
        json.dumps({key: [0.125] * _DIM}), "utf-8"
    )
    embedder = _make_embedder(tmp_path, vec_value=0.9)

    vec = await embedder.embed("历史文本")

    assert vec == [0.125] * _DIM, "v1 缓存未被复用"
    assert embedder._session.posts == 0, "读 v1 缓存仍发起了网络请求"


# ---------------------------------------------------------------------------
# ⑧ v2 往返
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cache_round_trips_across_instances(tmp_path):
    """v2 写盘必须能被新实例读回（读不回 = 重复调 embedding API）。"""
    first = _make_embedder(tmp_path)
    await first.embed("往返文本")
    assert first._session.posts == 1

    second = _make_embedder(tmp_path)
    vec = await second.embed("往返文本")

    assert second._session.posts == 0, "v2 缓存未被读回，重复调用了 embedding API"
    assert vec is not None and len(vec) == _DIM
    assert all(abs(x - 0.25) < 1e-6 for x in vec)
