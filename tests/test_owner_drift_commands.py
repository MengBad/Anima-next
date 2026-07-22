"""Owner 漂移命令：暂停反刍 / 笔记维护 / 状态。"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from anima.cognitive.learning import SelfLearning, reset_learning_singleton
from anima.llm.handler import LLMHandler


@pytest.fixture(autouse=True)
def _reset_learning():
    reset_learning_singleton()
    yield
    reset_learning_singleton()


def test_pause_and_resume_rumination(tmp_path):
    sl = SelfLearning(str(tmp_path))
    until = sl.pause_rumination(2.0)
    assert until > time.time()
    assert sl.is_rumination_paused() is True
    assert sl.ruminate  # method exists
    # 模拟过期
    sl.rumination_paused_until = time.time() - 1
    assert sl.is_rumination_paused() is False
    sl.pause_rumination(1.0)
    assert sl.resume_rumination() is True
    assert sl.is_rumination_paused() is False


def test_pause_blocks_ruminate_sync_gate(tmp_path):
    """ruminate 在暂停时立即返回 None（不调 LLM）。"""
    import asyncio

    sl = SelfLearning(str(tmp_path))
    sl.pause_rumination(24)

    class FakeLLM:
        async def chat(self, *a, **k):
            raise AssertionError("paused rumination must not call LLM")

    result = asyncio.get_event_loop().run_until_complete(
        sl.ruminate(FakeLLM(), "x" * 80)
    ) if False else None
    # use asyncio.run for clarity
    result = asyncio.run(sl.ruminate(FakeLLM(), "用户说了很长一段话" * 5))
    assert result is None


def test_delete_last_and_clear_notes(tmp_path):
    sl = SelfLearning(str(tmp_path))
    assert sl.add_note("今天聊得很愉快", category="general")
    assert sl.add_note("还想再玩一会儿", category="general")
    assert sl.count_note_lines() == 2
    assert sl.delete_last_notes(1) == 1
    assert sl.count_note_lines() == 1
    assert sl.clear_notes() == 1
    assert sl.count_note_lines() == 0


def test_purge_sticky_via_learning(tmp_path):
    sl = SelfLearning(str(tmp_path))
    # 直接写盘绕过写入闸门，模拟历史粘滞
    sl._persist.write_text(
        "self_notes.md",
        "[2026-07-01 12:00] 今天开心\n[2026-07-01 12:01] 一百个小鱼干的债\n",
    )
    dropped = sl.purge_sticky_notes()
    assert dropped == 1
    assert "小鱼干" not in sl.get_notes()
    assert "开心" in sl.get_notes()


def test_pause_persists(tmp_path):
    sl = SelfLearning(str(tmp_path))
    sl.pause_rumination(12)
    sl2 = SelfLearning(str(tmp_path))
    assert sl2.is_rumination_paused() is True


@pytest.mark.asyncio
async def test_handler_drift_status_command(tmp_path, monkeypatch):
    from anima.core.config import AnimaConfig, PersistenceConfig, PermissionConfig

    cfg = AnimaConfig(
        persistence=PersistenceConfig(data_dir=str(tmp_path)),
        permissions=PermissionConfig(owners=[10001]),
    )
    core = MagicMock()
    core.config = cfg
    core.gateway = MagicMock()
    core.gateway.send = AsyncMock()
    core.heartbeat = None
    core.tasks = MagicMock()
    core.audit = None
    handler = LLMHandler(core)
    handler._is_owner = lambda uid: True  # type: ignore

    event = {"raw": {"sender": {"nickname": "owner"}}}
    ok = await handler._handle_command(event, "!anima drift status", 10001, None, "private")
    assert ok is True
    core.gateway.send.assert_awaited()
    msg = core.gateway.send.await_args.args[0].message
    assert "漂移" in msg
    assert "general" in msg


@pytest.mark.asyncio
async def test_handler_drift_pause(tmp_path):
    from anima.core.config import AnimaConfig, PersistenceConfig, PermissionConfig
    from anima.cognitive.learning import get_learning

    cfg = AnimaConfig(
        persistence=PersistenceConfig(data_dir=str(tmp_path)),
        permissions=PermissionConfig(owners=[10001]),
    )
    core = MagicMock()
    core.config = cfg
    core.gateway = MagicMock()
    core.gateway.send = AsyncMock()
    core.heartbeat = None
    core.tasks = MagicMock()
    core.audit = None
    handler = LLMHandler(core)

    ok = await handler._handle_command({}, "!anima drift pause 2", 10001, None, "private")
    assert ok is True
    sl = get_learning(str(tmp_path))
    assert sl.is_rumination_paused()
