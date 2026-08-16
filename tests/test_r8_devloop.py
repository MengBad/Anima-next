"""R8 Autonomous Development Loop 测试。

验证闭环编排 + 受控门禁（fail-closed：未授权不部署）。
使用 mock handler，不真调 LLM/pytest/部署脚本。
"""

import pytest

from anima.agent.devloop import (
    DevIssue,
    DevLoopOrchestrator,
    DevLoopResult,
    make_r8_identity_registry,
)
from anima.agent.orchestrator import STAGE_DEPLOY

pytestmark = pytest.mark.asyncio


def _make_handlers(capture: dict):
    """构造 5 阶段 mock handler；把 developer/tester/deploy 的产出记到 capture。"""
    async def _plan(task_id, request, identity):
        return "计划：修复 X"
    async def _develop(task_id, request, identity):
        capture.setdefault("develop", []).append(identity.agent_id)
        return "候选已生成（待审批）"
    async def _test(task_id, request, identity):
        capture.setdefault("test", []).append(identity.agent_id)
        return "pytest 通过"
    async def _review(task_id, request, identity):
        return "提案已收集，等待 Owner 审批"
    async def _deploy(task_id, request, identity):
        capture.setdefault("deploy", []).append(identity.agent_id)
        return "deploy 阶段执行"
    return {
        "planner": _plan,
        "developer": _develop,
        "tester": _test,
        "reviewer": _review,
        "deploy": _deploy,
    }


async def test_r8_loop_runs_all_stages_locally():
    """闭环能驱动 5 个阶段（本地 handler），并产出证据链。"""
    capture = {}
    reg = make_r8_identity_registry()
    orch = DevLoopOrchestrator(reg, _make_handlers(capture))
    issue = DevIssue(title="修复回复延迟", detail="p99 偏高", capability="deploy.release",
                     required_scopes=frozenset({"write"}))
    res = await orch.run_issue(issue, owner_authorized=True)
    assert isinstance(res, DevLoopResult)
    assert res.pipeline is not None
    # deploy.release 能力下，deploy agent 被授权 → 阶段完成
    assert res.pipeline.status == "completed"
    stages = [s.stage for s in res.pipeline.stages]
    assert stages == ["planner", "developer", "tester", "reviewer", "deploy"]
    # 证据链完整
    assert len(res.pipeline.evidence_trail()) == 5
    # developer/test/deploy handler 都被调用
    assert capture["develop"] == ["agent-developer"]
    assert capture["test"] == ["agent-tester"]
    assert capture["deploy"] == ["agent-deploy"]


async def test_r8_holds_controlled_deploy_gate_without_owner():
    """受控门禁：未授权 → 即便 Deploy Agent 被授权、deploy 阶段执行，也不调用真实部署函数。"""
    capture = {}
    calls: dict[str, bool] = {}
    async def _deploy_fn(issue, pipeline):
        calls["deploy"] = True
        return "部署成功"
    reg = make_r8_identity_registry()
    # 未注入 deploy_fn → 即使授权也不真部署；这里验证 needs_owner_approval 收口。
    orch = DevLoopOrchestrator(reg, _make_handlers(capture))
    issue = DevIssue(title="发布新功能", capability="deploy.release",
                     required_scopes=frozenset({"write"}))
    res = await orch.run_issue(issue, owner_authorized=False)
    deploy_stage = next(s for s in res.pipeline.stages if s.stage == STAGE_DEPLOY)
    assert deploy_stage.outcome == "completed"   # Deploy Agent 授权通过
    assert res.needs_owner_approval is True      # 但 R8 受控门禁收口
    assert res.deploy_attempted is False         # 未触发真实部署
    assert calls.get("deploy") is not True       # deploy_fn 绝未被调用


async def test_r8_calls_deploy_fn_when_authorized():
    """Owner 授权且有部署函数 → 真正调用 deploy_fn（部署实证）。"""
    capture = {}
    calls = {}

    async def _deploy_fn(issue, pipeline):
        calls["deploy"] = True
        return "部署成功 vX"

    reg = make_r8_identity_registry()
    orch = DevLoopOrchestrator(reg, _make_handlers(capture), deploy_fn=_deploy_fn)
    issue = DevIssue(title="上线修复", capability="deploy.release",
                     required_scopes=frozenset({"write"}))
    res = await orch.run_issue(issue, owner_authorized=True)
    assert res.needs_owner_approval is False
    assert res.deploy_attempted is True
    assert res.deploy_ok is True
    assert calls.get("deploy") is True


async def test_r8_fail_closed_when_capability_lacks_deploy():
    """非 deploy.release 能力的 issue（如 code.develop）→ deploy 阶段 fail-closed DENY。"""
    reg = make_r8_identity_registry()
    orch = DevLoopOrchestrator(reg, _make_handlers({}))
    issue = DevIssue(title="新增工具", capability="code.develop",
                     required_scopes=frozenset({"write"}))
    res = await orch.run_issue(issue, owner_authorized=True)
    # developer 有 code.develop 允许；deploy agent 只有 deploy.release → deploy 阶段 DENY
    deploy_stage = next(s for s in res.pipeline.stages if s.stage == STAGE_DEPLOY)
    assert deploy_stage.outcome == "denied"
    assert res.needs_owner_approval is True
    assert res.deploy_attempted is False


async def test_r8_evidence_records_every_stage():
    """任何阶段（含 DENY）都记录证据，符合 Audit Everything。"""
    reg = make_r8_identity_registry()
    orch = DevLoopOrchestrator(reg, _make_handlers({}))
    issue = DevIssue(title="x", capability="deploy.release",
                     required_scopes=frozenset({"write"}))
    res = await orch.run_issue(issue, owner_authorized=False)
    trail = res.pipeline.evidence_trail()
    assert len(trail) == 5
    for entry in trail:
        assert entry["evidence"]  # 每阶段都有证据（含被拒的 deploy）
        assert entry["evidence"]["outcome"] in ("completed", "denied", "allowed")
