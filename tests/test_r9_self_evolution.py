"""R9 Controlled Self Evolution 测试。

验证：提案生成、安全围栏（核心文件保护）、fail-closed 审批门（默认 DENY）。
不真调 LLM/部署；用 mock selfcode_manager。
"""

import pytest

from anima.agent.self_evolution import (
    CORE_PROTECTED,
    EvolutionGate,
    EvolutionLoop,
    EvolutionProposer,
    EvolutionProposal,
    EvolutionReviewer,
)


class _FakeSC:
    async def create_engineering_task(self, goal, creator):
        return type("T", (), {"id": "task-mock"})()


@pytest.mark.asyncio
async def test_proposer_generates_proposal_without_install():
    """Proposer 生成提案（复用 SelfCode），不安装、不落地。"""
    p = await EvolutionProposer(_FakeSC()).propose("优化回复延迟", target_files=("anima/tools/foo.py",))
    assert isinstance(p, EvolutionProposal)
    assert p.status == "proposed"
    assert p.id.startswith("evo-")
    assert "anima/tools/foo.py" in p.target_files


def test_reviewer_rejects_core_file_touches():
    """安全围栏：提案触及核心权限/部署文件 → 直接 reject（需人工 PR）。"""
    p = EvolutionProposal(
        id="evo-x", title="t", target_files=("anima/capability/rbac.py",),
        diff_summary="改动 RBAC",
    )
    verdict, issues = EvolutionReviewer().review(p)
    assert verdict == "reject"
    assert any("受保护核心文件" in i for i in issues)
    assert "anima/capability/rbac.py" in CORE_PROTECTED


def test_reviewer_approves_non_core_proposal():
    """非核心文件提案 → approve（进入 Owner 审批队列，仍不自动落地）。"""
    p = EvolutionProposal(
        id="evo-y", title="t", target_files=("anima/tools/new_tool.py",),
        diff_summary="新增受限工具",
    )
    verdict, _ = EvolutionReviewer().review(p)
    assert verdict == "approve"


def test_gate_is_fail_closed_by_default():
    """EvolutionGate fail-closed：未审批 → is_approved=False；只有 approve 才放行。"""
    gate = EvolutionGate()
    p = EvolutionProposal(id="evo-z", title="t", target_files=("anima/tools/x.py",),
                          diff_summary="x")
    decision = gate.request_approval(p)
    assert decision["decision"] == "pending_owner"
    assert gate.is_approved("evo-z") is False      # 默认 DENY
    approved = gate.approve("evo-z")
    assert approved["decision"] == "approved"
    assert gate.is_approved("evo-z") is True        # Owner 显式批准后才放行


def test_gate_rejects_protected_proposal_even_if_approve_called():
    """被安全围栏 reject 的提案，即便调 approve 也不放行（核心保护优先）。"""
    gate = EvolutionGate()
    p = EvolutionProposal(id="evo-core", title="t",
                          target_files=("anima/agent/orchestrator.py",),
                          diff_summary="改授权逻辑")
    decision = gate.request_approval(p)
    assert decision["decision"] == "rejected"
    # 即便尝试批准，仍不被放行
    assert gate.approve("evo-core")["decision"] == "rejected"
    assert gate.is_approved("evo-core") is False


def test_gate_reject_revokes_approval():
    """reject 撤销已批准状态。"""
    gate = EvolutionGate()
    p = EvolutionProposal(id="evo-r", title="t", target_files=("anima/tools/y.py",),
                          diff_summary="y")
    gate.request_approval(p)
    gate.approve("evo-r")
    assert gate.is_approved("evo-r") is True
    gate.reject("evo-r")
    assert gate.is_approved("evo-r") is False


def _loop_handlers():
    """R8 5 阶段 handler（测试用，deploy 仅记录意图）。"""
    async def _plan(task_id, request, identity):
        return "计划"
    async def _develop(task_id, request, identity):
        return "候选"
    async def _test(task_id, request, identity):
        return "pytest 通过"
    async def _review(task_id, request, identity):
        return "提案"
    async def _deploy(task_id, request, identity):
        return "deploy 阶段执行"
    return {"planner": _plan, "developer": _develop, "tester": _test,
            "reviewer": _review, "deploy": _deploy}


@pytest.mark.asyncio
async def test_evolution_loop_fail_closed_no_deploy_without_owner():
    """EvolutionLoop.evolve：未授权 → 只提案不部署（self-improvement ≠ self-authorization）。"""
    from anima.agent.devloop import DevLoopOrchestrator, make_r8_identity_registry

    deployed = {}

    async def _deploy_fn(issue, pipeline):
        deployed["called"] = True
        return "deployed"

    dev = DevLoopOrchestrator(make_r8_identity_registry(), _loop_handlers(), deploy_fn=_deploy_fn)
    loop = EvolutionLoop(dev, selfcode_manager=_FakeSC())

    # 未授权（owner_authorized=False）→ 不部署，deploy_fn 不被调用
    res = await loop.evolve("优化回复延迟", target_files=("anima/tools/x.py",),
                            auto_deploy=True, owner_authorized=False)
    assert res["decision"] == "pending_owner"
    assert "called" not in deployed


@pytest.mark.asyncio
async def test_evolution_loop_approves_and_deploys_with_owner():
    """EvolutionLoop.evolve：Owner 批准+授权 → 经 R8 闭环部署。"""
    from anima.agent.devloop import DevLoopOrchestrator, make_r8_identity_registry

    deployed = {}

    async def _deploy_fn(issue, pipeline):
        deployed["called"] = True
        return "deployed"

    dev = DevLoopOrchestrator(make_r8_identity_registry(), _loop_handlers(), deploy_fn=_deploy_fn)
    loop = EvolutionLoop(dev, selfcode_manager=_FakeSC())

    res = await loop.evolve("优化回复延迟", target_files=("anima/tools/x.py",),
                            auto_deploy=True, owner_authorized=True)
    assert res["decision"] == "approved"
    assert deployed.get("called") is True
    assert res["deployed"] is True

