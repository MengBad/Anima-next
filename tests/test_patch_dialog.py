"""补丁自然语言审批 + 雨云结果通知（2026-10-30）。

背景（生产实证）：
  · 补丁 ea1d75f52a 只能用 !anima iterate approve <id> 批准；
    owner 收到「补丁写好了」的通知后回一句「批准」没用——那句话走正常对话管线。
  · 09:00:17 自动签到配额用尽 → 09:00:18 仍提醒 owner 手动签到，
    因为两条逻辑互不知道对方跑没跑。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from anima.agent import patch_dialog as pd


class _Task:
    def __init__(self, tid="ea1d75f52a", kind="core_patch",
                 status="awaiting_approval", proposal=None, updated_at=100.0):
        self.id = tid
        self.kind = kind
        self.status = status
        self.proposal = proposal or {}
        self.updated_at = updated_at


class _Tasks:
    def __init__(self, items):
        self.items = {t.id: t for t in items}
        self.rejected = []

    def reject(self, tid, reviewer, reason=""):
        t = self.items[tid]
        if t.status != "awaiting_approval":
            raise ValueError("状态不允许驳回")
        t.status = "rejected"
        self.rejected.append((tid, reviewer))
        return t


class _Perms:
    owners = [1562290139]


class _Cfg:
    permissions = _Perms()


class _CI:
    def __init__(self):
        self.approved = []
        self.applied = []

    def approve_core_patch(self, core, tid, reviewer):
        core.tasks.items[tid].status = "approved_pending_apply"
        self.approved.append(tid)
        return core.tasks.items[tid]

    def apply_core_patch(self, core, tid, reviewer, restart=False):
        self.applied.append(tid)
        return f"已落地 {tid}"


def _core(items=None, ci=None):
    core = SimpleNamespace(
        config=_Cfg(),
        tasks=_Tasks(items or []),
        _ci=ci or _CI(),
    )
    return core


@pytest.fixture(autouse=True)
def _patch_ci(monkeypatch):
    """把 get_controlled_iteration 换成取 core 上的桩。"""
    import anima.cognitive.controlled_iteration as mod
    monkeypatch.setattr(mod, "get_controlled_iteration", lambda core: core._ci)


def test_non_owner_ignored():
    core = _core([_Task()])
    assert pd.handle_private(core, "批准", 999) is None


def test_no_pending_returns_none():
    core = _core([])
    assert pd.handle_private(core, "批准", 1562290139) is None


def test_approve_latest_pending():
    core = _core([_Task()])
    out = pd.handle_private(core, "批准", 1562290139)
    assert out is not None
    assert "ea1d75f52a" in out
    assert core._ci.approved == ["ea1d75f52a"]


def test_reject():
    core = _core([_Task()])
    out = pd.handle_private(core, "驳回", 1562290139)
    assert out is not None
    assert core.tasks.items["ea1d75f52a"].status == "rejected"


def test_apply_requires_approval_first():
    core = _core([_Task(status="awaiting_approval")])
    out = pd.handle_private(core, "落地", 1562290139)
    assert out is not None
    assert "还没批准" in out
    assert core._ci.applied == []


def test_apply_after_approve():
    core = _core([_Task(status="approved_pending_apply")])
    out = pd.handle_private(core, "落地", 1562290139)
    assert out is not None
    assert core._ci.applied == ["ea1d75f52a"]


def test_view_does_not_change_state():
    core = _core([_Task()])
    out = pd.handle_private(core, "看看", 1562290139)
    assert out is not None
    assert core.tasks.items["ea1d75f52a"].status == "awaiting_approval"
    assert core._ci.approved == []


def test_picks_latest_when_multiple():
    a = _Task(tid="aaaa11111111", updated_at=100.0)
    b = _Task(tid="bbbb22222222", updated_at=200.0)
    core = _core([a, b])
    pd.handle_private(core, "批准", 1562290139)
    assert core._ci.approved == ["bbbb22222222"]


def test_long_text_is_not_a_command():
    """长句多半在讨论别的事，不该被当成审批。"""
    core = _core([_Task()])
    out = pd.handle_private(core, "我觉得这个补丁改动太小了，先别急着上", 1562290139)
    assert out is None
    assert core._ci.approved == []


def test_only_core_patch_counts():
    """故障工单（fault_ticket）不该被「批准」误伤。"""
    core = _core([_Task(kind="fault_ticket")])
    assert pd.handle_private(core, "批准", 1562290139) is None
