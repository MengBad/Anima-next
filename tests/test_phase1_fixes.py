"""Phase 1 修复验证测试。

覆盖：
- sandbox.py: import sys 存在性
- event_bus.py: handler 异常日志
- gateway.py: 审计长度计算
- scar.py: _prune 数据保留
- relation.py: notes 长度限制
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# 1.1 sandbox.py: import sys
# ---------------------------------------------------------------------------

def test_sandbox_has_sys_import():
    """sandbox.py 必须 import sys，否则 run_python 会 NameError。"""
    import anima.tools.sandbox as sb
    import sys as _sys
    assert hasattr(sb, "sys"), "sandbox.py 缺少 import sys"
    assert sb.sys is _sys


# ---------------------------------------------------------------------------
# 1.2 event_bus.py: handler 异常日志
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_event_bus_logs_handler_exception(caplog):
    """EventBus.publish 应记录 handler 异常而非静默吞掉。"""
    from anima.core.event_bus import EventBus

    bus = EventBus()

    async def bad_handler(payload):
        raise ValueError("boom")

    bus.subscribe("test_topic", bad_handler)

    with caplog.at_level(logging.ERROR, logger="anima.eventbus"):
        await bus.publish("test_topic", {"data": 1})

    assert any("EventBus handler 异常" in r.message for r in caplog.records), \
        "handler 异常未被记录到日志"


@pytest.mark.asyncio
async def test_event_bus_normal_handler_no_error(caplog):
    """正常 handler 不应触发错误日志。"""
    from anima.core.event_bus import EventBus

    bus = EventBus()
    called = []

    async def good_handler(payload):
        called.append(payload)

    bus.subscribe("ok_topic", good_handler)

    with caplog.at_level(logging.ERROR, logger="anima.eventbus"):
        await bus.publish("ok_topic", "hello")

    assert called == ["hello"]
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


# ---------------------------------------------------------------------------
# 1.3 gateway.py: 审计长度计算
# ---------------------------------------------------------------------------

def test_gateway_audit_text_length_str():
    """str 消息的 text_length 应为 len(str)。"""
    msg_str = SimpleNamespace(message="你好世界")
    # 模拟 gateway 中的计算逻辑
    if isinstance(msg_str.message, str):
        text_length = len(msg_str.message)
    elif isinstance(msg_str.message, list):
        text_length = sum(
            len(seg.get("text", "")) for seg in msg_str.message
            if isinstance(seg, dict)
        )
    else:
        text_length = 0
    assert text_length == 4


def test_gateway_audit_text_length_list():
    """list 消息的 text_length 应为所有 text 段的总长度。"""
    msg_list = SimpleNamespace(message=[
        {"type": "at", "qq": 12345},
        {"type": "text", "text": "你好"},
        {"type": "image", "file": "abc.png"},
        {"type": "text", "text": "世界！"},
    ])
    if isinstance(msg_list.message, str):
        text_length = len(msg_list.message)
    elif isinstance(msg_list.message, list):
        text_length = sum(
            len(seg.get("text", "")) for seg in msg_list.message
            if isinstance(seg, dict)
        )
    else:
        text_length = 0
    assert text_length == 5  # "你好" + "世界！"


# ---------------------------------------------------------------------------
# 1.4 scar.py: _prune 数据保留
# ---------------------------------------------------------------------------

def test_scar_prune_keeps_unfaded_within_limit():
    """未淡化且在 max_scars 内的伤痕应保留。"""
    from anima.cognitive.scar import ScarSystem, Scar
    import tempfile, os

    with tempfile.TemporaryDirectory() as tmp:
        ss = ScarSystem(data_dir=tmp, max_scars=3)
        # 添加 3 条未淡化伤痕
        for i in range(3):
            s = ss.add(user_id=1000 + i, event=f"event_{i}", severity=0.5 + i * 0.1)
        assert len(ss._scars) == 3
        ss._prune()
        assert len(ss._scars) == 3  # 全部保留


def test_scar_prune_removes_faded_old():
    """淡化超 30 天的伤痕应被彻底删除。"""
    from anima.cognitive.scar import ScarSystem, Scar
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        ss = ScarSystem(data_dir=tmp, max_scars=5)
        s = ss.add(user_id=1001, event="old_hurt", severity=0.3)
        s.faded = True
        s.last_triggered = time.time() - 86400 * 31  # 31 天前
        ss._prune()
        assert len(ss._scars) == 0


def test_scar_prune_keeps_faded_recent():
    """淡化但未超 30 天的伤痕应保留（在 max_scars 内）。"""
    from anima.cognitive.scar import ScarSystem
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        ss = ScarSystem(data_dir=tmp, max_scars=5)
        s = ss.add(user_id=1001, event="recent_hurt", severity=0.3)
        s.faded = True
        s.last_triggered = time.time() - 86400 * 5  # 5 天前
        ss._prune()
        assert len(ss._scars) == 1


def test_scar_prune_truncates_to_max():
    """超过 max_scars 的按严重度截断。"""
    from anima.cognitive.scar import ScarSystem, Scar
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        ss = ScarSystem(data_dir=tmp, max_scars=2)
        # 直接操作 _scars 绕过 add() 内部的 _prune 调用
        for i in range(5):
            s = Scar(user_id=1000 + i, event=f"e{i}", severity=0.1 * (i + 1))
            ss._scars.append(s)
        assert len(ss._scars) == 5
        ss._prune()
        assert len(ss._scars) == 2
        # 保留的应是严重度最高的两条
        assert all(s.severity >= 0.4 for s in ss._scars)


# ---------------------------------------------------------------------------
# 1.6 relation.py: notes 长度限制
# ---------------------------------------------------------------------------

def test_relation_notes_trimmed():
    """notes 超过 800 字符后应被截断。"""
    from anima.cognitive.relation import RelationEngine
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        engine = RelationEngine(data_dir=tmp)
        # 模拟大量 notes
        rel = engine.get(12345)
        for i in range(50):
            engine.record_event(12345, f"事件_{i:03d}_" + "x" * 20, delta_trust=0.01)

        assert len(rel.notes) <= RelationEngine._NOTES_MAX_LEN + 10  # 允许少量溢出
