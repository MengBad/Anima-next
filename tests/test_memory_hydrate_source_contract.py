# -*- coding: utf-8 -*-
"""宪法记忆水合的数据源契约（2026-09-22）。

## 为什么需要这组测试

`MemoryEngine.hydrate` **真的跑在生产启动路径上**：

    anima_core.start()
      → MemoryAuthority(hydrate_now=True)              # anima_core.py:295 开机水合
      → MemoryAuthority.hydrate_from_chat(force=True)  # anima_core.py:361 每 6h 周期水合
        → MemoryEngine.hydrate(cm)                     # memory_engine.py:56

日志与审计都在打，但生产**连续 6 次开机**都是同一句：

    宪法记忆开机水合: 共 0 条 episode(只读, 有界)
    {"type": "constitution_memory_hydrated", "added": 0, "source": "boot"}

根因：`hydrate` 读 `entry.get("text")`，而 `chat_memory.get_recent_group_context`
的返回结构是 `{"role", "content"}` —— **没有 `text` 键** ⇒ 逐条 `continue` ⇒ 恒空。

## 这个 bug 为什么能活下来（本文件存在的理由）

既有两个测试文件的 fake CM 都返回 `{"role": "user", "text": "..."}`：
fake 的键名与生产实物**分叉** ⇒ 单测全绿、生产恒空。

    tests/test_memory_authority_hydration.py::_FakeCM
    tests/test_constitution_memory_closure.py::_FakeChatMemory

所以本文件一律用**真实 `ChatMemory` 实例**驱动，把「数据源形状」钉死：
将来谁再改坏键名，这里会红，而不是靠生产日志才被发现。
"""
from __future__ import annotations

import inspect
import logging
import re

from anima.constitution.memory_engine import MemoryEngine
from anima.memory.chat_memory import ChatMemory

GID = 727631526
UID = 1562290139
_LOGGER_NAME = "anima.constitution.memory_engine"


def _real_cm(text: str = "今天天气不错"):
    """真实 ChatMemory（不落盘）写入一条群消息 ⇒ 群短时缓冲真的有内容。"""
    cm = ChatMemory(persist_path="")
    cm.observe_group_message(GID, UID, "阿布", text, message_id=1001)
    return cm


# --------------------------------------------------------------------------
# 1) 核心：真实数据源必须能喂进 hydrate
# --------------------------------------------------------------------------
def test_hydrate_consumes_real_chat_memory_entries():
    """真实 ChatMemory 里有一条群消息 ⇒ hydrate 必须产出 episode。

    RED 判据：修复前 added == 0（读 text 键取不到 content 键）。
    """
    cm = _real_cm()
    assert cm.group_recent_ids() == {GID}, "夹具失效：真实 ChatMemory 未产出群缓冲"
    added = MemoryEngine().hydrate(cm)
    assert added > 0, (
        "hydrate 从**真实** chat_memory 读不到任何条目 —— 取值键与生产实物分叉"
        "（生产条目只有 role/content 两个键）"
    )


def test_hydrated_episode_carries_real_text():
    """采到的 episode 必须真的带原文，而不是只增了计数。"""
    engine = MemoryEngine()
    engine.hydrate(_real_cm("喵喵喵测试内容"))
    eps = engine.snapshot().episodes
    assert eps, "夹具失效：未产出任何 episode"
    texts = [str(e.content.get("text") or "") for e in eps]
    assert any("喵喵喵测试内容" in t for t in texts), (
        f"采到的 episode 不含原文 ⇒ 键名修对了但内容没接上: {texts}"
    )


# --------------------------------------------------------------------------
# 2) 契约：hydrate 支持的键 必须与真实条目键有交集
# --------------------------------------------------------------------------
def test_hydrate_supported_keys_intersect_real_entry_keys():
    """把「数据源形状」变成可执行断言，防再次分叉。"""
    cm = _real_cm()
    rows = cm.get_recent_group_context(GID)
    assert rows, "夹具失效"
    real_keys = set(rows[0])
    assert "content" in real_keys, (
        f"生产条目形状变了（现在是 {sorted(real_keys)}）—— 本守卫与 hydrate 都需同步"
    )

    src = inspect.getsource(MemoryEngine.hydrate)
    # 🔴 只取**真正用于取值**的键（`entry.get("<key>")`）。
    # 不能扫函数全文：函数体里还有 provenance 分支的 `"content"` 字面量，
    # 扫全文会让这条守卫在 bug 未修时**假通过**（第一版就踩了这个坑）。
    read_keys = set(re.findall(r'entry\.get\(\s*"([A-Za-z_]+)"', src))
    read_keys.discard("role")  # role 是元数据，不是正文
    assert read_keys, "hydrate 源码里找不到任何正文取值键"
    assert "content" in read_keys, (
        f"hydrate 的正文取值键是 {sorted(read_keys)}，不含生产键 `content` ⇒ 水合恒空"
        "（本次事故的机制）"
    )
    assert read_keys & real_keys, (
        f"hydrate 取值键 {sorted(read_keys)} 与真实条目键 {sorted(real_keys)} "
        "**无交集** ⇒ 水合恒空"
    )


def test_hydrate_keeps_legacy_text_backward_compat():
    """向后兼容：旧结构（带 text 键）仍应被支持，不得只改旗易帜。"""
    class _LegacyCM:
        def group_recent_ids(self):
            return {7}

        def get_recent_group_context(self, gid, limit=12):
            return [{"role": "user", "text": "旧结构也能读"}]

    assert MemoryEngine().hydrate(_LegacyCM()) == 1


# --------------------------------------------------------------------------
# 3) 可观测：必须能区分「跑了但 0 条」与「没跑」
# --------------------------------------------------------------------------
def test_hydrate_reports_sample_counts(caplog):
    """修复前：恒 0 条时**完全静默**，无法归因（开机 0 条 / 周期 0 条都看不见）。"""
    cm = _real_cm()
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        MemoryEngine().hydrate(cm)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("水合采样" in m for m in msgs), (
        "hydrate 无采样计数日志 ⇒ 生产上无法区分『跑了但 0 条』与『任务没跑』: "
        f"{msgs}"
    )


def test_hydrate_logs_zero_sample_explicitly(caplog):
    """空数据源也必须留痕（否则「开机 0 条」永远无法解释）。"""
    class _EmptyCM:
        def group_recent_ids(self):
            return set()

        def get_recent_group_context(self, gid, limit=12):
            return []

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        added = MemoryEngine().hydrate(_EmptyCM())
    assert added == 0
    msgs = [r.getMessage() for r in caplog.records]
    assert any("水合采样" in m for m in msgs), (
        f"空数据源未留痕 ⇒ 「开机 0 条」不可解释: {msgs}"
    )
