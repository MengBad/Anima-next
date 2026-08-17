"""P2 — Evolution Impact Score 单元测试（确定性，无 LLM）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from anima.evolution import impact as imp


def _write_corpus(data_dir: Path, rows: list[dict]) -> None:
    p = Path(data_dir) / "evolution_evidence.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _row(proposal_id: str, evs: list[dict]) -> dict:
    return {
        "proposal_id": proposal_id,
        "report": {
            "ok": True,
            "evidence": evs,
            "summary": {"overall": "effective", "effective": len(evs),
                        "harmful": 0, "ineffective": 0, "total": len(evs)},
        },
    }


# 一个「有效」证据：test_pass_rate 0.8 -> 0.9（higher better，改善）
EFF_TEST = {"metric_key": "test_pass_rate", "before": 0.8, "after": 0.9,
            "delta": 0.1, "direction": "improved", "verdict": "effective"}
# 一个「有害」证据：llm_failure_rate 0.1 -> 0.4（lower better，但升高 = 有害）
HARM_RATE = {"metric_key": "llm_failure_rate", "before": 0.1, "after": 0.4,
             "delta": 0.3, "direction": "regressed", "verdict": "harmful"}


# ---------------------------------------------------------------------------
# 1. 空语料 → 诚实 has_data=False，score=0
# ---------------------------------------------------------------------------
def test_empty_corpus_honest(tmp_path):
    report = imp.build_impact_report(str(tmp_path / "data"))
    assert report.has_data is False
    assert report.overall_score == 0.0
    assert report.n_changes == 0


# ---------------------------------------------------------------------------
# 2. 语料聚合：per-metric 计数 + 相对改善
# ---------------------------------------------------------------------------
def test_metric_aggregation(tmp_path):
    data = tmp_path / "data"
    _write_corpus(data, [
        _row("p1", [EFF_TEST]),
        _row("p2", [EFF_TEST]),
        _row("p3", [EFF_TEST]),
    ])
    report = imp.build_impact_report(str(data))
    assert report.has_data is True
    by_key = {m.metric_key: m for m in report.metrics}
    assert "test_pass_rate" in by_key
    m = by_key["test_pass_rate"]
    assert m.n == 3
    assert m.effective == 3
    assert m.success_rate == pytest.approx(1.0)
    # 0.8 -> 0.9 相对 +12.5%
    assert m.mean_relative_pct == pytest.approx(12.5, abs=0.5)


# ---------------------------------------------------------------------------
# 3. 有害指标计入 → overall_score 受惩罚
# ---------------------------------------------------------------------------
def test_harmful_lowers_score(tmp_path):
    data = tmp_path / "data"
    # 全部有害：整体 score 应为 0（有效占比 0 - 0.5*有害占比1 = -0.5 → clamp 0）
    _write_corpus(data, [_row("p1", [HARM_RATE]), _row("p2", [HARM_RATE])])
    report = imp.build_impact_report(str(data))
    assert report.has_data is True
    assert report.overall_score == 0.0
    assert report.top_harmful == ["llm_failure_rate"]
    assert any("警惕" in s for s in report.suggestions)


# ---------------------------------------------------------------------------
# 4. 混合语料：建议引擎给出「优先优化有效指标」
# ---------------------------------------------------------------------------
def test_mixed_corpus_suggestions(tmp_path):
    data = tmp_path / "data"
    _write_corpus(data, [
        _row("p1", [EFF_TEST, EFF_TEST]),
        _row("p2", [HARM_RATE]),
    ])
    report = imp.build_impact_report(str(data))
    assert report.has_data is True
    assert report.top_effective == ["test_pass_rate"]
    assert any("优先" in s for s in report.suggestions)
    # 有效 2 次 / 总 3 次 → score ~ (2/3) - 0.5*(1/3) ≈ 0.5
    assert report.overall_score > 0.0


# ---------------------------------------------------------------------------
# 5. score_for_proposal：数据驱动支撑
# ---------------------------------------------------------------------------
def test_score_for_proposal_supported(tmp_path):
    data = tmp_path / "data"
    _write_corpus(data, [_row("p1", [EFF_TEST]), _row("p2", [EFF_TEST])])
    report = imp.build_impact_report(str(data))
    sc = imp.score_for_proposal(report, ["test_pass_rate"])
    assert sc["has_data"] is True
    assert sc["score"] == pytest.approx(1.0)
    assert sc["support"] == "historical_evidence"


def test_score_for_proposal_no_match(tmp_path):
    data = tmp_path / "data"
    _write_corpus(data, [_row("p1", [EFF_TEST])])
    report = imp.build_impact_report(str(data))
    sc = imp.score_for_proposal(report, ["nonexistent_metric"])
    assert sc["has_data"] is True
    assert sc["support"] == "no_matching_metric_in_history"


def test_score_for_proposal_no_data(tmp_path):
    report = imp.build_impact_report(str(tmp_path / "data"))
    sc = imp.score_for_proposal(report, ["test_pass_rate"])
    assert sc["has_data"] is False
    assert sc["support"] == "no_historical_evidence"


# ---------------------------------------------------------------------------
# 6. 坏行隔离：损坏行不阻断解析
# ---------------------------------------------------------------------------
def test_corpus_bad_line_isolated(tmp_path):
    data = tmp_path / "data"
    p = Path(data) / "evolution_evidence.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(_row("p1", [EFF_TEST])) + "\n" +
        "this is not json\n" +
        json.dumps(_row("p2", [EFF_TEST])) + "\n",
        encoding="utf-8",
    )
    report = imp.build_impact_report(str(data))
    assert report.has_data is True
    assert report.n_changes == 2
