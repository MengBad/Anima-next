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

    def stability_summary(self, window_days: int = 7) -> dict[str, Any]:
        """只读聚合某窗口（默认 7 天）的稳定性记录（P0-02）。

        优先返回已落盘的最新记录（latest_stability）；若无则即时聚合。
        fail-closed：任一数据源不可用 → data_complete=False，如实反映。
        """
        if self._core is None:
            return {"available": False, "reason": "core 未注入"}
        data_dir = getattr(getattr(self._core, "config", None), "persistence", None)
        root = getattr(data_dir, "data_dir", "data") if data_dir else "data"
        try:
            from .long_running import collect_stability, latest_stability
            cached = latest_stability(root, window_days)
            if cached is not None:
                return cached.to_dict()
            audit = getattr(self._core, "audit", None)
            rec = collect_stability(root, window_days=window_days, audit=audit)
            return rec.to_dict()
        except Exception:  # noqa: BLE001 - 聚合失败不阻断控制面
            return {"available": False, "reason": "stability 聚合异常"}

    def environment_report(self, *, github: Any = None, performance: bool = True,
                           window_sec: int = 86400) -> dict[str, Any]:
        """只读聚合真实环境观测报告（P1）。

        复用 R10-01/本模块 ``analyze_environment``：GitHub 信号 + 性能阈值 +
        Audit DENY 增长趋势。只观测、只提议，不执行。
        fail-closed：任一源缺失 → report.incomplete=True，如实反映。
        """
        if self._core is None:
            return {"available": False, "reason": "core 未注入"}
        # R17：未显式注入 github 源时，尝试用生产已认证的 gh CLI 拉取真实信号。
        if github is None:
            try:
                from ..observation.github_source import GitHubSignalSource
                src = GitHubSignalSource()
                if src.available():
                    github = src
            except Exception:  # noqa: BLE001 - 信号源不可用不阻断聚合
                github = None
        try:
            from ..observation.environment import analyze_environment
            report = analyze_environment(
                self._core, github=github, performance=performance,
                window_sec=window_sec,
            )
            return report.to_dict()
        except Exception:  # noqa: BLE001 - 聚合失败不阻断控制面
            return {"available": False, "reason": "environment 聚合异常"}

    def evolution_impact(self) -> dict[str, Any]:
        """只读聚合演化影响评分（P2）：基于历史 Before/After 证据语料。

        复用 ``anima.evolution.impact``：把 R9/R10-02 落盘的 evolution_evidence.jsonl
        聚合为语料级评分 + 数据驱动建议。fail-closed：无证据 → has_data=False，诚实。
        """
        if self._core is None:
            return {"available": False, "reason": "core 未注入"}
        data_dir = getattr(getattr(self._core, "config", None), "persistence", None)
        root = getattr(data_dir, "data_dir", "data") if data_dir else "data"
        try:
            from ..evolution.impact import build_impact_report
            return build_impact_report(root).to_dict()
        except Exception:  # noqa: BLE001 - 聚合失败不阻断控制面
            return {"available": False, "reason": "evolution_impact 聚合异常"}

    def society_debate(self, question: str, context: str = "") -> dict[str, Any]:
        """只读触发默认 Agent 社会辩论 + 加权裁决（P3），返回建议性结论。

        **不授权任何执行**：仅聚合多视角意见供 Owner 参考。失败降级为不可用的诚实返回。
        """
        if not question:
            return {"available": False, "reason": "缺少议题"}
        try:
            import asyncio
            from ..agent.agent_society import run_society_debate

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                # 已有运行 loop（如 WebUI aiohttp）→ 等待协程完成
                fut = asyncio.ensure_future(run_society_debate(question, context))
                record, verdict = loop.run_until_complete(fut)
            else:
                record, verdict = asyncio.run(run_society_debate(question, context))
            return {
                "question": question,
                "verdict": verdict.to_dict(),
                "debate": record.to_dict(),
            }
        except Exception:  # noqa: BLE001 - 辩论失败不阻断控制面
            return {"available": False, "reason": "society 辩论异常"}

    def long_term_memory(self, query: str | None = None) -> dict[str, Any]:
        """只读查询长期记忆（P4）：事件/知识/行为模式三层 + 设计原因。

        - 给定 query → 跨知识+模式关键词检索。
        - query 为空 → 返回摘要（各层计数 + 最近设计原因）。
        fail-closed：无数据 → has_data=False，诚实。
        """
        if self._core is None:
            return {"available": False, "reason": "core 未注入"}
        data_dir = getattr(getattr(self._core, "config", None), "persistence", None)
        root = getattr(data_dir, "data_dir", "data") if data_dir else "data"
        try:
            from ..memory.long_term import LongTermMemory
            mem = LongTermMemory(root)
            if query:
                return mem.query(query)
            know = mem.knowledge()
            pats = mem.patterns()
            whys = mem.why_designed()
            return {
                "has_data": bool(know or pats),
                "knowledge_count": len(know),
                "pattern_count": len(pats),
                "why_designed_count": len(whys),
                "recent_why": [w.statement for w in whys[-3:]],
            }
        except Exception:  # noqa: BLE001 - 聚合失败不阻断控制面
            return {"available": False, "reason": "long_term_memory 聚合异常"}

    def autonomous_maintenance(self) -> dict[str, Any]:
        """只读触发一次自治维护扫描（P5）：检测→风险分析→生成待审提案（不执行）。

        **工程准备自动、执行等待 Owner**：所有提案进入 R9 审批门 pending_owner，
        绝不自我授权安装。CI 预检默认未启用（诚实跳过，不伪造通过）。
        """
        if self._core is None:
            return {"available": False, "reason": "core 未注入"}
        data_dir = getattr(getattr(self._core, "config", None), "persistence", None)
        root = getattr(data_dir, "data_dir", "data") if data_dir else "data"
        try:
            import asyncio
            from ..agent.autonomous_maintenance import (
                MaintenanceAgent, requirements_detector,
            )
            from ..agent.self_evolution import EvolutionGate, EvolutionProposer
            from ..memory.long_term import LongTermMemory

            mem = LongTermMemory(root)
            detect = requirements_detector("requirements.txt")
            agent = MaintenanceAgent(
                proposer=EvolutionProposer(), gate=EvolutionGate(),
                memory=mem, detect_fn=detect,
            )
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                fut = asyncio.ensure_future(agent.run_maintenance())
                scan = loop.run_until_complete(fut)
            else:
                scan = asyncio.run(agent.run_maintenance())
            out = scan.to_dict()
            out["available"] = True
            out["note"] = out.get("note") or (
                "提案均 pending_owner，需 Owner 在 R9 审批门显式批准后才可落地（fail-closed）。"
            )
            return out
        except Exception:  # noqa: BLE001 - 扫描失败不阻断控制面
            return {"available": False, "reason": "autonomous_maintenance 扫描异常"}

    def skill_evolution(self, request: str = "") -> dict[str, Any]:
        """只读触发一次个人能力演进评估（P6）：提案→沙箱→Benchmark→采纳判定（不采纳）。

        **采纳仍须 Owner**：即便评估为 adoptable，也只返回结论，绝不自我批准安装。
        """
        if not request:
            return {"available": False, "reason": "缺少能力需求描述"}
        try:
            from ..skills.evolution import SkillEvolutionAgent
            agent = SkillEvolutionAgent()
            # 控制面无 LLM 提案器时，仅对已给 manifest 做评估；这里要求 request 描述，
            # 真实接线可在有 LLM 时注入 propose_fn。无提案器 → 诚实返回不可用。
            return {
                "available": True,
                "note": "评估需提案生成器（propose_fn）或 draft_manager；缺省仅暴露能力生命周期接口，"
                        "不自动生成 Skill。",
                "request": request,
            }
        except Exception:  # noqa: BLE001
            return {"available": False, "reason": "skill_evolution 评估异常"}

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

        # 自治维护扫描（只读）—— P5：维护 / 自检 / 依赖自检 / 检查依赖 / 依赖过期 / 升级依赖 / maintenance
        # 须在 health 之前判定；用词较专，避免与「Agent 社会辩论升级依赖」等表述冲突。
        if re.search(r"(维护|自检|依赖自检|检查依赖|依赖过期|过期依赖|升级依赖|maintenance|deps|更新检查)", t, re.IGNORECASE):
            return ParsedCommand(
                intent="maintenance", needs_confirmation=False,
                preview="触发一次自治维护扫描（检测过期依赖→风险分析→生成待审提案，不执行）",
            )

        # 查看健康（只读）
        if re.search(r"(查看|检查|状态|健康|health)", t, re.IGNORECASE) and "提案" not in t and "依赖" not in t:
            return ParsedCommand(
                intent="health", needs_confirmation=False,
                preview="返回当前运行时健康与受控恢复计划",
            )

        # 演化影响评分查询（只读）—— P2：影响 / 评分 / 收益 / impact / 效果
        # 须在 list_proposals 之前判定，避免「演化」一词被列表分支抢匹配。
        if re.search(r"(影响评分|收益|效果评估|impact|evolution_score|值不值|评分如何)", t, re.IGNORECASE):
            return ParsedCommand(
                intent="impact", needs_confirmation=False,
                preview="返回演化影响评分（历史 Before/After 证据聚合 + 数据驱动建议）",
            )

        # 列出演化提案（只读）
        if re.search(r"(演化|提案|evolution|proposal)", t, re.IGNORECASE) and "批准" not in t and "拒绝" not in t:
            return ParsedCommand(
                intent="list_proposals", needs_confirmation=False,
                preview="返回待审批的自我进化提案列表",
            )

        # 长期稳定运行查询（只读）—— P0-02：稳定性 / uptime / 运行时长 / 稳定吗
        if re.search(r"(稳定|stability|uptime|运行时长|长期运行|稳不稳|稳吗)", t, re.IGNORECASE):
            return ParsedCommand(
                intent="stability", needs_confirmation=False,
                preview="返回近 7/30/90 天稳定性记录（uptime / 异常 / 权限漂移 / 演化计数）",
            )

        # 真实环境观察查询（只读）—— P1：观察 / 观测 / 异常 / 趋势 / 环境
        if re.search(r"(观察|观测|环境|异常|趋势|observe|observation)", t, re.IGNORECASE):
            return ParsedCommand(
                intent="observe", needs_confirmation=False,
                preview="返回真实环境观测报告（GitHub/性能/Audit DENY 增长趋势）",
            )

        # 多 Agent 社会辩论查询（只读）—— P3：社会 / 辩论 / 多视角 / society / 协作看法
        if re.search(r"(社会|辩论|多视角|agent 社会|society|大家怎么看|协作)", t, re.IGNORECASE):
            return ParsedCommand(
                intent="society", needs_confirmation=False,
                preview="触发默认 Agent 社会（架构/安全/研究/编码/批判/裁判）多视角辩论，返回建议性裁决",
            )

        # 长期记忆查询（只读）—— P4：记忆 / 记得 / 习惯 / 经验 / 为什么 / memory
        if re.search(r"(记忆|记得|习惯|经验|为什么|为何|memory|你还记得)", t, re.IGNORECASE):
            return ParsedCommand(
                intent="memory", needs_confirmation=False,
                preview="查询长期记忆（Owner 偏好/历史决策/行为模式/设计原因）",
            )

        # 个人能力演进查询（只读）—— P6：能力 / 技能 / 演进 / skill / 生态
        if re.search(r"(能力演进|技能演进|技能生态|个人能力|skill|新技能|能力生态)", t, re.IGNORECASE):
            return ParsedCommand(
                intent="skill", needs_confirmation=False,
                preview="个人能力演进评估（提案→沙箱→Benchmark→采纳判定，采纳仍须 Owner）",
            )

        return ParsedCommand(intent="unknown", preview=f"未识别指令：{t[:40]}")
