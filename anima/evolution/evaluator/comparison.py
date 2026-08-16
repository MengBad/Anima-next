"""R10-02 演化评估引擎 —— Before/After 对比与改进证据。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ImprovementEvidence:
    """一次演化改进的可验证证据：优化前 vs 优化后，是否真的更好。"""

    metric_key: str
    before: float
    after: float
    delta: float = 0.0
    direction: str = "neutral"  # improved | regressed | neutral
    verdict: str = "unknown"     # effective | ineffective | harmful
    captured_at: float = field(default_factory=time.time)
    evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric_key": self.metric_key,
            "before": self.before,
            "after": self.after,
            "delta": self.delta,
            "direction": self.direction,
            "verdict": self.verdict,
            "captured_at": self.captured_at,
            "evidence": self.evidence,
        }


# 指标语义：值为「越高越好」(higher_better) 或「越低越好」(lower_better)
_HIGHER_BETTER = {
    "runtime_running", "channel_connected", "health_samples",
    "llm_request_count", "test_pass_rate",
}
_LOWER_BETTER = {
    "llm_failure_count", "llm_failure_rate", "automation_pending",
}


def _direction(metric_key: str, before: float, after: float) -> str:
    if before == after:
        return "neutral"
    if metric_key in _HIGHER_BETTER:
        higher_better = True
    elif metric_key in _LOWER_BETTER:
        higher_better = False
    else:
        higher_better = True  # 默认越高越好
    if higher_better:
        return "improved" if after > before else "regressed"
    return "improved" if after < before else "regressed"


def compare_metric(metric_key: str, before: float, after: float,
                  *, epsilon: float = 1e-9) -> ImprovementEvidence:
    """对比单个指标，产出改进证据。"""
    delta = after - before
    direction = _direction(metric_key, before, after)
    if direction == "neutral":
        verdict = "ineffective"
    elif direction == "improved":
        verdict = "effective"
    else:
        verdict = "harmful"
    return ImprovementEvidence(
        metric_key=metric_key, before=before, after=after, delta=delta,
        direction=direction, verdict=verdict,
        evidence=f"{metric_key}: {before} -> {after} (Δ{delta:+.4f}, {direction})",
    )


def compare_samples(before: dict[str, float], after: dict[str, float]) -> list[ImprovementEvidence]:
    """对比两组指标快照，逐键产出改进证据（仅对比共有键）。"""
    out: list[ImprovementEvidence] = []
    for key in before:
        if key not in after:
            continue
        out.append(compare_metric(key, float(before[key]), float(after[key])))
    return out


def summarize_verdict(evidence: list[ImprovementEvidence]) -> dict[str, Any]:
    """聚合多个指标证据，给出整体结论。"""
    effective = sum(1 for e in evidence if e.verdict == "effective")
    harmful = sum(1 for e in evidence if e.verdict == "harmful")
    ineffective = sum(1 for e in evidence if e.verdict == "ineffective")
    if harmful > 0:
        overall = "harmful"
    elif effective > 0:
        overall = "effective"
    else:
        overall = "ineffective"
    return {
        "overall": overall,
        "effective": effective,
        "harmful": harmful,
        "ineffective": ineffective,
        "total": len(evidence),
    }
