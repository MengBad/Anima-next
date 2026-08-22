# -*- coding: utf-8 -*-
"""R20 —— 长期自主运行快照（Long-Term Operation）单元测试。

验证 build_long_term_operation：
- 确定性「继续 / 降级 / 移除」能力判定（阈值驱动，无 LLM）；
- 只建议不执行：downgrade/remove 只在 capability_verdicts / recommendations；
- fail-closed：信号缺失 -> unavailable / unknown，绝不编造；
- HumanAIControlSurface.long_term_operation 在 core 缺失时 fail-closed。
"""
from __future__ import annotations

from unittest import mock

from anima.observation.long_term_operation import (
    LongTermOperation,
    _verdict_runtime,
    build_long_term_operation,
)


def test_verdict_runtime_continue():
    v = _verdict_runtime({"90d": {"uptime_ratio": 0.99, "anomaly_total": 0,
                                  "permission_drift_count": 0}})
    assert v.capability == "runtime"
    assert v.verdict == "continue"


def test_verdict_runtime_downgrade_on_anomaly():
    v = _verdict_runtime({"90d": {"uptime_ratio": 0.99, "anomaly_total": 5,
                                  "permission_drift_count": 0}})
    assert v.verdict == "downgrade"


def test_verdict_runtime_remove_on_high_anomaly():
    v = _verdict_runtime({"90d": {"uptime_ratio": 0.99, "anomaly_total": 12,
                                  "permission_drift_count": 0}})
    assert v.verdict == "remove"


def test_verdict_runtime_downgrade_on_drift():
    v = _verdict_runtime({"90d": {"uptime_ratio": 0.99, "anomaly_total": 0,
                                  "permission_drift_count": 1}})
    assert v.verdict == "downgrade"


def test_verdict_runtime_unknown_when_no_90d():
    v = _verdict_runtime({})
    assert v.verdict == "unknown"


def test_build_long_term_operation_all_continue():
    op = build_long_term_operation(
        stability={"90d": {"uptime_ratio": 0.99, "anomaly_total": 0,
                           "permission_drift_count": 0}},
        observed_count=2, pending_owner_count=0, evidence_count=1, now=1000.0)
    assert isinstance(op, LongTermOperation)
    assert op.generated_at == 1000.0
    assert op.horizon == "ok"
    assert len(op.capability_verdicts) == 3
    assert op.recommendations == []


def test_build_long_term_operation_remove_recommendation():
    op = build_long_term_operation(
        stability={"90d": {"uptime_ratio": 0.99, "anomaly_total": 12,
                           "permission_drift_count": 0}},
        observed_count=2, pending_owner_count=0, evidence_count=0)
    assert op.horizon == "degraded"
    recs = " ".join(op.recommendations)
    assert "runtime" in recs and "remove" in recs
    # 只建议不执行：没有 execute/disable/uninstall 动作字段
    assert "disable" not in recs and "uninstall" not in recs


def test_build_long_term_operation_no_stability_unknown():
    op = build_long_term_operation(stability=None, observed_count=0,
                                   pending_owner_count=0, evidence_count=0)
    assert "stability" in op.unavailable
    runtime = [v for v in op.capability_verdicts if v.capability == "runtime"][0]
    assert runtime.verdict == "unknown"


def test_build_long_term_operation_evolution_downgrade_on_backlog():
    op = build_long_term_operation(
        stability={"90d": {"uptime_ratio": 0.99, "anomaly_total": 0,
                           "permission_drift_count": 0}},
        observed_count=1, pending_owner_count=25, evidence_count=0)
    evo = [v for v in op.capability_verdicts if v.capability == "evolution"][0]
    assert evo.verdict == "downgrade"


# ----------------------------------------------------------------------
# 控制面集成（fail-closed / 不自我授权）
# ----------------------------------------------------------------------

def test_control_surface_long_term_operation_core_missing():
    from anima.os.control_surface import HumanAIControlSurface
    surf = HumanAIControlSurface(None)
    out = surf.long_term_operation()
    assert out.get("available") is False
    assert "core" in out.get("reason", "")


def test_control_surface_long_term_operation_read_only():
    from anima.os.control_surface import HumanAIControlSurface
    core = mock.Mock()
    core.audit = None
    surf = HumanAIControlSurface(core)
    with mock.patch(
        "anima.observation.long_term_operation.build_long_term_operation",
        return_value=LongTermOperation(
            generated_at=1.0, horizon="ok",
            capability_verdicts=[], unavailable=[], recommendations=[],
        ),
    ):
        out = surf.long_term_operation()
    assert out.get("horizon") == "ok"
    # 只陈述、只建议：结果只有状态字段，无任何执行动作。
    assert "execute" not in out and "disable" not in out
