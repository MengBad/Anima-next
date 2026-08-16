"""R13 Autonomous Operation 测试：运行时健康与受控恢复。

验证 fail-closed 健康评估、各降级场景的恢复计划、需 Owner 授权标记、
信号缺失 → critical、以及桥接 R8 DevIssue。纯确定性、无 LLM、无真实重启。
"""

import pytest

from anima.agent.autonomous_operation import (
    AutonomousOperationController,
    HealthRecoveryPlanner,
    ISSUE_SOURCE_RECOVERY,
    RecoveryPlan,
)


def _snap(running=1.0, connected=1.0, fail_rate=0.0, pending=0.0):
    return {
        "runtime_running": running,
        "channel_connected": connected,
        "llm_failure_rate": fail_rate,
        "automation_pending": pending,
    }


def test_healthy_no_plan():
    c = AutonomousOperationController()
    rep = c.evaluate(snapshot=_snap())
    assert rep.degraded is False
    assert rep.plans == []


def test_runtime_down_critical_requires_owner():
    c = AutonomousOperationController()
    rep = c.evaluate(snapshot=_snap(running=0.0))
    actions = {p.action for p in rep.plans}
    assert "restart_runtime" in actions
    p = next(p for p in rep.plans if p.action == "restart_runtime")
    assert p.severity == "critical"
    assert p.requires_owner is True


def test_channel_down_high_requires_owner():
    c = AutonomousOperationController()
    rep = c.evaluate(snapshot=_snap(connected=0.0))
    p = next(p for p in rep.plans if p.action == "reconnect_channel")
    assert p.severity == "high"
    assert p.requires_owner is True


def test_high_llm_fail_rate_review():
    c = AutonomousOperationController()
    rep = c.evaluate(snapshot=_snap(fail_rate=0.6))
    p = next(p for p in rep.plans if p.action == "review_llm")
    assert p.severity == "high"
    assert p.requires_owner is True


def test_automation_backlog_throttle_no_owner():
    c = AutonomousOperationController()
    rep = c.evaluate(snapshot=_snap(pending=80))
    p = next(p for p in rep.plans if p.action == "throttle_automation")
    assert p.severity == "normal"
    assert p.requires_owner is False  # 节流是咨询类，不触生产


def test_missing_signal_fail_closed_critical():
    # 空快照（采集失败）→ 视为 runtime=0 → critical 且需 Owner
    c = AutonomousOperationController()
    rep = c.evaluate(snapshot={})
    assert rep.snapshot["runtime_running"] == 0.0
    assert any(p.severity == "critical" and p.requires_owner for p in rep.plans)


def test_planner_deterministic():
    planner = HealthRecoveryPlanner()
    a = planner.plan(_snap(running=0.0, connected=0.0, fail_rate=0.6, pending=80))
    b = planner.plan(_snap(running=0.0, connected=0.0, fail_rate=0.6, pending=80))
    assert [p.action for p in a] == [p.action for p in b]
    assert len(a) == 4  # 四类计划全部触发


def test_to_dev_issues_bridges_r8():
    c = AutonomousOperationController()
    rep = c.evaluate(snapshot=_snap(running=0.0))
    issues = c.to_dev_issues(rep.plans)
    assert len(issues) == len(rep.plans)
    for i in issues:
        assert i.source == ISSUE_SOURCE_RECOVERY
        assert i.title.startswith("[自治运维]")


def test_recovery_plan_serializable():
    p = RecoveryPlan(action="restart_runtime", severity="critical",
                     requires_owner=True, rationale="r", evidence={"x": 1})
    d = p.to_dict()
    assert d["action"] == "restart_runtime"
    assert d["requires_owner"] is True
    assert d["evidence"]["x"] == 1
