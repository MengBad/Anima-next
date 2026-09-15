# -*- coding: utf-8 -*-
"""R19 —— 自主工程闭环快照（Engineering Loop，只读、只提案、绝不执行）。

把既有的「观测 → 自评 → 提案 → Owner 门禁」拼成一份闭环状态快照，供 Owner
控制台只读查看。R19 的定位是提案闭环，不是自动改代码：

- 只归集、只陈述、只提案：本模块零写入、零权限提升、零自动工程。
  任何可行动项都落在 proposed_issues / recommendations，交由既有 Owner 审批门
  （R8 DevLoopOrchestrator / R9 EvolutionGate / R14 控制面）处理。
- 复用既有能力，不重复实现：
  * 观测：anima.observation.collector.collect_all（R10-01）
  * 自监控提案：anima.agent.issue_sources.collect_self_monitor_issues（R8）
  * 演化提案：anima.agent.self_evolution.list_evolution_proposals（R9）
  * 自评：anima.observation.self_evaluation.build_self_evaluation（R18）
- fail-closed / 诚实：任一信号源不可用 -> 该段标记 unavailable，绝不编造。
- 闭环健康只做阶段覆盖判定，不用于任何自动决策阈值。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class EngineeringLoop:
    """一份诚实的自主工程闭环快照（只读）。"""

    generated_at: float
    loop_health: str                  # ok | degraded | unknown
    stages: dict[str, bool]           # 各阶段覆盖
    observed_count: int = 0
    proposed_issues: list[dict[str, Any]] = field(default_factory=list)
    pending_owner_count: int = 0
    self_evaluation: Optional[dict[str, Any]] = None
    unavailable: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "loop_health": self.loop_health,
            "stages": self.stages,
            "observed_count": self.observed_count,
            "proposed_issues": self.proposed_issues,
            "pending_owner_count": self.pending_owner_count,
            "self_evaluation": self.self_evaluation,
            "unavailable": self.unavailable,
            "recommendations": self.recommendations,
        }


# 闭环阶段（Observe -> Plan -> Implement -> Test -> Review -> Deploy -> Evidence）。
_LOOP_STAGES = ("observe", "plan", "implement", "test", "review", "deploy", "evidence")


def _default_stages() -> dict[str, bool]:
    return {s: False for s in _LOOP_STAGES}


def build_engineering_loop(
    *,
    core: Any = None,
    observation_events: Optional[list[Any]] = None,
    proposed_issues: Optional[list[Any]] = None,
    evolution_proposals: Optional[list[dict[str, Any]]] = None,
    self_evaluation: Optional[dict[str, Any]] = None,
    now: Optional[float] = None,
) -> EngineeringLoop:
    """把既有信号源（均已存在）聚合为一份闭环快照。

    各信号源可显式注入（便于测试 / 不强制依赖 core）；未注入时按需采集，
    采集失败即诚实记入 unavailable。stages 反映「该阶段能力是否存在」，用于
    定位闭环缺口，绝不表示自动执行任何阶段。
    """
    now = now if now is not None else time.time()
    stages = _default_stages()
    unavailable: list[str] = []
    recommendations: list[str] = []

    # -- 观测阶段（observe）--
    events: list[Any] = []
    if observation_events is not None:
        events = list(observation_events)
        stages["observe"] = True
    elif core is not None:
        try:
            from .collector import collect_all
            events = list(collect_all(core))
            stages["observe"] = True
        except Exception:  # noqa: BLE001 - 采集失败不阻断快照
            unavailable.append("observation")
    else:
        unavailable.append("observation")
    observed_count = len(events)

    # -- 提案阶段（plan，来自 R8 自监控采集器）--
    issues: list[Any] = []
    if proposed_issues is not None:
        issues = list(proposed_issues)
        stages["plan"] = True
    elif core is not None:
        try:
            from ..agent.issue_sources import collect_self_monitor_issues
            audit = getattr(core, "audit", None)
            denials: list[dict[str, Any]] = []
            try:
                if audit is not None and hasattr(audit, "recent"):
                    denials = [
                        e for e in (audit.recent() or [])
                        if "deny" in str(e.get("type", "")).lower()
                        or "denied" in str(e.get("type", "")).lower()
                    ]
            except Exception:  # noqa: BLE001
                denials = []
            selfcode = getattr(core, "selfcode_manager", None)
            rq = getattr(core, "reply_quality", None)
            rq_report = None
            try:
                if rq is not None and hasattr(rq, "report"):
                    rq_report = rq.report()
            except Exception:  # noqa: BLE001
                rq_report = None
            issues = list(collect_self_monitor_issues(
                selfcode_manager=selfcode, audit_denials=denials,
                reply_quality_report=rq_report))
            stages["plan"] = True
        except Exception:  # noqa: BLE001 - 提案采集失败不阻断快照
            unavailable.append("proposals")
    else:
        unavailable.append("proposals")
    proposed = [i.to_dict() if hasattr(i, "to_dict") else i for i in issues]

    # -- 演化提案（R9 门禁）--
    evo: list[dict[str, Any]] = []
    if evolution_proposals is not None:
        evo = list(evolution_proposals)
    else:
        try:
            from ..agent.self_evolution import list_evolution_proposals
            evo = list(list_evolution_proposals())
        except Exception:  # noqa: BLE001 - 演化门不可用不阻断快照
            unavailable.append("evolution")
    pending_owner_count = sum(
        1 for p in evo
        if str(p.get("status") or p.get("decision") or "") == "pending_owner"
    )

    # -- 自评（R18）--
    se = self_evaluation
    if se is None and core is not None:
        try:
            from ..os.control_surface import HumanAIControlSurface
            surf = HumanAIControlSurface(core)
            se = surf.self_evaluation()
        except Exception:  # noqa: BLE001 - 自评失败不阻断快照
            unavailable.append("self_evaluation")
            se = None
    elif se is None:
        unavailable.append("self_evaluation")

    # 阶段覆盖：implement/test/review/deploy/evidence 依据既有模块可用性只读探测。
    for name, mod in (
        ("implement", "anima.agent.devloop"),
        ("test", "anima.agent.devloop"),
        ("review", "anima.agent.orchestrator"),
        ("deploy", "anima.agent.devloop"),
        ("evidence", "anima.evolution.evaluator"),
    ):
        try:
            __import__(mod)
            stages[name] = True
        except Exception:  # noqa: BLE001
            stages[name] = False

    # -- 闭环健康（只读，只反映阶段覆盖，不做自动决策）--
    covered = sum(1 for v in stages.values() if v)
    if covered == len(_LOOP_STAGES):
        loop_health = "ok"
    elif covered == 0:
        loop_health = "unknown"
    else:
        loop_health = "degraded"

    # -- 诚实建议 --
    if unavailable:
        recommendations.append(
            "以下信号源当前不可用，建议排查采集链路：" + ", ".join(unavailable))
    missing_stages = [s for s in _LOOP_STAGES if not stages[s]]
    if missing_stages:
        recommendations.append(
            "闭环阶段缺失：" + ", ".join(missing_stages) + "（仅提示能力缺口，不自动补齐）")
    if pending_owner_count:
        recommendations.append(
            "有 " + str(pending_owner_count) + " 条演化提案等待 Owner 审批（fail-closed，绝不自动放行）")

    return EngineeringLoop(
        generated_at=now,
        loop_health=loop_health,
        stages=stages,
        observed_count=observed_count,
        proposed_issues=proposed,
        pending_owner_count=pending_owner_count,
        self_evaluation=se,
        unavailable=unavailable,
        recommendations=recommendations,
    )
