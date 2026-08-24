# -*- coding: utf-8 -*-
"""EvidenceJury (G4) tests —— 客观晋升尺。"""
from anima.evolution.jury import judge, JuryPolicy


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
