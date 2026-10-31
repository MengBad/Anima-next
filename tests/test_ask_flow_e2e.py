"""反问兜底的端到端闭环：挂起 -> 回答 -> 恢复。

生产实证（17:14 / 17:17 两轮）：
  17:14 你：找个画师联系方式 -> 它搜一次 -> 反问「你要找哪位」
  17:17 你：SUZUME            -> 被当新任务，再搜一次，再反问
根因有二：
  1. tool_calls>0 就不能兜底（已修正）
  2. attach_pending 从未被调用 -> resolve_reply 恒 None（本次修正）
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from anima.agent import ask_user as au
from anima.agent.ask_backstop import build_pending_question, is_ask_back


class _Store:
    def __init__(self):
        self.items = {}
        self.saves = 0

    def save(self, task):
        self.items[getattr(task, "id", "")] = task
        self.saves += 1


class _Agent:
    def __init__(self):
        self.store = _Store()


def _core():
    c = SimpleNamespace(agent=_Agent())
    c._active_agent_task = None
    return c


def test_attach_then_resolve_roundtrip():
    """挂起后，owner 的回答必须被消费并恢复任务。"""
    core = _core()
    pq = build_pending_question("你要找哪位画师呀？")
    task = au.attach_pending(core, pq, user_id=1562290139)

    assert core._active_agent_task is task
    assert task.pending_question.get("questions")
    assert core.agent.store.saves == 1   # 落盘了

    out = au.resolve_reply(core, "SUZUME")
    assert out is not None
    assert "SUZUME" in out
    assert task.status == "executing"
    assert task.pending_question == {}


def test_no_pending_means_normal_chat():
    core = _core()
    assert au.resolve_reply(core, "今天天气不错") is None


def test_attach_prefers_live_agent_task():
    """有活跃 AgentTask 时挂到它上面，恢复后才能继续跑。"""
    core = _core()
    live = SimpleNamespace(
        id="t1", status="executing", updated_at=100.0,
        pending_question={}, steps=[], created_at=1.0,
    )
    core.agent.store.items["t1"] = live
    pq = build_pending_question("选哪个？")
    task = au.attach_pending(core, pq)
    assert task.id == "t1"
    assert live.status == "waiting_user"


def test_stale_pending_is_cleared():
    core = _core()
    pq = build_pending_question("问点什么")
    task = au.attach_pending(core, pq)
    # 手动把时间戳推到很久以前
    task.pending_question["_attached_at"] = 0.0
    assert au.clear_stale(core, now=au.PENDING_TTL_SEC + 1) is True
    assert core._active_agent_task is None


def test_realtime_backstop_sample_hits():
    """17:14:38 那条真实漏网回复，现在必须被兜住。"""
    text = ("行啦行啦，先把那个画师的名字或者主页交出来呀！"
            "刚才在X上胡乱翻了一圈，根本不知道你要找的是哪位大触嘛喵~")
    # 生产环境此刻 tool_calls=1（它先盲搜了一次）
    assert is_ask_back(text, detail_type="private", tool_calls=1) is not None
