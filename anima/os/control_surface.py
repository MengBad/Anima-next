"""R14 —— Human-AI OS 控制面（Control Surface）。

面向 Owner 的「人机协同操作系统」控制面：**聚合** ANIMA 作为 Autonomous OS 的全景
（健康 / 演化提案 / 受控恢复计划 / 观测事件），并解析 Owner 的**受限自然语言指令**。

设计纪律（延续 R6–R14 受控原则）：
- 本模块是**控制面 / 聚合层**，**不持有任何新的执行权限**。
- 指令解析器基于**白名单 + 确定性规则**（无 LLM、可复现、可审计），只识别已知意图；
  未识别指令一律返回 ``unknown``，绝不猜测执行。
- 只读类指令（查看健康 / 列出提案）可直接返回数据；**特权类指令（批准/拒绝/恢复）只产出
  「待确认动作」**，指明应走的既有 Owner 授权端点（R9 / R13），由 Owner 在既有通道确认，
  R14 绝不自我授权（self-improvement ≠ self-authorization）。
- Observation → Evaluation → Evolution 的既有闭环在此只做**可视化聚合**，不改变其行为。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class OSOverview:
    """Autonomous OS 全景快照（聚合层，只读）。"""

    health: dict[str, Any] = field(default_factory=dict)
    recovery_plans: list[dict[str, Any]] = field(default_factory=list)
    evolution_proposals: list[dict[str, Any]] = field(default_factory=list)
    observation_events: int = 0
    degraded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "health": self.health,
            "recovery_plans": self.recovery_plans,
            "evolution_proposals": self.evolution_proposals,
            "observation_events": self.observation_events,
            "degraded": self.degraded,
        }


@dataclass
class ParsedCommand:
    """一条被解析的 Owner 指令（受控、可审计）。"""

    intent: str              # health | list_proposals | approve | reject | recover | unknown
    args: dict[str, Any] = field(default_factory=dict)
    needs_confirmation: bool = False  # True → 须走既有 Owner 授权端点确认
    confirmation_endpoint: str = ""    # 既有授权端点（仅描述，不调用）
    preview: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "args": self.args,
            "needs_confirmation": self.needs_confirmation,
            "confirmation_endpoint": self.confirmation_endpoint,
            "preview": self.preview,
        }


class HumanAIControlSurface:
    """Human-AI OS 控制面：聚合全景 + 解析受限指令。"""

    def __init__(self, core: Any = None) -> None:
        self._core = core

    # ------------------------------------------------------------------
    # 全景聚合（只读，复用既有能力）
    # ------------------------------------------------------------------
    def overview(self, *, health: dict | None = None,
                 recovery_plans: list | None = None,
                 evolution_proposals: list | None = None,
                 observation_events: int = 0) -> OSOverview:
        """聚合 Autonomous OS 全景。各字段可注入（便于测试 / 不强制依赖 core）。"""
        health = health or (self._snapshot_health() if self._core is not None else {})
        recovery = recovery_plans if recovery_plans is not None else (
            self._recovery_plans() if self._core is not None else []
        )
        props = evolution_proposals if evolution_proposals is not None else (
            self._evolution_proposals() if self._core is not None else []
        )
        degraded = bool(health.get("degraded")) or any(
            p.get("severity") in ("high", "critical") for p in recovery
        )
        return OSOverview(
            health=health, recovery_plans=recovery,
            evolution_proposals=props, observation_events=observation_events,
            degraded=degraded,
        )

    def _snapshot_health(self) -> dict[str, Any]:
        try:
            from ..agent.autonomous_operation import AutonomousOperationController
            ctrl = AutonomousOperationController()
            rep = ctrl.evaluate(self._core)
            return {"snapshot": rep.snapshot, "degraded": rep.degraded,
                    "plans": [p.to_dict() for p in rep.plans]}
        except Exception:  # noqa: BLE001 - 聚合失败不阻断
            return {"snapshot": {}, "degraded": True, "plans": []}

    def _recovery_plans(self) -> list[dict[str, Any]]:
        try:
            from ..agent.autonomous_operation import AutonomousOperationController
            ctrl = AutonomousOperationController()
            rep = ctrl.evaluate(self._core)
            return [p.to_dict() for p in rep.plans]
        except Exception:  # noqa: BLE001
            return []

    def _evolution_proposals(self) -> list[dict[str, Any]]:
        try:
            from ..agent.self_evolution import list_evolution_proposals
            return list_evolution_proposals()
        except Exception:  # noqa: BLE001
            return []

    # ------------------------------------------------------------------
    # 受限自然语言指令解析（白名单 + 确定性规则，无 LLM）
    # ------------------------------------------------------------------
    def parse_command(self, text: str) -> ParsedCommand:
        """解析 Owner 指令。未识别 → unknown（绝不猜测执行）。"""
        t = (text or "").strip()
        if not t:
            return ParsedCommand(intent="unknown", preview="空指令")

        # 批准提案 <id>（特权，需确认）
        m = re.search(r"批准\s*(?:提案)?\s*([A-Za-z0-9\-_]{3,})", t)
        if m:
            pid = m.group(1)
            return ParsedCommand(
                intent="approve", args={"proposal_id": pid},
                needs_confirmation=True,
                confirmation_endpoint="POST /api/evolution/{proposal_id}/approve",
                preview=f"将批准提案 {pid}（须经 R9 Owner 授权端点确认）",
            )

        # 拒绝提案 <id>（特权，需确认）
        m = re.search(r"拒绝\s*(?:提案)?\s*([A-Za-z0-9\-_]{3,})", t)
        if m:
            pid = m.group(1)
            return ParsedCommand(
                intent="reject", args={"proposal_id": pid},
                needs_confirmation=True,
                confirmation_endpoint="POST /api/evolution/{proposal_id}/reject",
                preview=f"将拒绝提案 {pid}（须经 R9 Owner 授权端点确认）",
            )

        # 运行健康检查 / 恢复评估（只读，触发一次评估）—— 须在 health 之前判定
        if re.search(r"(恢复|recover|健康检查|诊断)", t, re.IGNORECASE):
            return ParsedCommand(
                intent="recover", needs_confirmation=False,
                preview="返回当前受控恢复计划（需 Owner 授权的动作已标 requires_owner）",
            )

        # 查看健康（只读）
        if re.search(r"(查看|检查|状态|健康|health)", t, re.IGNORECASE) and "提案" not in t:
            return ParsedCommand(
                intent="health", needs_confirmation=False,
                preview="返回当前运行时健康与受控恢复计划",
            )

        # 列出演化提案（只读）
        if re.search(r"(演化|提案|evolution|proposal)", t, re.IGNORECASE) and "批准" not in t and "拒绝" not in t:
            return ParsedCommand(
                intent="list_proposals", needs_confirmation=False,
                preview="返回待审批的自我进化提案列表",
            )

        return ParsedCommand(intent="unknown", preview=f"未识别指令：{t[:40]}")
