"""R15 — ANIMA OS Final：系统级端到端验收。

把 R6–R14 的能力串成一条**可验证的端到端闭环**，证明 ANIMA 已成为：
  - 长期运行（R13 健康与受控恢复）
  - 自我验证（R10-00 CI + R10-02 改进证据）
  - 自我评估（R10-01 观测 + R10-02 评估）
  - 自我优化（R9 受控进化 + R11 记忆/技能演化）
  - 人机协同（R14 控制面，绝不自我授权）

验收断言（全部 fail-closed、纯确定性、无 LLM）：
  1. Observation（R10-01）→ 把用户反馈变成可观测事件与 DevIssue；
  2. Evolution（R9/R11）→ 经受控门禁产出提案（默认不部署）；
  3. Evaluation（R10-02）→ 部署前后产出改进证据并落盘；
  4. Multi-Agent（R12）→ 裁判裁决为建议性、fail-closed；
  5. Autonomous Operation（R13）→ 健康降级产出需 Owner 的恢复计划；
  6. Human-AI OS（R14）→ 控制面聚合全景、特权指令只指向既有授权端点；
  7. 全链路不自我授权任何生产动作（self-improvement ≠ self-authorization）。
"""

import pytest
import tempfile


# ---------------------------------------------------------------------------
# 复用既有 mock 工厂（与 R9/R10/R11/R14 测试同口径）
# ---------------------------------------------------------------------------
def _loop_handlers():
    async def _plan(task_id, request, identity):
        return "计划"
    async def _develop(task_id, request, identity):
        return "代码"
    async def _test(task_id, request, identity):
        return "测试通过"
    async def _review(task_id, request, identity):
        return "提案"
    async def _deploy(task_id, request, identity):
        return "deploy 阶段执行"
    return {"planner": _plan, "developer": _develop, "tester": _test,
            "reviewer": _review, "deploy": _deploy}


class _FakeSC:
    async def create_engineering_task(self, goal, creator):
        return type("T", (), {"id": "task-mock"})()


def _make_core(tmp_data_dir, fail=5):
    class _Core:
        def __init__(self):
            class _Ch:
                def health(self):
                    return {"connected": True}
            self.gateway = type("G", (), {"channel": _Ch()})()

            class _LLM:
                def health(self):
                    return {"request_count": 100, "failure_count": fail}
            self.llm = _LLM()

            class _Hist:
                def health(self):
                    return {"samples": 50}
            self.health_history = _Hist()

            class _WF:
                def summary(self):
                    return {"active": 0}
            self.workflows = _WF()

            class _Persist:
                data_dir = tmp_data_dir
            self.config = type("Cfg", (), {"persistence": _Persist()})()
    return _Core()


def _make_dev(deploy_fn):
    from anima.agent.devloop import DevLoopOrchestrator, make_r8_identity_registry
    return DevLoopOrchestrator(make_r8_identity_registry(), _loop_handlers(), deploy_fn=deploy_fn)


# ---------------------------------------------------------------------------
# R15 端到端验收
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_end_to_end_autonomous_os_loop():
    """完整闭环：观测用户反馈 → 受控进化提案 → 评估证据 → 控制面聚合。"""
    deployed = {}

    async def _deploy_fn(issue, pipeline):
        deployed["called"] = True
        return "deployed"

    tmp = tempfile.mkdtemp(prefix="anima-r15-")
    core = _make_core(tmp)
    dev = _make_dev(_deploy_fn)

    # --- R10-03 可观测闭环（dry_run 默认不部署）---
    from anima.evolution.orchestrator import ObservationOptimizationLoop
    loop = ObservationOptimizationLoop(dev, selfcode_manager=_FakeSC(), core=core, dry_run=True)
    feedback = [{"title": "回复有bug", "detail": "用户反馈回复错误", "target": "llm"}]
    report = await loop.run_cycle(feedback_queue=feedback, owner_authorized=False)

    # 1) Observation：收到反馈并产生 Issue
    assert report["ok"] is True
    assert len(report["issues"]) >= 1
    # 2) Evolution（R9/R11）：dry_run 不部署
    assert report["mode"] == "dry_run"
    assert deployed.get("called") is None
    assert len(report["proposals"]) >= 1
    for p in report["proposals"]:
        assert p["decision"] in ("pending_owner", "approved")
    # 3) 闭环声明不自我授权
    assert "绝不自我授权" in report["note"]

    # --- R14 控制面聚合全景（含本闭环的提案）---
    from anima.os.control_surface import HumanAIControlSurface
    surf = HumanAIControlSurface()
    ov = surf.overview(
        health={"snapshot": {"runtime_running": 1.0}, "degraded": False, "plans": []},
        recovery_plans=[],
        evolution_proposals=report["proposals"],
        observation_events=len(report["observed_events"]),
    )
    assert ov.degraded is False
    assert ov.observation_events == len(report["observed_events"])
    assert len(ov.evolution_proposals) == len(report["proposals"])

    # --- R14 受限指令：批准只指向既有 Owner 授权端点，绝不自我授权 ---
    cmd = surf.parse_command(f"批准提案 {report['proposals'][0]['id']}")
    assert cmd.intent == "approve"
    assert cmd.needs_confirmation is True
    assert cmd.confirmation_endpoint.startswith("POST /api/evolution/")
    d = cmd.to_dict()
    assert d["needs_confirmation"] is True


@pytest.mark.asyncio
async def test_failed_deploy_yields_eval_evidence_and_recovery_plan():
    """失败部署后：R10-02 仍产出改进证据；R13 检测降级并给出需 Owner 的恢复计划。"""
    async def _deploy_fn(issue, pipeline):
        raise RuntimeError("simulated deploy failure")

    tmp = tempfile.mkdtemp(prefix="anima-r15-deploy-")
    core = _make_core(tmp)
    dev = _make_dev(_deploy_fn)

    from anima.evolution.orchestrator import ObservationOptimizationLoop
    loop = ObservationOptimizationLoop(dev, selfcode_manager=_FakeSC(), core=core, dry_run=False)
    feedback = [{"title": "回复有bug", "detail": "用户反馈回复错误", "target": "llm"}]
    # owner_authorized=True 触发真实部署（此处故意失败）
    report = await loop.run_cycle(feedback_queue=feedback, owner_authorized=True)
    # 部署失败，但 R10-02 评估仍在部署前/后采集并产出证据（fail-open）
    assert report["mode"] == "deployed"
    assert any(p.get("improvement_evidence") is not None for p in report["proposals"])

    # R13：运行时已宕（running=0） → 关键恢复计划需 Owner
    from anima.agent.autonomous_operation import AutonomousOperationController
    op = AutonomousOperationController()
    health = op.evaluate(snapshot={"runtime_running": 0.0, "channel_connected": 0.0,
                                   "llm_failure_rate": 0.0, "automation_pending": 0.0})
    assert health.degraded is True
    actions = {p.action for p in health.plans}
    assert "restart_runtime" in actions
    assert all(p.requires_owner for p in health.plans if p.action == "restart_runtime")


@pytest.mark.asyncio
async def test_multi_agent_judge_is_advisory_only():
    """R12：裁判裁决为建议性；无裁判权 → inconclusive（fail-closed）。"""
    from anima.agent.multi_agent_society import (
        AgentIdentity, AgentRegistry, Debater, Judge, DebateForum, CAP_DEBATE_ARGUE,
        CAP_DEBATE_JUDGE, SCOPE_READ,
    )

    reg = AgentRegistry()
    a = AgentIdentity(agent_id="agent-debater-a", role="debater",
                      capabilities={CAP_DEBATE_ARGUE: SCOPE_READ})
    b = AgentIdentity(agent_id="agent-debater-b", role="debater",
                      capabilities={CAP_DEBATE_ARGUE: SCOPE_READ})
    reg.register(a); reg.register(b)
    da, db = Debater(a, "for"), Debater(b, "against")
    # 无裁判能力的 judge
    jno = Judge(AgentIdentity(agent_id="agent-judge", role="judge", capabilities={}))
    forum = DebateForum(reg, [da, db], jno, rounds=1, quorum=2)
    rec = await forum.run_debate("q")
    v = forum.arbitrate(rec)
    assert v.decision == "inconclusive"  # fail-closed：无裁判权
    # Verdict 不含任何授权/部署字段
    assert set(v.to_dict()).issubset({"decision", "consensus_score", "rationale", "judge_id", "participated"})


def test_objective_capabilities_all_present():
    """验收：目标所需的几大能力类均已实现（导入存在性）。"""
    import importlib
    mods = [
        "anima.agent.self_evolution",       # R9 自我优化
        "anima.observation.collector",      # R10-01 自我评估（观测）
        "anima.evolution.evaluator",         # R10-02 改进证据（评估）
        "anima.evolution.orchestrator",      # R10-03 闭环
        "anima.agent.memory_evolution",      # R11 记忆/技能演化（优化）
        "anima.agent.multi_agent_society",   # R12 多 Agent 社会
        "anima.agent.autonomous_operation",  # R13 自治运维（长期运行）
        "anima.os.control_surface",          # R14 人机控制面
    ]
    for m in mods:
        importlib.import_module(m)
    assert True
