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
