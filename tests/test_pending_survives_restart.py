"""重启后挂起任务仍能被回答：_active_agent_task 只活在内存，磁盘才是真源。"""

from __future__ import annotations

import time

import pytest

from anima.agent import ask_user as au
from anima.agent.state import AgentTask


class _Store:
    def __init__(self, items=None):
        self.items = {t.id: t for t in (items or [])}
        self.saved = 0

    def save(self, task):
        self.items[task.id] = task
        self.saved += 1


class _Agent:
    def __init__(self, store):
        self.store = store

    async def resume(self, task):
        self.resumed = task.id


class _Core:
    def __init__(self, store):
        self.agent = _Agent(store)
        self._active_agent_task = None


def _mk(task_id="t1", *, attached_ago=0.0, status="waiting_user", text="要账号密码"):
    t = AgentTask(id=task_id, request="华为云任务", creator=1)
    t.status = status
    t.pending_question = {
        "questions": [{"text": text, "kind": "text", "options": [], "allow_custom": True}],
        "_attached_at": time.time() - attached_ago,
    }
    t.updated_at = time.time()
    return t


def test_adopt_finds_waiting_task_after_restart():
    """模拟重启：内存为空，磁盘上有一个未过期的 waiting_user。"""
    core = _Core(_Store([_mk()]))
    assert core._active_agent_task is None

    got = au.adopt_pending_from_store(core)

    assert got is not None
    assert core._active_agent_task is got


def test_adopt_skips_expired():
    core = _Core(_Store([_mk(attached_ago=au.PENDING_TTL_SEC + 60)]))
    assert au.adopt_pending_from_store(core) is None


def test_adopt_skips_non_waiting():
    core = _Core(_Store([_mk(status="completed")]))
    assert au.adopt_pending_from_store(core) is None


def test_adopt_picks_most_recent():
    old = _mk("old", attached_ago=600)
    new = _mk("new", attached_ago=1)
    core = _Core(_Store([old, new]))
    got = au.adopt_pending_from_store(core)
    assert got is not None and got.id == "new"


def test_adopt_no_store_is_noop():
    core = _Core(None)
    assert au.adopt_pending_from_store(core) is None


def test_resolve_reply_works_after_restart():
    """核心回归：重启后 owner 的回答仍被消费，而不是当普通聊天。"""
    core = _Core(_Store([_mk()]))

    got = au.resolve_reply(core, "你来操作")

    assert got is not None, "重启后回答被当成普通聊天了"
    assert "你来操作" in got


def test_clear_stale_marks_expired_not_completed():
    """过期不能标 completed —— 那是在撒谎，还掩盖任务已死。"""
    t = _mk(attached_ago=au.PENDING_TTL_SEC + 60)
    core = _Core(_Store([t]))
    core._active_agent_task = t

    assert au.clear_stale(core) is True
    assert t.status == "expired"
    assert t.status != "completed"


def test_clear_stale_keeps_fresh():
    t = _mk(attached_ago=10)
    core = _Core(_Store([t]))
    core._active_agent_task = t

    assert au.clear_stale(core) is False
    assert t.status == "waiting_user"