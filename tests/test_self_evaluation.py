# -*- coding: utf-8 -*-
"""R18 —— Self Evaluation 聚合单元测试（确定性，信号源全部 mock）。

验证 build_self_evaluation：
- 6 轴（runtime / observation / stability / safety / evolution / interaction）正确评分；
- 信号缺失 -> 该轴 unknown，绝不编造分数；
- 诚实推荐：GitHub 缺失给出 ANIMA_GITHUB_TOKEN 建议；
- HumanAIControlSurface.self_evaluation 在 core 缺失时 fail-closed。
"""
from __future__ import annotations

from unittest import mock

from anima.observation.self_evaluation import (
    SelfEvaluation,
    _score_evolution,
    _score_observation,
    _score_runtime,
    _score_safety,
    _score_stability,
    build_self_evaluation,
)


# ----------------------------------------------------------------------
# 单轴评分
# ----------------------------------------------------------------------

def test_score_runtime_healthy():
    snap = {"snapshot": {"runtime_running": 1.0, "channel_connected": 1.0,
                         "llm_failure_rate": 0.0, "automation_pending": 0.0},
            "degraded": False}
    ax = _score_runtime(snap)
    assert ax.axis == "runtime"
    assert ax.score == 0.8
    assert ax.status == "ok"


def test_score_runtime_unknown_when_empty():
    ax = _score_runtime({})
    assert ax.score is None
    assert ax.status == "unknown"


def test_score_runtime_attention_on_degraded():
    snap = {"snapshot": {"runtime_running": 1.0, "channel_connected": 1.0},
            "degraded": True}
    ax = _score_runtime(snap)
    assert ax.status == "attention"


def test_score_observation_ok():
    rep = {"sources_available": ["performance", "audit"],
           "sources_missing": [], "incomplete": False}
    ax = _score_observation(rep)
    assert ax.score == 1.0
    assert ax.status == "ok"


def test_score_observation_attention_on_missing():
    rep = {"sources_available": [], "sources_missing": ["github"],
           "incomplete": True}
    ax = _score_observation(rep)
    assert ax.status == "attention"
    assert ax.evidence["missing"] == ["github"]


def test_score_observation_unknown_when_no_report():
    ax = _score_observation({})
    assert ax.score is None
    assert ax.status == "unknown"


def test_score_stability_ok():
    win = {"90d": {"uptime_ratio": 0.99, "anomaly_total": 0,
                   "permission_drift_count": 0}}
    ax = _score_stability(win)
    assert ax.status == "ok"
    assert ax.score >= 0.95


def test_score_stability_attention_on_anomaly():
    win = {"30d": {"uptime_ratio": 0.99, "anomaly_total": 5,
                   "permission_drift_count": 0}}
    ax = _score_stability(win)
    assert ax.status == "attention"


def test_score_stability_unknown_when_empty():
    ax = _score_stability({})
    assert ax.score is None
    assert ax.status == "unknown"


def test_score_safety_ok():
    ax = _score_safety({"available": True, "growing": False, "current": 1},
                       {"available": True, "drift_count": 0})
    assert ax.status == "ok"
    assert ax.score == 1.0


def test_score_safety_attention_when_deny_growing():
    ax = _score_safety({"available": True, "growing": True, "current": 10},
                       {"available": True, "drift_count": 0})
    assert ax.status == "attention"
    assert ax.score < 1.0


def test_score_safety_unknown_when_no_signal():
    ax = _score_safety({}, {})
    assert ax.score is None
    assert ax.status == "unknown"


def test_score_evolution_ok():
    ax = _score_evolution({"unapproved_self_mod_count": 0,
                           "pending_owner_count": 2})
    assert ax.status == "ok"
    assert ax.score == 1.0


def test_score_evolution_attention_on_unapproved():
    ax = _score_evolution({"unapproved_self_mod_count": 2,
                           "pending_owner_count": 0})
    assert ax.status == "attention"
    assert ax.score < 1.0


def test_score_evolution_unknown_when_empty():
    ax = _score_evolution({})
    assert ax.score is None
    assert ax.status == "unknown"


# ----------------------------------------------------------------------
# 聚合
# ----------------------------------------------------------------------

def _full_signals():
    return {
        "runtime_snapshot": {
            "snapshot": {"runtime_running": 1.0, "channel_connected": 1.0,
                         "llm_failure_rate": 0.0, "automation_pending": 0.0},
            "degraded": False,
        },
        "environment_report": {
            "sources_available": ["performance", "audit"],
            "sources_missing": [], "incomplete": False,
        },
        "stability": {"90d": {"uptime_ratio": 0.99, "anomaly_total": 0,
                              "permission_drift_count": 0}},
        "audit_deny_trend": {"available": True, "growing": False, "current": 0},
        "config_drift": {"available": True, "drift_count": 0},
        "evolution": {"unapproved_self_mod_count": 0, "pending_owner_count": 0},
        "interaction": {
            "conversation": {"ok_ratio": 0.9},
            "tools": {"success_rate": 0.88},
            "persona": {"stability": 1.0},
            "image": {"delivery_ok_ratio": 0.95},
        },
    }


def test_build_self_evaluation_all_ok():
    ev = build_self_evaluation(**_full_signals())
    assert isinstance(ev, SelfEvaluation)
    assert len(ev.axes) == 6  # runtime/observation/stability/safety/evolution/interaction
    assert ev.overall is not None and ev.overall >= 0.9
    assert ev.attention == []
    assert ev.recommendations == []


def test_build_self_evaluation_uses_now_override():
    ev = build_self_evaluation(**_full_signals(), now=1234.5)
    assert ev.generated_at == 1234.5


def test_build_self_evaluation_honest_github_missing_recommendation():
    sig = _full_signals()
    sig["environment_report"] = {"sources_available": ["performance"],
                                 "sources_missing": ["github"],
                                 "incomplete": True}
    sig["stability"] = {}
    sig["audit_deny_trend"] = {}
    sig["config_drift"] = {}
    ev = build_self_evaluation(**sig)
    axes = {a.axis: a for a in ev.axes}
    assert axes["observation"].status == "attention"
    assert axes["stability"].status == "unknown"
    assert axes["safety"].status == "unknown"
    recs = " ".join(ev.recommendations)
    assert "ANIMA_GITHUB_TOKEN" in recs
    assert "stability" in recs


def test_build_self_evaluation_no_known_axes_overall_none():
    ev = build_self_evaluation(
        runtime_snapshot={}, environment_report={}, stability={},
        audit_deny_trend={}, config_drift={}, evolution={})
    assert ev.overall is None
    assert all(a.score is None for a in ev.axes)


# ----------------------------------------------------------------------
# 控制面集成（fail-closed / 不自我授权）
# ----------------------------------------------------------------------

def test_control_surface_self_evaluation_core_missing():
    from anima.os.control_surface import HumanAIControlSurface
    surf = HumanAIControlSurface(None)
    out = surf.self_evaluation()
    assert out.get("available") is False
    assert "core" in out.get("reason", "")


def test_control_surface_self_evaluation_read_only_no_execution():
    from anima.os.control_surface import HumanAIControlSurface
    core = mock.Mock()
    core.audit = None
    surf = HumanAIControlSurface(core)
    with mock.patch.object(
        surf, "environment_report",
        return_value={"sources_available": ["performance"],
                      "sources_missing": [], "incomplete": False},
    ), mock.patch.object(
        surf, "_snapshot_health",
        return_value={"snapshot": {"runtime_running": 1.0,
                                   "channel_connected": 1.0},
                      "degraded": False},
    ), mock.patch.object(
        surf, "stability_summary",
        return_value={"uptime_ratio": 0.99, "anomaly_total": 0,
                      "permission_drift_count": 0},
    ), mock.patch(
        "anima.observation.environment.audit_deny_growth",
        return_value={"available": True, "growing": False},
    ), mock.patch(
        "anima.agent.self_evolution.list_evolution_proposals",
        return_value=[],
    ):
        out = surf.self_evaluation()
    assert out.get("overall") is not None
    assert out.get("axes") is not None
    axes = {a["axis"]: a for a in out["axes"]}
    # 查询成功（空列表=良好纪律）-> evolution 为 ok，不应编造 unknown；
    # 真正 unknown 发生在查询本身抛错（见下）。
    assert axes["evolution"]["status"] == "ok"
    # 整体只陈述、不执行：无 recommendations 触发任何写操作。
    assert isinstance(out.get("recommendations"), list)


def test_control_surface_self_evaluation_evolution_unknown_on_query_failure():
    from anima.os.control_surface import HumanAIControlSurface
    core = mock.Mock()
    core.audit = None
    surf = HumanAIControlSurface(core)
    with mock.patch.object(
        surf, "environment_report",
        return_value={"sources_available": ["performance"],
                      "sources_missing": [], "incomplete": False},
    ), mock.patch.object(
        surf, "_snapshot_health",
        return_value={"snapshot": {"runtime_running": 1.0,
                                   "channel_connected": 1.0},
                      "degraded": False},
    ), mock.patch.object(
        surf, "stability_summary",
        return_value={"uptime_ratio": 0.99, "anomaly_total": 0,
                      "permission_drift_count": 0},
    ), mock.patch(
        "anima.observation.environment.audit_deny_growth",
        return_value={"available": True, "growing": False},
    ), mock.patch(
        "anima.agent.self_evolution.list_evolution_proposals",
        side_effect=RuntimeError("db unavailable"),
    ):
        out = surf.self_evaluation()
    axes = {a["axis"]: a for a in out["axes"]}
    # 查询抛错 -> 信号缺失 -> 诚实标 unknown。
    assert axes["evolution"]["status"] == "unknown"
