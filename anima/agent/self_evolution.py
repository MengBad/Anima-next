"""R9 —— Controlled Self Evolution：受控自我进化。

ANIMA 可以**提出**自身优化，但**绝不自我授权**生产更新。

流程：
    检测不足 → 提出方案(EvolutionProposer) → 沙箱测试(复用 R8 Tester)
    → 生成 Evidence(R6.5) → Owner 审批(EvolutionGate, fail-closed) → 生产更新(复用 R8 Deploy)

核心原则（self-improvement ≠ self-authorization）：
- EvolutionGate 默认 DENY；只有 Owner 显式 ``approve()`` 才放行生产更新。
- 安全围栏：禁止 R9 自动改动**核心权限/安全/部署代码**（见 ``CORE_PROTECTED``），
  触及则需 Owner 走人工 PR 流程，R9 闭环直接拒绝该提案。
- 每次演化有回滚备份（复用 SelfCode ``_backup`` 思路）。

与 R8 的关系：R9 = R8 闭环 + 「提案需 Owner 审批的硬门禁」+ 「核心代码保护围栏」。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

# 受保护的核心文件：R9 闭环**绝不自动修改**（触碰即提案被拒，需 Owner 人工 PR）。
# 这些是权限/安全/部署的命根子，违反「Production Is Sacred」与「No Hidden Authority」。
CORE_PROTECTED = frozenset({
    "anima/core/security.py",
    "anima/capability/rbac.py",
    "anima/agent/orchestrator.py",          # R7 授权逻辑
    "anima/agent/devloop.py",               # R8 闭环（防止自我改写闭环）
    "anima/capability/evidence.py",         # R6.5 证据（审计不可被自我削弱）
    "_deploy.py",                           # 部署脚本自身
    "anima/__main__.py",
    "config/default.yaml",
})


@dataclass
class EvolutionProposal:
    """一次自我进化提案（受控：默认不落地生产）。"""

    id: str
    title: str
    rationale: str = ""                 # 为什么改
    target_files: tuple[str, ...] = ()  # 拟改动的文件
    diff_summary: str = ""              # 改动摘要 / patch 描述
    risk: str = ""                      # 风险评估
    rollback: str = ""                  # 回滚方案
    evidence: str = ""                  # 沙箱测试 / 验证证据
    status: str = "proposed"            # proposed | approved | rejected | applied
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "rationale": self.rationale,
            "target_files": list(self.target_files),
            "diff_summary": self.diff_summary,
            "risk": self.risk,
            "rollback": self.rollback,
            "evidence": self.evidence,
            "status": self.status,
            "created_at": self.created_at,
        }

    def touches_core(self) -> bool:
        """是否触及受保护核心文件（True → 必须 Owner 人工 PR，R9 闭环拒绝自动落地）。"""
        return any(f in CORE_PROTECTED for f in self.target_files)


class EvolutionProposer:
    """生成自我进化提案。

    复用 R8 的 Developer 能力（SelfCode 双 Agent）生成候选代码，但**只产出提案、
    不安装**。提案标注拟改动文件，供 EvolutionReviewer 做安全围栏检查。
    """

    def __init__(self, selfcode_manager: Any = None) -> None:
        self._sc = selfcode_manager

    async def propose(self, goal: str, *, target_files: tuple[str, ...] = ()) -> EvolutionProposal:
        """把一次「优化目标」变成 EvolutionProposal（仅提案，不落地）。"""
        pid = f"evo-{uuid.uuid4().hex[:8]}"
        rationale = f"自主检测到优化机会：{goal}"
        diff_summary = ""
        evidence = ""
        if self._sc is not None and hasattr(self._sc, "create_engineering_task"):
            try:
                task = await self._sc.create_engineering_task(goal, creator=0)
                tid = getattr(task, "id", None)
                diff_summary = f"SelfCode 候选已生成（待审批，不自动安装）：task={tid}"
                evidence = "候选经双 Agent 审查 + 沙箱自测（见 SelfCode 提案）"
            except Exception as exc:  # noqa: BLE001
                diff_summary = f"候选生成失败：{type(exc).__name__}: {exc}"
        return EvolutionProposal(
            id=pid,
            title=goal[:120],
            rationale=rationale,
            target_files=tuple(target_files),
            diff_summary=diff_summary,
            evidence=evidence,
            risk="候选作为受限运行时工具，不改动核心源码；落地前需 Owner 审批",
            rollback="安装前自动备份运行时工具目录；回滚只需删除对应文件或恢复备份",
            status="proposed",
        )


class EvolutionReviewer:
    """审查提案：静态校验 + 安全围栏（核心文件保护）。

    任何触碰 CORE_PROTECTED 的提案直接判 ``reject``（需 Owner 人工 PR），
    这是「No Hidden Authority / Production Is Sacred」的硬约束。
    """

    def review(self, proposal: EvolutionProposal) -> tuple[str, list[str]]:
        """返回 (verdict, issues)：verdict ∈ {approve, reject}。"""
        issues: list[str] = []
        if proposal.touches_core():
            issues.append(
                "提案触及受保护核心文件（%s），R9 闭环拒绝自动落地；"
                "请 Owner 走人工 PR 流程。" % ", ".join(
                    f for f in proposal.target_files if f in CORE_PROTECTED
                )
            )
            return "reject", issues
        if not proposal.diff_summary:
            issues.append("缺少改动摘要，无法评估")
            return "reject", issues
        # 非核心文件 + 有摘要 → 可进入 Owner 审批（仍不自动落地）
        return "approve", issues


class EvolutionGate:
    """Owner 审批门（fail-closed）：默认 DENY，只有显式 approve 才放行。

    注意：即便 approved，也只是「允许进入部署阶段」；真正的生产更新仍复用 R8
    的受控部署（需 owner_authorized=True 才调 deploy_fn）。R9 绝不自己改生产核心。
    """

    def __init__(self) -> None:
        self._proposals: dict[str, EvolutionProposal] = {}
        self._approved: set[str] = set()

    def register(self, proposal: EvolutionProposal) -> None:
        self._proposals[proposal.id] = proposal

    def get(self, proposal_id: str) -> EvolutionProposal | None:
        return self._proposals.get(proposal_id)

    def request_approval(self, proposal: EvolutionProposal) -> dict[str, Any]:
        """提交提案进入审批队列（默认等待 Owner 决策）。"""
        verdict, issues = EvolutionReviewer().review(proposal)
        if verdict == "reject":
            proposal.status = "rejected"
            self.register(proposal)  # 记录被拒提案，便于后续 approve 查询
            return {"id": proposal.id, "decision": "rejected", "issues": issues}
        proposal.status = "proposed"
        self.register(proposal)
        return {
            "id": proposal.id,
            "decision": "pending_owner",
            "message": "提案已就绪，等待 Owner 审批（fail-closed：默认不更新生产）",
        }

    def approve(self, proposal_id: str) -> dict[str, Any]:
        """Owner 显式批准（唯一放行路径）。"""
        p = self._proposals.get(proposal_id)
        if p is None:
            return {"id": proposal_id, "decision": "unknown"}
        if p.status == "rejected":
            return {"id": proposal_id, "decision": "rejected", "message": "已被安全围栏拒绝，需人工 PR"}
        p.status = "approved"
        self._approved.add(proposal_id)
        return {"id": proposal_id, "decision": "approved",
                "message": "已批准；可经 R8 受控部署落地（仍需 owner_authorized）"}

    def reject(self, proposal_id: str) -> dict[str, Any]:
        p = self._proposals.get(proposal_id)
        if p is None:
            return {"id": proposal_id, "decision": "unknown"}
        p.status = "rejected"
        self._approved.discard(proposal_id)
        return {"id": proposal_id, "decision": "rejected"}

    def is_approved(self, proposal_id: str) -> bool:
        return proposal_id in self._approved


class EvolutionLoop:
    """R9 受控自我进化闭环适配器：把 R9 提案接入 R8 开发闭环。

    流程：propose → review（安全围栏）→ gate（Owner 审批，fail-closed）→
    （approved 且 auto_deploy）→ 经 R8 DevLoopOrchestrator 跑闭环并受控部署。

    设计纪律：
    - 未通过安全围栏 → 直接拒绝，绝不进闭环。
    - 未获 Owner 批准 → 只生成提案，不部署。
    - 即便 approved，R8 受控部署仍需 owner_authorized=True 才调 deploy_fn。
    所以 R9 **永远无法自我授权生产更新**。
    """

    def __init__(self, dev_loop: Any, *, selfcode_manager: Any = None) -> None:
        self._dev = dev_loop
        self._proposer = EvolutionProposer(selfcode_manager)
        self._gate = EvolutionGate()

    async def propose(self, goal: str, *, target_files: tuple[str, ...] = ()) -> dict[str, Any]:
        """生成提案并进入审批队列（默认不落地）。返回门禁决策。"""
        proposal = await self._proposer.propose(goal, target_files=target_files)
        return self._gate.request_approval(proposal)

    async def evolve(self, goal: str, *, target_files: tuple[str, ...] = (),
                     auto_deploy: bool = False, owner_authorized: bool = False) -> dict[str, Any]:
        """端到端：提出 → 审查 → 门禁 →（若批准）经 R8 闭环受控部署。

        auto_deploy / owner_authorized 必须同时为真才真正部署；否则只产出提案。
        """
        proposal = await self._proposer.propose(goal, target_files=target_files)
        decision = self._gate.request_approval(proposal)
        if decision["decision"] != "pending_owner":
            return decision  # 被安全围栏拒绝

        if not (auto_deploy and owner_authorized):
            return {
                "id": proposal.id,
                "decision": "pending_owner",
                "message": "提案已就绪，等待 Owner 审批；未授权不部署（fail-closed）",
            }

        # Owner 已显式批准且授权部署 → 经 R8 闭环落地（R8 内部仍受控）
        self._gate.approve(proposal.id)
        from .devloop import DevIssue
        issue = DevIssue(
            title=goal, detail=proposal.diff_summary, source="audit",
            capability="deploy.release", required_scopes=frozenset({"write"}),
            severity="high",
        )
        result = await self._dev.run_issue(issue, owner_authorized=owner_authorized)
        return {
            "id": proposal.id,
            "decision": "approved",
            "deployed": result.deploy_ok,
            "deploy_error": result.deploy_error,
            "needs_owner_approval": result.needs_owner_approval,
        }
