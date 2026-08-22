# -*- coding: utf-8 -*-
"""R18 —— Self Evaluation（自我评估，只读、只观测）。

把已存在的「运行事实」聚合为一份诚实的多轴自评，供 Owner 控制台 / OS 面板只读查看。

设计纪律（延续 R6-R20 / 生产即圣域）：
- 只聚合、只陈述、绝不自我授权：本模块零写入、零权限提升、零自动工程。
  任何建议都落入 recommendations 字段，交由既有 Owner 审批门处理。
- 证据可溯源：每个维度的 score 都带 evidence，符合 R6.5 Audit Everything。
- fail-closed / 诚实：任一信号源不可用 -> 该维度标 unknown，绝不编造分数。
- 评分用于让 Owner 一眼看清状态，不用于任何自动决策阈值。

评分轴（均为 0.0~1.0，越高越好；unknown 表示信号缺失）：
- runtime     运行时健康（存活/通道/模型链路）
- observation 真实环境观测完整度（sources 覆盖 / 缺失诚实）
- stability   长期稳定（近 7/30/90 天 uptime / 异常 / 漂移 / 演化计数）
- safety      护栏有效性（审计 DENY 是否受控、配置/权限漂移是否被盯住）
- evolution   演化纪律（提案是否都经 Owner 审批、有无越权自改）
- interaction 交互质量（对话质量/工具成功率/人格一致性/图片交付，P5）
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


@dataclass
class AxisScore:
    """单轴评分（只读、可溯源）。"""
    axis: str
    score: Optional[float]   # 1.0=最好；None=unknown（信号缺失/不可判）
    status: str              # ok / attention / unknown
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class SelfEvaluation:
    """一份诚实的多轴自评快照。"""
    generated_at: float
    axes: list[AxisScore] = field(default_factory=list)
    overall: Optional[float] = None
    attention: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "overall": round(self.overall, 4) if self.overall is not None else None,
            "axes": [
                {
                    "axis": a.axis,
                    "score": round(a.score, 4) if a.score is not None else None,
                    "status": a.status,
                    "summary": a.summary,
                    "evidence": a.evidence,
                }
                for a in self.axes
            ],
            "attention": self.attention,
            "recommendations": self.recommendations,
        }


def _score_runtime(snapshot: dict[str, Any]) -> AxisScore:
    degr = bool(snapshot.get("degraded"))
    snap = snapshot.get("snapshot") or {}
    running = float(snap.get("runtime_running", 0.0))
    channel = float(snap.get("channel_connected", 0.0))
    llm_fail = float(snap.get("llm_failure_rate", 0.0) or 0.0)
    pending = float(snap.get("automation_pending", 0.0) or 0.0)
    if not snap:
        return AxisScore("runtime", None, "unknown", "无运行时快照，无法判定", {})
    base = (running * 0.4 + channel * 0.4)
    penalty = 0.0
    if llm_fail > 0.1:
        penalty += min(0.3, llm_fail)
    if pending > 50:
        penalty += min(0.2, (pending - 50) / 250.0)
    score = _clamp(base - penalty)
    status = "ok" if (score >= 0.7 and not degr) else "attention"
    ev = {
        "runtime_running": running, "channel_connected": channel,
        "llm_failure_rate": llm_fail, "automation_pending": pending,
        "degraded": degr,
    }
    return AxisScore(
        "runtime", score, status,
        "运行时健康" if status == "ok" else "运行时存在关注项", ev)


def _score_observation(report: dict[str, Any]) -> AxisScore:
    avail = report.get("sources_available") or []
    missing = report.get("sources_missing") or []
    incomplete = bool(report.get("incomplete"))
    if not avail and not missing:
        return AxisScore("observation", None, "unknown", "无观测报告，无法判定", {})
    total = len(avail) + len(missing)
    score = _clamp(len(avail) / total) if total else 0.0
    status = "ok" if (score >= 0.7 and not incomplete) else "attention"
    return AxisScore(
        "observation", score, status,
        "真实环境观测覆盖" if status == "ok" else "真实环境观测不完整（部分源缺失）",
        {"available": avail, "missing": missing, "incomplete": incomplete})


def _score_stability(stability: dict[str, Any]) -> AxisScore:
    windows = {k: v for k, v in stability.items() if isinstance(v, dict)}
    if not windows:
        return AxisScore("stability", None, "unknown", "无稳定性记录，无法判定", {})
    best_key = next((k for k in ("90d", "30d", "7d") if k in windows), None)
    rec = windows.get(best_key, {})
    uptime = float(rec.get("uptime_ratio", 0.0) or 0.0)
    anomaly = int(rec.get("anomaly_total", 0) or 0)
    drift = int(rec.get("permission_drift_count", 0) or 0)
    score = _clamp(uptime - min(0.2, (anomaly + drift) * 0.02))
    status = "ok" if (uptime >= 0.95 and not (anomaly or drift)) else "attention"
    ev = {best_key: {"uptime_ratio": uptime, "anomaly_total": anomaly,
                     "permission_drift_count": drift}}
    return AxisScore(
        "stability", score, status,
        "长期运行稳定" if status == "ok" else "长期运行存在关注项", ev)


def _score_safety(audit_trend: dict[str, Any], drift: dict[str, Any]) -> AxisScore:
    ev: dict[str, Any] = {}
    score = 1.0
    if audit_trend:
        ev["audit_deny"] = {"available": audit_trend.get("available"),
                            "growing": audit_trend.get("growing"),
                            "current": audit_trend.get("current")}
        if audit_trend.get("growing"):
            score -= 0.3
    if drift:
        ev["config_drift"] = {"available": drift.get("available"),
                              "drift_count": drift.get("drift_count")}
        if drift.get("available") is False:
            score -= 0.2
    if not ev:
        return AxisScore("safety", None, "unknown", "无护栏信号，无法判定", {})
    score = _clamp(score)
    # 护栏轴零容忍：任何扣分项都落到 attention，供 Owner 关注。
    status = "ok" if score > 0.7 else "attention"
    return AxisScore(
        "safety", score, status,
        "护栏有效" if status == "ok" else "护栏存在关注项（DENY 增长或漂移监测异常）", ev)


def _score_evolution(evolution: dict[str, Any]) -> AxisScore:
    if not evolution:
        return AxisScore("evolution", None, "unknown", "无演化信号，无法判定", {})
    ev = {}
    score = 1.0
    unapproved_runs = int(evolution.get("unapproved_self_mod_count", 0) or 0)
    ev["unapproved_self_mod_count"] = unapproved_runs
    if unapproved_runs > 0:
        score -= min(0.8, unapproved_runs * 0.2)
    pending = int(evolution.get("pending_owner_count", 0) or 0)
    ev["pending_owner_count"] = pending
    score = _clamp(score)
    status = "ok" if unapproved_runs == 0 else "attention"
    return AxisScore(
        "evolution", score, status,
        "演化纪律良好（无越权自改）" if status == "ok" else "演化纪律告警：存在未审批自改", ev)


def _score_interaction(interaction: dict[str, Any]) -> AxisScore:
    """P5 —— 交互质量轴（对话 / 工具 / 人格一致性 / 图片交付）。

    interaction 期望含子字典，各子源缺失则该项 marked unknown（诚实 fail-closed）：
    - conversation: ok_ratio（对话适当率）, samples
    - tools:       success_rate（工具成功率）, calls
    - persona:     stability（人格一致性 0~1，越高越稳定）, drift_rejects（近窗口拒绝漂移次数）
    - image:       delivery_ok_ratio（图片成功交付率）, samples
    """
    ev: dict[str, Any] = {}
    subs: list[float] = []
    for key in ("conversation", "tools", "persona", "image"):
        sub = interaction.get(key)
        if not isinstance(sub, dict) or not sub:
            ev[key] = {"available": False}
            continue
        ok_ratio = sub.get("ok_ratio")
        success = sub.get("success_rate")
        stab = sub.get("stability")
        deliver = sub.get("delivery_ok_ratio")
        val = None
        if key == "conversation":
            val = _clamp(float(ok_ratio)) if ok_ratio is not None else None
        elif key == "tools":
            val = _clamp(float(success)) if success is not None else None
        elif key == "persona":
            val = _clamp(float(stab)) if stab is not None else None
        elif key == "image":
            val = _clamp(float(deliver)) if deliver is not None else None
        ev[key] = {"available": val is not None,
                   "value": round(val, 4) if val is not None else None,
                   **({k: sub.get(k) for k in ("samples", "calls", "drift_rejects") if sub.get(k) is not None})}
        if val is not None:
            subs.append(val)
    if not ev:
        return AxisScore("interaction", None, "unknown", "无交互质量信号，无法判定", {})
    if not subs:
        return AxisScore("interaction", None, "unknown",
                         "交互质量各源均缺失，无法判定", ev)
    score = _clamp(sum(subs) / len(subs))
    status = "ok" if (score >= 0.7 and len(subs) == 4) else "attention"
    summary = (
        "交互质量良好（对话/工具/人格/图片均完整）"
        if status == "ok"
        else ("交互质量关注项（分数偏低或部分信号缺失）"
              if len(subs) < 4 else "交互质量关注项（分数偏低）"))
    return AxisScore("interaction", score, status, summary, ev)


def build_self_evaluation(
    *,
    runtime_snapshot: Optional[dict[str, Any]] = None,
    environment_report: Optional[dict[str, Any]] = None,
    stability: Optional[dict[str, Any]] = None,
    audit_deny_trend: Optional[dict[str, Any]] = None,
    config_drift: Optional[dict[str, Any]] = None,
    evolution: Optional[dict[str, Any]] = None,
    interaction: Optional[dict[str, Any]] = None,
    now: Optional[float] = None,
) -> SelfEvaluation:
    """把各信号源（均已存在）聚合为一份诚实自评。"""
    now = now if now is not None else time.time()
    axes: list[AxisScore] = []
    axes.append(_score_runtime(runtime_snapshot or {}))
    axes.append(_score_observation(environment_report or {}))
    axes.append(_score_stability(stability or {}))
    axes.append(_score_safety(audit_deny_trend or {}, config_drift or {}))
    axes.append(_score_evolution(evolution or {}))
    axes.append(_score_interaction(interaction or {}))

    known = [a for a in axes if a.score is not None]
    overall = round(sum(a.score for a in known) / len(known), 4) if known else None

    attention = [a.axis for a in axes if a.status == "attention"]
    recommendations: list[str] = []
    miss = [a for a in axes if a.status == "unknown"]
    if miss:
        recommendations.append(
            "以下信号源当前不可用，建议排查采集链路："
            + ", ".join(m.axis for m in miss))
    if any(a.axis == "observation" and "github" in (a.evidence.get("missing") or [])
           for a in axes):
        recommendations.append(
            "GitHub 真实信号缺失（私有库/无凭据）：可设 ANIMA_GITHUB_TOKEN 启用带鉴权观测，"
            "或确认公开可见性。")
    if any(a.axis == "safety" and a.status == "attention" for a in axes):
        recommendations.append(
            "护栏关注项：建议查看审计 DENY 增长与配置/权限漂移详情，必要时收紧策略。")

    return SelfEvaluation(
        generated_at=now, axes=axes, overall=overall,
        attention=attention, recommendations=recommendations)
