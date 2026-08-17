"""P2 —— Evolution Impact Score（演化影响评分）。

把 R9/R10-02 已落盘的**逐次 Before/After 改进证据**（`data/evolution_evidence.jsonl`）
聚合为**语料级演化影响评分**，让 ANIMA 能说出：

    「我建议优化 X，因为历史数据显示：同类改动在 N 次尝试中把
      metric_key 平均改善 +Δ%（有效 {k} 次 / 有害 {j} 次）。」

而非拍脑袋。这是 P2 计划的核心：优化效果**可量化、可回溯、可建议**。

设计纪律（延续 R6–R15 / P0–P1）：
- **只读**：只聚合既存证据，不写运行态、不触发任何部署。
- **fail-closed / 诚实**：证据语料为空 / 缺失 → ``has_data=False``，评分记 0，
  绝不编造「历史证明有益」。
- **证据可溯源**：每条聚合都带样本数（n），结论标注置信度。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# 指标语义（与 R10-02 comparison 对齐）：越高越好 / 越低越好
_HIGHER_BETTER = {
    "runtime_running", "channel_connected", "health_samples",
    "llm_request_count", "test_pass_rate",
}
_LOWER_BETTER = {
    "llm_failure_count", "llm_failure_rate", "automation_pending",
}


def _is_higher_better(metric_key: str) -> bool:
    if metric_key in _HIGHER_BETTER:
        return True
    if metric_key in _LOWER_BETTER:
        return False
    return True  # 默认越高越好


# ---------------------------------------------------------------------------
# 证据语料读取（坏行隔离）
# ---------------------------------------------------------------------------
def load_evidence_corpus(data_dir: str) -> list[dict[str, Any]]:
    """读取 evolution_evidence.jsonl（每行 {proposal_id, report}），坏行隔离。"""
    path = Path(data_dir) / "evolution_evidence.jsonl"
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and "report" in obj:
                    out.append(obj)
    except OSError:
        return []
    return out


# ---------------------------------------------------------------------------
# 评分模型
# ---------------------------------------------------------------------------
@dataclass
class MetricImpact:
    """单个指标在全部历史演化中的累计影响。"""

    metric_key: str
    n: int = 0
    effective: int = 0
    harmful: int = 0
    ineffective: int = 0
    mean_delta: float = 0.0
    mean_relative_pct: float = 0.0   # 相对 before 的平均改善百分比
    success_rate: float = 0.0        # effective / n

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric_key": self.metric_key,
            "n": self.n,
            "effective": self.effective,
            "harmful": self.harmful,
            "ineffective": self.ineffective,
            "mean_delta": round(self.mean_delta, 6),
            "mean_relative_pct": round(self.mean_relative_pct, 4),
            "success_rate": round(self.success_rate, 4),
        }


@dataclass
class ImpactReport:
    """一次演化影响评分报告（语料级）。"""

    generated_at: float
    has_data: bool
    metrics: list[MetricImpact] = field(default_factory=list)
    overall_score: float = 0.0        # 0..1，加权（有效占比 - 有害惩罚）
    n_changes: int = 0
    top_effective: list[str] = field(default_factory=list)
    top_harmful: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "has_data": self.has_data,
            "overall_score": round(self.overall_score, 4),
            "n_changes": self.n_changes,
            "metrics": [m.to_dict() for m in self.metrics],
            "top_effective": self.top_effective,
            "top_harmful": self.top_harmful,
            "suggestions": self.suggestions,
        }


def compute_impact(corpus: list[dict[str, Any]], *, now: float | None = None) -> ImpactReport:
    """把证据语料聚合为演化影响评分。

    - 按 ``metric_key`` 累计 effective/harmful/ineffective 与平均 Δ。
    - 相对改善百分比：``(after-before)/|before|``，方向按 higher/lower better 归一为「改善为正」。
    - overall_score = clamp(有效占比 - 0.5*有害占比)，0..1；无数据 → 0 且 has_data=False。
    """
    import time
    now = now if now is not None else time.time()

    by_key: dict[str, list[dict[str, Any]]] = {}
    n_changes = 0
    for row in corpus:
        report = row.get("report") or {}
        evidence = report.get("evidence") or []
        if not evidence:
            continue
        n_changes += 1
        for ev in evidence:
            key = ev.get("metric_key")
            if not key:
                continue
            by_key.setdefault(key, []).append(ev)

    if not by_key:
        return ImpactReport(generated_at=now, has_data=False, overall_score=0.0, n_changes=n_changes)

    impacts: list[MetricImpact] = []
    total_eff = total_harm = total_n = 0
    for key, evs in by_key.items():
        n = len(evs)
        eff = sum(1 for e in evs if e.get("verdict") == "effective")
        harm = sum(1 for e in evs if e.get("verdict") == "harmful")
        ineff = sum(1 for e in evs if e.get("verdict") == "ineffective")
        deltas = [float(e.get("delta", 0.0) or 0.0) for e in evs]
        mean_delta = sum(deltas) / n if n else 0.0

        # 相对改善（归一方向）：higher_better → after-before 为正即改善；
        # lower_better → before-after 为正即改善。
        rels: list[float] = []
        for e in evs:
            b = float(e.get("before", 0.0) or 0.0)
            a = float(e.get("after", 0.0) or 0.0)
            denom = abs(b) if b != 0 else 1.0
            raw = (a - b) / denom
            rels.append(raw if _is_higher_better(key) else -raw)
        mean_rel = (sum(rels) / n) * 100.0 if n else 0.0

        impacts.append(MetricImpact(
            metric_key=key, n=n, effective=eff, harmful=harm, ineffective=ineff,
            mean_delta=mean_delta, mean_relative_pct=mean_rel,
            success_rate=(eff / n) if n else 0.0,
        ))
        total_eff += eff
        total_harm += harm
        total_n += n

    overall = (total_eff / total_n) - 0.5 * (total_harm / total_n) if total_n else 0.0
    overall = max(0.0, min(1.0, overall))

    # 取有效/有害 Top（按成功率高/有害多）
    eff_sorted = sorted(impacts, key=lambda m: (-m.success_rate, -m.n))
    harm_sorted = sorted(impacts, key=lambda m: (-m.harmful, -m.n))
    top_effective = [m.metric_key for m in eff_sorted if m.effective > 0][:3]
    top_harmful = [m.metric_key for m in harm_sorted if m.harmful > 0][:3]

    # 数据驱动建议（诚实，仅在有数据时给出）
    suggestions: list[str] = []
    if top_effective:
        best = eff_sorted[0]
        suggestions.append(
            f"历史数据显示：优化「{best.metric_key}」在 {best.n} 次尝试中有效 {best.effective} 次"
            f"（成功率 {best.success_rate:.0%}，平均改善 {best.mean_relative_pct:+.1f}%），"
            f"可优先纳入后续提案。"
        )
    if top_harmful:
        worst = harm_sorted[0]
        suggestions.append(
            f"警惕：改动「{worst.metric_key}」曾出现 {worst.harmful} 次有害结果，"
            f"提案前需更强围栏 / 受控回滚。"
        )
    if not suggestions:
        suggestions.append("历史证据不足，暂无法给出数据驱动建议（需积累更多演化证据）。")

    return ImpactReport(
        generated_at=now, has_data=True, metrics=impacts,
        overall_score=overall, n_changes=n_changes,
        top_effective=top_effective, top_harmful=top_harmful,
        suggestions=suggestions,
    )


def score_for_proposal(report: ImpactReport, metric_keys: list[str]) -> dict[str, Any]:
    """给定候选提案关注的指标列表，返回该提案的历史影响评分（数据驱动）。

    用于「我建议 X，因为历史数据证明有益」：把提案目标指标的历史成功率 /
    平均改善作为支撑证据。无历史数据 → 诚实返回 has_data=False。
    """
    if not report.has_data:
        return {"has_data": False, "score": 0.0, "support": "no_historical_evidence"}
    by_key = {m.metric_key: m for m in report.metrics}
    relevant = [by_key[k] for k in metric_keys if k in by_key]
    if not relevant:
        return {
            "has_data": True, "score": 0.0,
            "support": "no_matching_metric_in_history",
            "known_metrics": [m.metric_key for m in report.metrics],
        }
    n = sum(m.n for m in relevant)
    eff = sum(m.effective for m in relevant)
    mean_rel = sum(m.mean_relative_pct for m in relevant) / len(relevant)
    score = (eff / n) if n else 0.0
    return {
        "has_data": True,
        "score": round(score, 4),
        "mean_relative_pct": round(mean_rel, 4),
        "n": n,
        "support": "historical_evidence" if score > 0 else "historical_but_ineffective",
        "metrics": [m.to_dict() for m in relevant],
    }


def build_impact_report(data_dir: str, *, now: float | None = None) -> ImpactReport:
    """从 data_dir 直接构建演化影响评分报告（便捷封装）。"""
    corpus = load_evidence_corpus(data_dir)
    return compute_impact(corpus, now=now)
