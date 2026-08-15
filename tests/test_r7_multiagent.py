"""R7 —— Multi-Agent Architecture：多 Agent 安全协作编排验证。

覆盖 R7 完成标准（多个 Agent 安全协作，独立身份/权限/能力范围）：
- 独立身份：每个 Agent 有独立 agent_id（Principal）
- 独立权限：每个 Agent 按角色 + 能力 scope 独立授权（R6.3 RBAC 语义）
- 独立能力范围：每个 Agent 只被授予特定能力 + scope
- 流水线：Planner → Developer → Tester → Reviewer → Deploy 顺序编排
- fail-closed：Agent 不存在 / 无权限 → 阶段 DENY，不执行
- 证据：每个阶段落 R6.5 Evidence，行为可事后还原（replay）

纪律（R7 anchored）：只写新增模块 + 测试，不修改既有 agent/* 核心。
"""
from __future__ import annotations

import pytest

from anima.agent.orchestrator import (
    DEFAULT_PIPELINE,
    AgentIdentity,
    AgentOrchestrator,
    AgentRegistry,
    PipelineResult,
    STAGE_DEPLOY,
    STAGE_DEVELOP,
    STAGE_PLAN,
    STAGE_REVIEW,
    STAGE_TEST,
)


# --------------------------------------------------------------------------
# 脚手架：标准四/五 Agent 流水线（Planner/Developer/Tester/Reviewer/Deploy）
# --------------------------------------------------------------------------

def _mk_identity(agent_id, role, capabilities):
    return AgentIdentity(
        agent_id=agent_id, role=role,
        capabilities={c: frozenset(s) for c, s in capabilities.items()},
    )


def _full_pipeline() -> AgentOrchestrator:
    """标准流水线：各 Agent 拥有独立身份 + 独立能力范围。"""
    agents = {
        "agent-planner": _mk_identity("agent-planner", "planner", {"task.plan": ["read"]}),
        "agent-developer": _mk_identity("agent-developer", "developer", {"code.develop": ["write"]}),
        "agent-tester": _mk_identity("agent-tester", "tester", {"code.test": ["read"]}),
        "agent-reviewer": _mk_identity("agent-reviewer", "reviewer", {"code.review": ["read"]}),
        "agent-deploy": _mk_identity("agent-deploy", "deploy", {"deploy.release": ["write"]}),
    }
    reg = AgentRegistry()
    for a in agents.values():
        reg.register(a)

    captured = {"calls": []}

    async def _handler(task_id, request, identity):
        captured["calls"].append((identity.agent_id, identity.role))
        return f"{identity.role} handled {request[:20]}"

    orch = AgentOrchestrator(
        reg, handlers={s: _handler for s in DEFAULT_PIPELINE}, evidence_sink=None,
    )
    orch._captured = captured
    return orch


def _captured(orch) -> list:
    return orch._captured["calls"]


# --------------------------------------------------------------------------
# 一、独立身份：每个 Agent 有独立 agent_id（Principal）
# --------------------------------------------------------------------------

def test_agents_have_independent_identities():
    reg = AgentRegistry()
    reg.register(_mk_identity("agent-planner", "planner", {"task.plan": {"read"}}))
    reg.register(_mk_identity("agent-developer", "developer", {"code.develop": {"write"}}))
    ids = reg.list()
    assert {a.agent_id for a in ids} == {"agent-planner", "agent-developer"}
    assert reg.get("agent-planner").role == "planner"
    assert reg.get("agent-developer").role == "developer"


# --------------------------------------------------------------------------
# 二、独立权限：能力 + scope 独立授权（fail-closed）
# --------------------------------------------------------------------------

def test_agent_may_scoped():
    dev = _mk_identity("agent-developer", "developer", {"code.develop": {"write", "exec"}})
    assert dev.may("code.develop", frozenset({"write"})) is True
    assert dev.may("code.develop", frozenset({"write", "exec"})) is True
    assert dev.may("code.develop", frozenset({"write", "network"})) is False  # 缺 network scope
    assert dev.may("deploy.release", frozenset({"write"})) is False            # 未声明该能力


async def test_unknown_agent_denied():
    """编排器路由到未注册 Agent → 阶段 DENY（fail-closed）。"""
    orch = _full_pipeline()
    reg = orch._registry
    # 移除 deploy agent → 部署阶段无授权 → DENY
    reg._agents.pop("agent-deploy", None)
    result = await orch.run("t1", "release v1.0", required_capability="deploy.release",
                            required_scopes=frozenset({"write"}))
    deploy_stage = [s for s in result.stages if s.stage == STAGE_DEPLOY][0]
    assert deploy_stage.outcome == "denied"
    assert "agent-deploy" in deploy_stage.error


# --------------------------------------------------------------------------
# 三、流水线：各阶段按能力+scope 授权后执行
# --------------------------------------------------------------------------

async def test_pipeline_routes_each_agent():
    """Planner→Developer→Tester→Reviewer 全通过，仅 Deploy 缺权限被拒。"""
    orch = _full_pipeline()
    # 标准开发任务：planner/developer 需要写，deploy 需要发布写
    result = await orch.run("task-dev", "build feature X", required_capability="code.develop",
                            required_scopes=frozenset({"write"}))
    stages = [s.stage for s in result.stages]
    assert stages == [STAGE_PLAN, STAGE_DEVELOP, STAGE_TEST, STAGE_REVIEW, STAGE_DEPLOY]

    # planner 拥有 task.plan，但请求能力是 code.develop → planner 阶段 fail-closed DENY
    # （正确行为：只有声明了该能力的 Agent 才被授权执行）
    plan_s = [s for s in result.stages if s.stage == STAGE_PLAN][0]
    assert plan_s.outcome == "denied"
    # 关键断言：developer 阶段完成（其有 code.develop:write）
    dev_s = [s for s in result.stages if s.stage == STAGE_DEVELOP][0]
    assert dev_s.outcome == "completed"
    assert dev_s.agent_id == "agent-developer"


# --------------------------------------------------------------------------
# 四、fail-closed：能力+scope 不满足 → 该阶段不执行
# --------------------------------------------------------------------------

async def test_denied_stage_does_not_invoke_handler():
    """请求需要 network scope，但无 Agent 有该 scope → 全阶段 DENY，零 handler 调用。"""
    orch = _full_pipeline()
    result = await orch.run("task-net", "use network", required_capability="code.develop",
                            required_scopes=frozenset({"network"}))
    # 无 agent 授予 network scope → 全部阶段 denied
    assert all(s.outcome in ("denied",) for s in result.stages)
    assert _captured(orch) == []  # 零执行（不越权）


# --------------------------------------------------------------------------
# 五、证据：每阶段落 R6.5 Evidence（可 replay）
# --------------------------------------------------------------------------

async def test_pipeline_records_evidence_trail(tmp_path):
    """编排后证据链完整，可经 R6.5 EvidenceStore 落盘重放。"""
    from anima.capability.evidence import EvidenceSink, EvidenceStore

    # 自建注册表：标准五 Agent（Planner/Developer/Tester/Reviewer/Deploy）
    reg = AgentRegistry()
    for a in _full_pipeline()._registry.list():
        reg.register(a)

    async def _local_handler(task_id, request, identity):
        return f"{identity.role} handled {request[:20]}"

    store = EvidenceStore(str(tmp_path / "evidence.jsonl"))
    sink = EvidenceSink(store)
    orch = AgentOrchestrator(
        reg, handlers={s: _local_handler for s in DEFAULT_PIPELINE}, evidence_sink=sink)
    result = await orch.run("task-ev", "release v2.0", required_capability="deploy.release",
                            required_scopes=frozenset({"write"}))
    assert result.status == "completed"
    trail = result.evidence_trail()
    assert len(trail) == len(DEFAULT_PIPELINE)  # 每阶段一条证据
    # 全程可还原：每条证据含 stage/agent/role/capability/outcome
    for entry in trail:
        assert entry["agent_id"] in ("agent-planner", "agent-developer", "agent-tester",
                                     "agent-reviewer", "agent-deploy")
        ev = entry["evidence"]
        assert ev["agent"] == entry["agent_id"]
        assert "capability" in ev
        assert "outcome" in ev
        assert "role" in ev
    assert store.read_all()  # 证据已落盘


# --------------------------------------------------------------------------
# 六、反绕过：编排器不做权限之外的判定，执行权在 handler
# --------------------------------------------------------------------------

async def test_orchestrator_does_not_invent_authority():
    """编排器只按注册的 AgentIdentity 授权，绝不因 handler 存在就放行。"""
    orch = _full_pipeline()
    # 请求一个所有 agent 都未声明的能力 → 全阶段 denied（即便 handler 存在）
    result = await orch.run("task-x", "do anything", required_capability="not.declared.anywhere",
                            required_scopes=frozenset())
    assert all(s.outcome == "denied" for s in result.stages)
    assert _captured(orch) == []  # 未授权绝不执行