# -*- coding: utf-8 -*-
"""R20 —— 长期自主运行快照（Long-Term Autonomous Operation，只读、只建议）。

把「长期运行」的既有事实聚合为一份长周期运营快照，供 Owner 控制台只读查看。
R20 定位是长期运营的「继续 / 降级 / 移除」建议器，不是自动运维执行：

- 只归集、只陈述、只建议：本模块零写入、零权限提升、零自动决策。
  任何「降级 / 移除」建议都落在 recommendations / capability_verdicts，交由既有
  Owner 审批门（R8/R9/R14）处理，绝不自动禁用/卸载任何能力。
- 复用既有能力，不重复实现：
  * 稳定记录：anima.os.long_running.collect_stability / latest_stability（P0-02）
  * 健康历史：core.health_history（R16）
  * 演化证据：anima.evolution.impact（P2，Before/After 证据语料）
  * 工程闭环：anima.observation.engineering_loop（R19）
- 确定性规则（阈值驱动，无 LLM），可单测、可复现、可审计。
- fail-closed / 诚实：任一信号源不可用 -> 该能力 verdict 记 unknown，绝不编造。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class CapabilityVerdict:
    """单个能力域的长期运行判定（continue / downgrade / remove / unknown）。"""

    capability: str
    verdict: str          # continue | downgrade | remove | unknown
    evidence: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "verdict": self.verdict,
            "evidence": self.evidence,
            "rationale": self.rationale,
        }


@dataclass
class LongTermOperation:
    """一份诚实的长周期运营快照（只读）。"""

    generated_at: float
    horizon: str                       # 运营展望（ok / degraded / unknown）
    capability_verdicts: list[CapabilityVerdict] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "horizon": self.horizon,
            "capability_verdicts": [v.to_dict() for v in self.capability_verdicts],
            "unavailable": self.unavailable,
            "recommendations": self.recommendations,
        }


# 判定阈值（确定性、可调）。
_DEFAULTS = {
    "uptime_ok": 0.95,          # 90 天 uptime 低于此 -> 关注
    "anomaly_remove": 10,       # 90 天异常累计高于此 -> 建议移除
    "anomaly_downgrade": 3,     # 90 天异常累计高于此 -> 建议降级
    "drift_downgrade": 1,       # 权限漂移 >= 此 -> 建议降级
}


def _verdict_runtime(stability: dict[str, Any]) -> CapabilityVerdict:
    """运行时域：基于 90 天稳定记录。"""
    win = stability.get("90d") if isinstance(stability, dict) else None
    if not win:
        return CapabilityVerdict("runtime", "unknown", {}, "无 90 天稳定记录")
    uptime = float(win.get("uptime_ratio", 0.0) or 0.0)
    anomaly = int(win.get("anomaly_total", 0) or 0)
    drift = int(win.get("permission_drift_count", 0) or 0)
    ev = {"uptime_ratio": uptime, "anomaly_total": anomaly,
          "permission_drift_count": drift}
    if anomaly >= _DEFAULTS["anomaly_remove"]:
        return CapabilityVerdict("runtime", "remove", ev,
                                 "90 天异常累计过高，建议 Owner 排查是否移除相关自治路径")
    if anomaly >= _DEFAULTS["anomaly_downgrade"] or drift >= _DEFAULTS["drift_downgrade"]:
        return CapabilityVerdict("runtime", "downgrade", ev,
                                 "异常或权限漂移需关注，建议 Owner 收紧相关自治阈值")
    if uptime >= _DEFAULTS["uptime_ok"]:
        return CapabilityVerdict("runtime", "continue", ev, "长期运行稳定")
    return CapabilityVerdict("runtime", "downgrade", ev,
                             "uptime 偏低，建议 Owner 检查健康/恢复链路")


def _verdict_observation(unavailable: list[str], observed: int) -> CapabilityVerdict:
    """观测域：基于 R19 工程闭环的观测信号覆盖。"""
    ev = {"observed_count": observed, "unavailable": list(unavailable)}
    if "observation" in unavailable:
        return CapabilityVerdict("observation", "unknown", ev, "观测源不可用")
    if observed == 0:
        return CapabilityVerdict("observation", "downgrade", ev,
                                 "暂无观测事件，建议检查采集链路")
    return CapabilityVerdict("observation", "continue", ev, "观测信号正常覆盖")


def _verdict_evolution(pending_owner: int, evidence_count: int) -> CapabilityVerdict:
    """演化域：基于待审提案与历史 Before/After 证据。"""
    ev = {"pending_owner_count": pending_owner, "evidence_count": evidence_count}
    if pending_owner > 20:
        return CapabilityVerdict("evolution", "downgrade", ev,
                                 "待审提案积压过多，建议 Owner 集中裁决或收紧提案频率")
    if evidence_count > 0:
        return CapabilityVerdict("evolution", "continue", ev, "有历史证据，演化闭环在运转")
    return CapabilityVerdict("evolution", "continue", ev, "演化纪律良好，等待证据积累")


def build_long_term_operation(
    *,
    core: Any = None,
    stability: Optional[dict[str, Any]] = None,
    observed_count: int = 0,
    pending_owner_count: int = 0,
    evidence_count: int = 0,
    now: Optional[float] = None,
) -> LongTermOperation:
    """把长期运行的既有信号聚合为运营快照。

    - stability / observed_count / pending_owner_count / evidence_count 可显式注入
      （便于测试 / 不强制依赖 core）；未注入时按需从 core 采集，失败即诚实记入
      unavailable。
    - 只读、只建议：capability_verdicts 的 downgrade/remove 均为建议，绝不执行。
    """
    now = now if now is not None else time.time()
    unavailable: list[str] = []
    recommendations: list[str] = []

    # -- 稳定记录（P0-02）--
    if stability is None and core is not None:
        try:
            from ..os.long_running import latest_stability
            data_dir = getattr(getattr(core, "config", None), "persistence", None)
            root = getattr(data_dir, "data_dir", "data") if data_dir else "data"
            stability = {
                "90d": latest_stability(root, 90).to_dict()
                if latest_stability(root, 90) is not None else {},
            }
        except Exception:  # noqa: BLE001 - 稳定记录不可用不阻断快照
            unavailable.append("stability")
            stability = {}
    elif stability is None:
        unavailable.append("stability")
        stability = {}
    if not stability:
        unavailable.append("stability")

    verdicts: list[CapabilityVerdict] = []
    verdicts.append(_verdict_runtime(stability))

    # -- 观测域（复用 R19 闭环信号）--
    verdicts.append(_verdict_observation(unavailable, observed_count))

    # -- 演化域（复用 R9/R19 信号）--
    verdicts.append(_verdict_evolution(pending_owner_count, evidence_count))

    # -- 运营展望 + 诚实建议 --
    known = [v for v in verdicts if v.verdict != "unknown"]
    if any(v.verdict == "remove" for v in verdicts):
        horizon = "degraded"
    elif any(v.verdict == "downgrade" for v in verdicts):
        horizon = "degraded"
    elif known and all(v.verdict == "continue" for v in known):
        horizon = "ok"
    elif not known:
        horizon = "unknown"
    else:
        horizon = "ok"

    for v in verdicts:
        if v.verdict in ("downgrade", "remove"):
            recommendations.append(
                f"[{v.capability}] 建议 {v.verdict}：{v.rationale}（仅建议，交 Owner 审批）")
    if unavailable:
        recommendations.append(
            "以下信号源当前不可用，建议排查：" + ", ".join(sorted(set(unavailable))))

    return LongTermOperation(
        generated_at=now,
        horizon=horizon,
        capability_verdicts=verdicts,
        unavailable=sorted(set(unavailable)),
        recommendations=recommendations,
    )
