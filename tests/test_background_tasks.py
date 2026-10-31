"""P2 · 后台任务观察与控制。

调研结论（2026-10-31）：后台执行本来就存在 ——
OwnerAgent.submit(auto_run=True) 就是 asyncio.create_task，任务真的在后台跑。
缺的是「看得到进度 / 能叫停」，本模块补这一层。
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from anima.agent import background as bg


class _Step:
    def __init__(self, status="pending"):
        self.status = status


class _Task:
    def __init__(self, tid="t1", status="executing", steps=None, updated_at=None):
        self.id = tid
        self.status = status
        self.request = "帮我做点什么"
        self.steps = steps if steps is not None else [_Step("done"), _Step()]
        self.current_step = 1
        self.replan_count = 0
        self.pending_question = {}
        self.error = ""
        self.updated_at = updated_at if updated_at is not None else time.time()


class _Store:
    def __init__(self, items):
        self.items = {t.id: t for t in items}

    def get(self, tid):
        return self.items.get(tid)


class _Executor:
    def __init__(self):
        self.cancelled = []

    def request_cancel(self, tid):
        self.cancelled.append(tid)


class _Agent:
    def __init__(self, store, executor=None):
        self.store = store
        self.executor = executor or _Executor()


def _core(items, executor=None):
    return SimpleNamespace(agent=_Agent(_Store(items), executor))


def test_list_tasks_shows_progress():
    core = _core([_Task()])
    out = bg.list_tasks(core)
    assert "t1" in out
    assert "executing" in out
    assert "1/2" in out


def test_list_tasks_empty():
    assert "没有" in bg.list_tasks(_core([]))


def test_get_status_detail():
    core = _core([_Task()])
    out = bg.get_task_status(core, "t1")
    assert "executing" in out
    assert "1/2" in out


def test_get_status_missing():
    assert "找不到" in bg.get_task_status(_core([]), "nope")


def test_wait_returns_immediately_on_terminal():
    core = _core([_Task(status="completed")])
    out = asyncio.run(bg.wait_task(core, "t1", timeout_sec=5))
    assert "结束" in out and "completed" in out


def test_wait_times_out_without_cancelling():
    core = _core([_Task(status="executing")])
    t0 = time.time()
    out = asyncio.run(bg.wait_task(core, "t1", timeout_sec=1))
    assert "仍在跑" in out
    assert time.time() - t0 < 5
    # 任务没被取消
    assert core.agent.store.get("t1").status == "executing"


def test_kill_task():
    core = _core([_Task(status="executing")])
    out = bg.kill_task(core, "t1")
    assert "正在停" in out
    assert core.agent.executor.cancelled == ["t1"]


def test_kill_terminal_task_is_noop():
    core = _core([_Task(status="completed")])
    out = bg.kill_task(core, "t1")
    assert "已经结束" in out
    assert core.agent.executor.cancelled == []


def test_group_visibility():
    """后台任务工具必须标 private_only —— 群里不该出现任务面板。"""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "anima" / "tools" / "builtin" / "agent_tools.py").read_text(encoding="utf-8")
    for name in ("list_tasks", "get_task_status", "wait_task", "kill_task"):
        idx = src.find(f'"{name}"')
        assert idx > 0, f"找不到 {name} 注册"
        window = src[idx:idx + 700]
        assert "private_only=True" in window, f"{name} 没标 private_only"
