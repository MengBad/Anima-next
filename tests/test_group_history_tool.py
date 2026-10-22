"""read_group_history 工具契约（2026-10-23）。

## 为什么有这个工具

在此之前，Agent 的工具表里**没有任何一个能读群聊记录**：
- `read_file` 只能读 `data/` 目录里的文件
- `get_group_members` 只给成员列表
- `get_group_info` 只给群名/人数

⇒ **「帮我盯着群动态」「总结这周的讨论」这类最自然的长期目标结构上做不成。**

生产实证（2026-10-23）：Goal「整理一次群动态」连续两轮，
Agent 都如实报告「缺少读取历史聊天记录的 API 或工具权限」。
**它没找借口，是真读不到。**

## 本测试锁死的边界

1. 跨用户聚合：同群多个用户桶能合并成时间线；
2. **绝不跨群**：别的群的内容不得出现；
3. **绝不返回私聊**：`private_*` 桶被排除；
4. 时间窗过滤 + 条数硬上限（防一次拉爆）；
5. 无时间戳的条目跳过（不猜时间）；
6. 工具仅 owner 可用（`min_level=LEVEL_OWNER`）。
"""
import time
import types

import pytest

from anima.memory.chat_memory import ChatMemory
from anima.tools.builtin.group_history import (
    MAX_HOURS, MAX_ROWS, make_read_group_history,
)


class _OD:
    """模拟 OrderedDict 的 items()。"""

    def __init__(self, pairs):
        self._pairs = pairs

    def items(self):
        return list(self._pairs)


def _mem_with(*buckets):
    m = ChatMemory()
    m._store.clear()
    for key, entries in buckets:
        m._store[key] = _OD([("r1", entries)])
    return m


def _msg(name, content, ago=0.0, role="user"):
    return {"role": role, "name": name, "content": content,
            "time": time.time() - ago}


# ── 1. 跨用户聚合 ─────────────────────────────────────────────────────


def test_aggregates_across_user_buckets():
    m = _mem_with(
        ("group_111_1", [_msg("小明", "聊AI绘图", 100)]),
        ("group_111_2", [_msg("小红", "我也在试", 50)]),
    )
    rows = m.get_group_history(111, hours=24)
    assert len(rows) == 2, f"应聚合两个用户桶，实得 {len(rows)}"
    assert rows[0]["name"] == "小明", "应按时间正序（早的在前）"
    assert rows[1]["name"] == "小红"


# ── 2. 绝不跨群 ───────────────────────────────────────────────────────


def test_never_leaks_other_groups():
    m = _mem_with(
        ("group_111_1", [_msg("小明", "本群消息")]),
        ("group_222_1", [_msg("别人", "别群消息")]),
    )
    rows = m.get_group_history(111, hours=24)
    texts = " ".join(r["content"] for r in rows)
    assert "本群消息" in texts
    assert "别群消息" not in texts, "🔴 绝不能把别群内容带进来"


# ── 3. 绝不返回私聊 ───────────────────────────────────────────────────


def test_never_returns_private_bucket():
    m = _mem_with(
        ("group_111_1", [_msg("小明", "群消息")]),
        ("private_999", [_msg("某人", "私聊内容")]),
    )
    rows = m.get_group_history(111, hours=24)
    texts = " ".join(r["content"] for r in rows)
    assert "群消息" in texts
    assert "私聊内容" not in texts, "🔴 私聊绝不能出现在群历史里"


# ── 4. 时间窗与上限 ───────────────────────────────────────────────────


def test_hours_filter():
    m = _mem_with(("group_111_1", [
        _msg("小明", "很久以前", ago=10 * 3600),
        _msg("小明", "刚刚", ago=10),
    ]))
    rows = m.get_group_history(111, hours=1)
    assert len(rows) == 1
    assert "刚刚" in rows[0]["content"]


def test_limit_keeps_latest():
    m = _mem_with(("group_111_1", [
        _msg("小明", f"第{i}条", ago=100 - i) for i in range(10)
    ]))
    rows = m.get_group_history(111, hours=24, limit=3)
    assert len(rows) == 3
    assert "第9条" in rows[-1]["content"], "应保留最新的"


def test_entries_without_timestamp_skipped():
    """无时间戳的条目无法定位，跳过（不猜）。"""
    m = _mem_with(("group_111_1", [
        {"role": "user", "name": "小明", "content": "没有时间戳"},
        _msg("小明", "有时间戳"),
    ]))
    rows = m.get_group_history(111, hours=24)
    assert len(rows) == 1
    assert "有时间戳" in rows[0]["content"]


# ── 5. 工具层：夹取上限 ───────────────────────────────────────────────


def test_tool_clamps_extreme_params():
    """调用方传超大值时必须被夹到上限（防一次拉爆）。"""
    calls = {}

    class _FakeMem:
        def get_group_history(self, gid, *, hours, limit):
            calls["hours"], calls["limit"] = hours, limit
            return []

    import anima.memory.chat_memory as cm
    orig = cm.get_chat_memory
    cm.get_chat_memory = lambda: _FakeMem()
    try:
        tool = make_read_group_history(types.SimpleNamespace())
        tool(group_id=111, hours=99999, limit=99999)
    finally:
        cm.get_chat_memory = orig

    assert calls["hours"] == MAX_HOURS
    assert calls["limit"] == MAX_ROWS


def test_tool_handles_empty_history():
    m = _mem_with()
    import anima.memory.chat_memory as cm
    orig = cm.get_chat_memory
    cm.get_chat_memory = lambda: m
    try:
        out = make_read_group_history(types.SimpleNamespace())(group_id=111)
    finally:
        cm.get_chat_memory = orig
    assert "没有可读的记录" in out


def test_tool_rejects_bad_group_id():
    tool = make_read_group_history(types.SimpleNamespace())
    assert "群号" in tool(group_id=0)
    assert "群号" in tool(group_id=-5)


def test_tool_formats_timeline():
    m = _mem_with(("group_111_1", [_msg("小明", "测试内容")]))
    import anima.memory.chat_memory as cm
    orig = cm.get_chat_memory
    cm.get_chat_memory = lambda: m
    try:
        out = make_read_group_history(types.SimpleNamespace())(group_id=111)
    finally:
        cm.get_chat_memory = orig
    assert "消息" not in out or "条记录" in out
    assert "小明" in out
    assert "测试内容" in out


# ── 6. 注册与权限 ─────────────────────────────────────────────────────


def test_tool_registered_owner_only():
    import inspect
    from anima.tools.builtin import register_all_builtin

    src = inspect.getsource(register_all_builtin)
    assert '"read_group_history"' in src, "工具必须注册"
    i = src.find('"read_group_history"')
    seg = src[i:i + 900]
    assert "LEVEL_OWNER" in seg, (
        "🔴 必须是 owner-only —— 这是「读别人说过什么」的能力"
    )
    assert 'required=["group_id"]' in seg
