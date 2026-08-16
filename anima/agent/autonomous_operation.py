"""R13 —— Autonomous Operation：运行时健康与受控恢复（Health & Recovery）。

让 ANIMA 具备「长期运行」的自治运维能力：**检测**运行时降级、**提出**受控恢复计划，
但**绝不自我执行**任何特权恢复动作（如重启 / 重部署），落地需 Owner 授权。

设计纪律（延续 R6–R10，尤其 Production Is Sacred / No Hidden Authority / Fail-Closed）：
- 只读采集核心健康信号（复用 R10-02 ``collect_metrics``），不修改生产状态。
- ``RecoveryPlanner`` 输出**建议性** ``RecoveryPlan``：每条标注 ``requires_owner``——
  涉及运行时/渠道/部署的恢复动作必须 Owner 显式授权才允许执行；分析与节流类建议仅为咨询。
- 任何健康信号缺失 / 不可读 → fail-closed 判为 ``critical`` 并 ``requires_owner=True``
  （宁可保守告警，也绝不默认「健康」而静默）。
- 可经 ``to_dev_issues`` 桥接 R8 DevIssue（source=recovery，capability=task.plan + read scope），
  进入既有演化闭环；R8 受控门禁保证即便进闭环也不自动部署。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# R13 行为结果来源标记（写入 DevIssue.source）
ISSUE_SOURCE_RECOVERY = "recovery"

# 默认阈值
_DEFAULTS = {
    "llm_failure_rate_high": 0.5,   # LLM 失败率高于此 → 高严重，建议复核配置
    "automation_pending_high": 50,  # 待处理自动化积压高于此 → normal，建议节流
    "running_ok": 1.0,
    "channel_ok": 1.0,
}


@dataclass
class RecoveryPlan:
    """一条受控恢复建议（建议性，不授权任何动作）。"""

    action: str          # restart_runtime | reconnect_channel | review_llm | throttle_automation
    severity: str        # normal | high | critical
    requires_owner: bool # True → 执行该恢复需 Owner 显式授权（fail-closed）
    rationale: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "severity": self.severity,
            "requires_owner": self.requires_owner,
            "rationale": self.rationale,
            "evidence": self.evidence,
        }


@dataclass
class HealthReport:
    """一次健康评估结果。"""

    snapshot: dict[str, float]
    degraded: bool
    plans: list[RecoveryPlan] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot": dict(self.snapshot),
            "degraded": self.degraded,
            "plans": [p.to_dict() for p in self.plans],
        }


class HealthRecoveryPlanner:
    """从健康快照确定性产出受控恢复计划（fail-closed）。"""

    def __init__(self, *, thresholds: dict[str, float] | None = None) -> None:
        self._t = {**_DEFAULTS, **(thresholds or {})}

    def plan(self, snapshot: dict[str, float]) -> list[RecoveryPlan]:
        """基于健康快照产出恢复计划列表（确定性、可复现、无 LLM）。"""
        plans: list[RecoveryPlan] = []
        running = float(snapshot.get("runtime_running", 0.0))
        connected = float(snapshot.get("channel_connected", 0.0))
        fail_rate = float(snapshot.get("llm_failure_rate", 0.0))
        pending = float(snapshot.get("automation_pending", 0.0))

        # 运行时未运行 → critical，需 Owner 授权重启（绝不静默）
        if running < self._t["running_ok"]:
            plans.append(RecoveryPlan(
                action="restart_runtime", severity="critical", requires_owner=True,
                rationale="运行时进程未运行（runtime_running=0），建议 Owner 授权重启。",
                evidence={"runtime_running": running},
            ))

        # 渠道未连接 → high，需 Owner 授权重连（NapCat/QQ）
        if connected < self._t["channel_ok"]:
            plans.append(RecoveryPlan(
                action="reconnect_channel", severity="high", requires_owner=True,
                rationale="QQ 渠道未连接（channel_connected=0），建议 Owner 授权重连 NapCat。",
                evidence={"channel_connected": connected},
            ))

        # LLM 高失败率 → high，建议复核配置（分析类，默认需 Owner 确认才动配置）
        if fail_rate >= float(self._t["llm_failure_rate_high"]):
            plans.append(RecoveryPlan(
                action="review_llm", severity="high", requires_owner=True,
                rationale=f"LLM 失败率 {fail_rate:.0%} 偏高，建议复核 LLM 配置 / fallback 模型（需 Owner 授权调整）。",
                evidence={"llm_failure_rate": round(fail_rate, 4)},
            ))

        # 自动化积压 → normal，建议节流（咨询类，不触生产）
        if pending >= float(self._t["automation_pending_high"]):
            plans.append(RecoveryPlan(
                action="throttle_automation", severity="normal", requires_owner=False,
                rationale=f"待处理自动化 {int(pending)} 条积压，建议节流主动发言频率。",
                evidence={"automation_pending": int(pending)},
            ))
        return plans


class AutonomousOperationController:
    """运行时健康与受控恢复控制器：探测 → 规划 →（可选）桥接 R8。"""

    def __init__(self, *, thresholds: dict[str, float] | None = None) -> None:
        self._planner = HealthRecoveryPlanner(thresholds=thresholds)

    def probe(self, core: Any) -> dict[str, float]:
        """采集核心健康快照（复用 R10-02，只读）。"""
        from ..evolution.evaluator import collect_metrics
        try:
            return dict(collect_metrics(core).metrics)
        except Exception:  # noqa: BLE001 - 采集中断也 fail-closed 视为未知
            return {}

    def evaluate(self, core: Any = None, *, snapshot: dict[str, float] | None = None) -> HealthReport:
        """评估健康并产出恢复计划。优先用传入 snapshot，否则探测 core。"""
        snap = snapshot if snapshot is not None else (self.probe(core) if core is not None else {})
        # 信号缺失 → fail-closed：视为 critical（running=0）
        if not snap:
            snap = {"runtime_running": 0.0, "channel_connected": 0.0,
                    "llm_failure_rate": 0.0, "automation_pending": 0.0}
        plans = self._planner.plan(snap)
        return HealthReport(snapshot=snap, degraded=bool(plans), plans=plans)

    def to_dev_issues(self, plans: list[RecoveryPlan]) -> list[Any]:
        """把恢复计划桥接为 R8 DevIssue（source=recovery，受控门禁在 R8 层）。"""
        from .devloop import DevIssue

        issues: list[DevIssue] = []
        for p in plans:
            issues.append(DevIssue(
                title=f"[自治运维] {p.action}（{p.severity}）",
                detail=f"{p.rationale}\n证据：{p.evidence}\n需 Owner 授权：{p.requires_owner}",
                source=ISSUE_SOURCE_RECOVERY,
                capability="task.plan",
                required_scopes=frozenset({"read"}),
                severity="high" if p.severity in ("high", "critical") else "normal",
            ))
        return issues
