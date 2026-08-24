# -*- coding: utf-8 -*-
"""EvidenceJury (G4) tests —— 客观晋升尺 + 接线入口。"""
from anima.evolution.jury import judge, JuryPolicy, verdict_for_report, verdict_for_metrics_pair


class _Ev:
    """模拟 evaluator 的 ImprovementEvidence(dataclass/attr 读)。"""
    def __init__(self, metric_key, before, after):
        self.metric_key = metric_key; self.before = before; self.after = after; self.delta = after - before


def test_no_evidence_degraded():
    v = judge([])
    assert v.decision == "degraded"
    assert v.sample_n == 0


def test_unknown_metric_no_delta_degraded():
    v = judge([{"metric_key": "mystery"}])
    assert v.decision == "degraded"


def test_regression_suggests_rollback():
    v = judge([{"metric_key": "llm_failure_rate", "before": 1.0, "after": 8.0}])
    assert v.decision == "rollback_suggest"


def test_improvement_below_promote_keeps():
    v = judge([{"metric_key": "test_pass_rate", "before": 50.0, "after": 51.5}])
    assert v.decision == "keep"


def test_big_improvement_no_regress_promotes():
    v = judge([{"metric_key": "test_pass_rate", "before": 50.0, "after": 60.0}])
    assert v.decision == "promote"


def test_lower_better_direction():
    v = judge([{"metric_key": "llm_failure_rate", "before": 8.0, "after": 2.0}])
    assert v.decision == "promote"


def test_regression_beats_improvement():
    v = judge([
        {"metric_key": "test_pass_rate", "before": 50.0, "after": 60.0},
        {"metric_key": "llm_failure_rate", "before": 1.0, "after": 7.0},
    ])
    assert v.decision == "rollback_suggest"


def test_honest_degraded_when_flat():
    v = judge([{"metric_key": "test_pass_rate", "before": 50.0, "after": 49.9}])
    assert v.decision == "degraded"


def test_verdict_for_report_dict_shape():
    v = verdict_for_report({"ok": True, "evidence": [{"metric_key": "test_pass_rate", "before": 50.0, "after": 60.0}]})
    assert v["decision"] == "promote"


def test_verdict_for_report_no_evidence():
    v = verdict_for_report({"ok": True, "evidence": []})
    assert v["decision"] == "degraded"


def test_verdict_for_metrics_pair():
    v = verdict_for_metrics_pair({"llm_failure_rate": 8.0}, {"llm_failure_rate": 2.0})
    assert v["decision"] == "promote"


def test_judge_accepts_dataclass_like():
    v = judge([_Ev("test_pass_rate", 50.0, 60.0)])
    assert v.decision == "promote"


def test_policy_threshold_tuning():
    p = JuryPolicy(min_improve_pct=10.0, promote_pct=30.0)
    v = judge([{"metric_key": "test_pass_rate", "before": 50.0, "after": 52.0}], p)
    assert v.decision == "degraded"
