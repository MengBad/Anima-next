"""EvidenceJury (G4) — 客观晋升尺 for 自进化闭环.

以 ImprovementEvidence(逐次 Before/After 指标, 方向归一 higher/lower better)
为唯一依据做「保留 / 晋级 / 回滚」判定, 而非 LLM 自夸或 CI 绿。

纪律(与 anima/evolution/impact.py 对齐):
- fail-closed: 无证据 / 证据不足 -> degraded(诚实), 绝不晋级。
- 回归闸: 任一关键指标显著退化 -> rollback_suggest(仍须 Owner 确认)。
- 改善达阈值且无回归 -> keep / promote。
- 只读: 不改运行态; 判定供 Owner 做最终闸。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

_HIGHER_BETTER = frozenset({
    "runtime_running", "channel_connected", "health_samples",
    "llm_request_count", "test_pass_rate",
})
_LOWER_BETTER = frozenset({
    "llm_failure_count", "llm_failure_rate", "automation_pending",
})


@dataclass(frozen=True)
class JuryPolicy:
    """可调阈值。regression_tol 为归一后改善%的下界(负=退化)。"""
    min_improve_pct: float = 1.0      # 归一改善 ≥ 此% 记为「有改善」
    promote_pct: float = 5.0          # 归一改善 ≥ 此% 且无回归 -> promote
    regression_tol: float = -1.0      # 归一改善 ≤ 此%(即退化≥1%) -> regressed
    directions: Mapping[str, str] = field(default_factory=dict)  # metric->higher|lower 覆盖


DEFAULT_POLICY = JuryPolicy()


@dataclass
class MetricVerdict:
    metric_key: str
    before: float
    after: float
    delta: float
    normalized: float   # 归一改善%(方向已对齐, 正=改善)
    decision: str       # improved|regressed|flat|no_evidence

    def to_dict(self) -> dict:
        return {
            "metric_key": self.metric_key, "before": self.before, "after": self.after,
            "delta": self.delta, "normalized": self.normalized, "decision": self.decision,
        }


@dataclass
class EvidenceVerdict:
    """判定结果: promote | keep | rollback_suggest | degraded (fail-closed)。"""
    decision: str
    reason: str
    metrics: list = field(default_factory=list)
    sample_n: int = 0

    def to_dict(self) -> dict:
        return {
            "decision": self.decision, "reason": self.reason, "sample_n": self.sample_n,
            "metrics": [m.to_dict() for m in self.metrics],
        }


def _direction(key: str, policy: JuryPolicy) -> str:
    over = policy.directions.get(key)
    if over:
        return over
    if key in _HIGHER_BETTER:
        return "higher"
    if key in _LOWER_BETTER:
        return "lower"
    return "higher"  # 未知默认按传入方向处理, 无对齐则 no_evidence


def _get(m: Mapping, key: str, default: Any = None):
    """兼容 dict 与 dataclass(如 ImprovementEvidence) 取字段。"""
    if isinstance(m, Mapping):
        return m.get(key, default)
    return getattr(m, key, default)


def _as_float(v: Any) -> float:
    try:
        return float(v)
    except Exception:
        return float("nan")


def _normalized(before: Any, after: Any, direction: str) -> float:
    b = _as_float(before)
    a = _as_float(after)
    if b != b or a != a or b == 0:
        return float("nan")
    raw = (a - b) / abs(b) * 100.0
    return raw if direction == "higher" else -raw


def judge(metrics, policy: JuryPolicy = DEFAULT_POLICY) -> EvidenceVerdict:
    """输入: 指标列表 [{metric_key,before,after}...]。输出: EvidenceVerdict。"""
    parsed: list = []
    for m in metrics:
        key = str(_get(m, "metric_key") or _get(m, "key") or "metric")
        before = _get(m, "before")
        after = _get(m, "after")
        direction = _direction(key, policy)
        norm = _normalized(before, after, direction)
        if norm != norm:
            parsed.append(MetricVerdict(key, 0.0, 0.0, 0.0, 0.0, "no_evidence"))
            continue
        raw_delta = _as_float(after) - _as_float(before)
        improved = norm >= policy.min_improve_pct
        regressed = norm <= policy.regression_tol
        decision = "improved" if improved else ("regressed" if regressed else "flat")
        parsed.append(MetricVerdict(
            key, _as_float(before), _as_float(after),
            round(raw_delta, 6), round(norm, 3), decision,
        ))
    sample_n = sum(1 for mv in parsed if mv.decision != "no_evidence")
    if sample_n == 0:
        return EvidenceVerdict("degraded", "无有效 Before/After 证据, fail-closed 诚实降级, 不晋级", parsed, sample_n)
    regressed_names = [mv.metric_key for mv in parsed if mv.decision == "regressed"]
    if regressed_names:
        reason = "关键指标回归(" + ",".join(regressed_names) + ") → 建议回滚(仍须 Owner 确认)"
        return EvidenceVerdict("rollback_suggest", reason, parsed, sample_n)
    best = max((mv for mv in parsed if mv.decision == "improved"), key=lambda x: x.normalized, default=None)
    if best is not None and best.normalized >= policy.promote_pct:
        reason = "改善达晋升阈(" + best.metric_key + " +" + str(best.normalized) + "%) 且无回归 → 建议晋级"
        return EvidenceVerdict("promote", reason, parsed, sample_n)
    if best is not None:
        return EvidenceVerdict("keep", "有改善但未达晋升阈 → 保留", parsed, sample_n)
    return EvidenceVerdict("degraded", "未见有效改善 → 诚实降级(不升级)", parsed, sample_n)


def verdict_for_report(report, policy: JuryPolicy = DEFAULT_POLICY) -> dict:
    """From Benchmark 报告(dict, 含 evidence:[{metric_key,before,after}...]) 出 EvidenceVerdict dict。"""
    metrics = report.get("evidence") or [] if isinstance(report, dict) else []
    return judge(metrics, policy).to_dict()


def verdict_for_metrics_pair(before, after, policy: JuryPolicy = DEFAULT_POLICY) -> dict:
    """From 两套键值指标 {metric_key: value} -> EvidenceVerdict dict(非 Benchmark 时用)。"""
    metrics = [
        {"metric_key": k, "before": b, "after": after.get(k, b)}
        for k, b in (before or {}).items()
    ]
    return judge(metrics, policy).to_dict()

