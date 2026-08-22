# -*- coding: utf-8 -*-
"""P5 —— _score_interaction / build_self_evaluation interaction 轴向测试。

验证 P5「交互质量评估」轴（对话质量/工具成功率/人格一致性/图片交付）：
- 完整四源给出聚合分数 + ok；
- 部分源缺失 -> attention（诚实 fail-closed，不编造缺失源分数）；
- 全部缺失 -> unknown；
- 接入 build_self_evaluation 后整体自评包含 interaction 轴。
"""
from __future__ import annotations

from anima.observation.self_evaluation import build_self_evaluation
from anima.observation.self_evaluation import _score_interaction


def test_interaction_full_ok():
    axis = _score_interaction({
        "conversation": {"ok_ratio": 0.9, "samples": 120},
        "tools": {"success_rate": 0.88, "calls": 50},
        "persona": {"stability": 1.0, "drift_rejects": 0},
        "image": {"delivery_ok_ratio": 0.95, "samples": 20},
    })
    assert axis.axis == "interaction"
    assert axis.status == "ok"
    assert axis.score is not None and axis.score >= 0.7
    ev = axis.evidence
    assert ev["conversation"]["value"] == 0.9
    assert ev["tools"]["value"] == 0.88
    assert ev["persona"]["value"] == 1.0
    assert ev["image"]["value"] == 0.95


def test_interaction_partial_is_attention_not_unknown():
    # 只有对话+工具两源 -> 有分数但 status=attention（诚实标注不完整）
    axis = _score_interaction({
        "conversation": {"ok_ratio": 0.85},
        "tools": {"success_rate": 0.8},
    })
    assert axis.status == "attention"
    assert axis.score is not None
    # 缺失源在 evidence 标 available:False，不编造
    assert axis.evidence["persona"]["available"] is False
    assert axis.evidence["image"]["available"] is False


def test_interaction_empty_is_unknown():
    assert _score_interaction({}).status == "unknown"
    assert _score_interaction({}).score is None
    # 空子字典也不产生分数
    axis = _score_interaction({"conversation": {}})
    assert axis.status == "unknown"


def test_interaction_low_score_is_attention():
    axis = _score_interaction({
        "conversation": {"ok_ratio": 0.5},
        "tools": {"success_rate": 0.4},
        "persona": {"stability": 0.9},
        "image": {"delivery_ok_ratio": 0.6},
    })
    assert axis.status == "attention"
    assert axis.score is not None and axis.score < 0.7


def test_build_self_evaluation_includes_interaction_axis():
    ev = build_self_evaluation(interaction={
        "conversation": {"ok_ratio": 0.9},
        "tools": {"success_rate": 0.87},
        "persona": {"stability": 1.0},
        "image": {"delivery_ok_ratio": 0.94},
    })
    axes = {a.axis for a in ev.axes}
    assert "interaction" in axes


def test_build_self_evaluation_without_interaction_no_crash():
    # 旧调用方不传 interaction -> interaction 轴 unknown，但整体不崩
    ev = build_self_evaluation()
    assert ev.overall is not None or ev.axes
    iax = next((a for a in ev.axes if a.axis == "interaction"), None)
    assert iax is not None and iax.status == "unknown"


def _mk_core():
    class P:
        data_dir = "data"
    class Cfg:
        persistence = P()
    class Core:
        config = Cfg()
    return Core()


def test_control_surface_interaction_summary_mocks():
    from anima.os.control_surface import HumanAIControlSurface
    from unittest import mock
    surf = HumanAIControlSurface(_mk_core())
    with mock.patch("anima.cognitive.reply_quality.get_reply_quality") as gq, \
         mock.patch("anima.media.prepare_cache_stats") as pcs:
        gq.return_value.report.return_value = {
            "total_replies": 100,
            "overlong_replies": 2, "label_leaks": 0, "identity_leaks": 1,
            "repeated_replies": 1, "empty_should_reply": 1, "unavailable_replies": 1,
            "tool_calls": 50, "tool_failures": 5,
        }
        pcs.return_value = {"hits": 90, "misses": 10}
        out = surf._interaction_summary()
    assert out["conversation"]["ok_ratio"] == 0.94
    assert out["conversation"]["samples"] == 100
    assert out["tools"]["success_rate"] == 0.9
    assert out["persona"]["stability"] == 0.99
    assert out["image"]["delivery_ok_ratio"] == 0.9


def test_control_surface_interaction_summary_fail_closed():
    from anima.os.control_surface import HumanAIControlSurface
    from unittest import mock
    surf = HumanAIControlSurface(_mk_core())
    with mock.patch("anima.cognitive.reply_quality.get_reply_quality",
                    side_effect=RuntimeError("boom")), \
         mock.patch("anima.media.prepare_cache_stats",
                    side_effect=RuntimeError("boom")):
        out = surf._interaction_summary()
    assert out == {}


def test_control_surface_self_evaluation_includes_interaction():
    from anima.os.control_surface import HumanAIControlSurface
    from unittest import mock
    surf = HumanAIControlSurface(_mk_core())
    with mock.patch.object(surf, "_snapshot_health",
                           return_value={"snapshot": {"runtime_running": 1.0,
                                                       "channel_connected": 1.0},
                                          "degraded": False}), \
         mock.patch.object(surf, "environment_report",
                           return_value={"sources_available": ["performance", "audit"],
                                         "sources_missing": [], "incomplete": False}), \
         mock.patch.object(surf, "stability_summary",
                           return_value={"uptime_ratio": 0.99, "anomaly_total": 0,
                                         "permission_drift_count": 0}), \
         mock.patch("anima.observation.environment.audit_deny_growth",
                    return_value={"available": True, "growing": False}), \
         mock.patch("anima.cognitive.reply_quality.get_reply_quality",
                    side_effect=RuntimeError("no data")):
        out = surf.self_evaluation()
    axes = {a["axis"]: a for a in out["axes"]}
    assert "interaction" in axes
