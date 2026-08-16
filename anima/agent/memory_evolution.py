"""R11-01 —— Memory Evolution Engine：从自身行为结果中**提出**记忆/技能优化。

设计原则（延续 R6–R10 受控纪律）：
- 引擎**只产出优化提案（OptimizationProposal）**，绝不自动改记忆、不复写 persona、
  不自动装/卸技能。真正落地需经 R8 闭环 + Owner 授权（self-improvement ≠ self-authorization）。
- 完全**确定性**规则（阈值驱动，无 LLM 调用），可单测、可复现、可审计。
- 读入「行为结果流」``outcomes``（由记忆/技能/工具调用层在运行时采集，本模块只读），
  产出可桥接 R8 ``DevIssue`` 的优化提案，进入既有「观测/演化/评估」闭环。

优化类别：
- ``skill_deprecate``：某技能高失败率 → 建议降级/重写（不自动禁用）。
- ``skill_promote``：某技能高成功率 + 正向反馈 → 建议晋升为常用能力/固化记忆。
- ``memory_prune``：同一类行动反复失败 → 建议写入「纠正笔记」避免重蹈。
- ``note_suggest``：同一正向模式反复出现 → 建议强化为稳定记忆。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# R11-01 行为结果来源标记（写入 DevIssue.source，仅字符串，非枚举）
ISSUE_SOURCE_EVOLUTION = "evolution"

# 默认阈值（可调）
_DEFAULTS = {
    "min_samples": 5,          # 某技能/模式出现的最小样本数才纳入评估
    "fail_rate_high": 0.5,     # 失败率高于此 → 建议降级
    "success_rate_high": 0.8,  # 成功率高于此且样本足 → 建议晋升
    "pos_feedback_min": 3,     # 正向反馈条数阈值
    "neg_feedback_min": 3,     # 负向反馈条数阈值
}


@dataclass
class OptimizationProposal:
    """一条由行为结果挖掘出的优化提案（受控：仅提案，不落地）。"""

    kind: str                  # skill_deprecate | skill_promote | memory_prune | note_suggest
    target: str                # 技能名 / 行动类型 / 记忆键
    rationale: str
    evidence: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0    # 0..1，样本越多越可信

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "target": self.target,
            "rationale": self.rationale,
            "evidence": self.evidence,
            "confidence": round(self.confidence, 4),
        }


class MemoryEvolutionEngine:
    """从行为结果流中挖掘可优化点，产出受控优化提案。"""

    def __init__(self, *, thresholds: dict[str, float] | None = None) -> None:
        self._t = {**_DEFAULTS, **(thresholds or {})}

    # ------------------------------------------------------------------
    # 行为结果流采集（本模块只读；真实采集由记忆/技能/工具层负责）
    # ------------------------------------------------------------------
    def _aggregate(self, outcomes: list[dict[str, Any]]) -> dict[str, Any]:
        """按 target（技能名/行动类型）聚合成功/失败/反馈统计。"""
        by_target: dict[str, dict[str, Any]] = {}
        for oc in outcomes:
            tgt = str(oc.get("target") or oc.get("skill") or oc.get("action") or "unknown")
            bucket = by_target.setdefault(tgt, {
                "samples": 0, "success": 0, "fail": 0,
                "pos_fb": 0, "neg_fb": 0,
            })
            bucket["samples"] += 1
            if oc.get("success") is True:
                bucket["success"] += 1
            elif oc.get("success") is False:
                bucket["fail"] += 1
            fb = str(oc.get("feedback") or "").lower()
            if fb in ("positive", "pos", "good", "👍"):
                bucket["pos_fb"] += 1
            elif fb in ("negative", "neg", "bad", "👎"):
                bucket["neg_fb"] += 1
        return by_target

    # ------------------------------------------------------------------
    # 提案生成（确定性规则）
    # ------------------------------------------------------------------
    def propose_optimizations(self, outcomes: list[dict[str, Any]]) -> list[OptimizationProposal]:
        """挖掘行为结果，产出优化提案列表（受控、不落地）。"""
        agg = self._aggregate(outcomes)
        proposals: list[OptimizationProposal] = []
        min_n = int(self._t["min_samples"])

        for tgt, s in agg.items():
            n = s["samples"]
            if n < min_n:
                continue
            fail_rate = s["fail"] / n if n else 0.0
            success_rate = s["success"] / n if n else 0.0
            confidence = min(1.0, n / (min_n * 2))

            # 高失败率 → 建议降级该技能/模式
            if fail_rate >= float(self._t["fail_rate_high"]) and s["fail"] >= min_n:
                proposals.append(OptimizationProposal(
                    kind="skill_deprecate", target=tgt,
                    rationale=f"「{tgt}」失败率 {fail_rate:.0%}（{s['fail']}/{n}），建议降级或重写，避免反复出错。",
                    evidence={"samples": n, "fail": s["fail"], "fail_rate": round(fail_rate, 4),
                              "pos_fb": s["pos_fb"], "neg_fb": s["neg_fb"]},
                    confidence=confidence,
                ))
            # 高成功率 + 正向反馈 → 建议晋升为常用/固化记忆
            elif success_rate >= float(self._t["success_rate_high"]) and s["pos_fb"] >= int(self._t["pos_feedback_min"]):
                proposals.append(OptimizationProposal(
                    kind="skill_promote", target=tgt,
                    rationale=f"「{tgt}」成功率 {success_rate:.0%}（{s['success']}/{n}）且正向反馈 {s['pos_fb']} 条，建议晋升为常用能力/固化记忆。",
                    evidence={"samples": n, "success": s["success"], "success_rate": round(success_rate, 4),
                              "pos_fb": s["pos_fb"], "neg_fb": s["neg_fb"]},
                    confidence=confidence,
                ))
            # 同一模式反复失败且负反馈足 → 写入纠正笔记（建议，不自动写）
            if s["fail"] >= min_n and s["neg_fb"] >= int(self._t["neg_feedback_min"]):
                proposals.append(OptimizationProposal(
                    kind="memory_prune", target=tgt,
                    rationale=f"「{tgt}」反复失败（{s['fail']} 次）且负反馈 {s['neg_fb']} 条，建议写入纠正笔记，避免重蹈该模式。",
                    evidence={"samples": n, "fail": s["fail"], "neg_fb": s["neg_fb"]},
                    confidence=confidence,
                ))
            # 同一正向模式反复出现 → 建议强化记忆
            if s["success"] >= min_n and s["pos_fb"] >= int(self._t["pos_feedback_min"]):
                proposals.append(OptimizationProposal(
                    kind="note_suggest", target=tgt,
                    rationale=f"「{tgt}」正向模式稳定（成功 {s['success']} 次，正向反馈 {s['pos_fb']} 条），建议强化为稳定记忆。",
                    evidence={"samples": n, "success": s["success"], "pos_fb": s["pos_fb"]},
                    confidence=confidence,
                ))
        return proposals

    # ------------------------------------------------------------------
    # 桥接 R8 自主开发闭环（与 R10-01 to_dev_issues 同口径：仅归集）
    # ------------------------------------------------------------------
    def to_dev_issues(self, proposals: list[OptimizationProposal]) -> list[Any]:
        """把优化提案桥接为 R8 DevIssue，进入既有演化闭环（受控门禁在 R8 层）。"""
        from anima.agent.devloop import DevIssue

        issues: list[DevIssue] = []
        for p in proposals:
            issues.append(DevIssue(
                title=f"[记忆演化] {p.kind}: {p.target}",
                detail=f"{p.rationale}\n证据：{p.evidence}",
                source=ISSUE_SOURCE_EVOLUTION,
                capability="task.plan",
                required_scopes=frozenset({"read"}),
                severity="normal",
            ))
        return issues
