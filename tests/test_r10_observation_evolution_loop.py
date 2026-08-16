"""R10 工程化自治闭环收口测试：Observation → Evolution → Evaluation 单一可观测闭环。

验证默认不部署（dry_run）与 Owner 授权部署两种模式，且闭环只产出提案与证据、
绝不自我授权生产更新（fail-closed）。
"""

import pytest
import tempfile

from anima.agent.self_evolution import EvolutionLoop
from anima.evolution.orchestrator import ObservationOptimizationLoop


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


@pytest.mark.asyncio
async def test_observation_loop_dry_run_produces_proposals_not_deployed():
    """默认 dry_run：观测到反馈 → 产出自进化提案，但不部署（fail-closed）。"""
    from anima.agent.devloop import DevLoopOrchestrator, make_r8_identity_registry

    tmp_data_dir = tempfile.mkdtemp(prefix="anima-loop-dry-")
    deployed = {}

    async def _deploy_fn(issue, pipeline):
        deployed["called"] = True
        return "deployed"

    core = _make_core(tmp_data_dir)
    dev = _make_dev(_deploy_fn)
    loop = ObservationOptimizationLoop(
        dev, selfcode_manager=_FakeSC(), core=core, dry_run=True,
    )

    feedback = [{"title": "回复有bug", "detail": "用户反馈回复错误", "target": "llm"}]
    report = await loop.run_cycle(feedback_queue=feedback, owner_authorized=False)

    assert report["ok"] is True
    assert report["mode"] == "dry_run"
    assert len(report["issues"]) >= 1
    assert len(report["proposals"]) >= 1
    # dry_run 不部署
    assert deployed.get("called") is None
    # 闭环明确声明不自我授权
    assert "绝不自我授权" in report["note"]
    # 提案默认不落地（pending_owner）
    for p in report["proposals"]:
        assert p["decision"] in ("pending_owner", "approved")


@pytest.mark.asyncio
async def test_observation_loop_owner_authorized_deploys_with_evidence():
    """owner_authorized + 非 dry_run：走 R9 受控部署，并产出 Before/After 改进证据。"""
    from anima.agent.devloop import DevLoopOrchestrator, make_r8_identity_registry

    tmp_data_dir = tempfile.mkdtemp(prefix="anima-loop-dep-")
    deployed = {}

    async def _deploy_fn(issue, pipeline):
        deployed["called"] = True
        return "deployed"

    core = _make_core(tmp_data_dir)
    dev = _make_dev(_deploy_fn)
    loop = ObservationOptimizationLoop(
        dev, selfcode_manager=_FakeSC(), core=core, dry_run=False,
    )

    feedback = [{"title": "回复有bug", "detail": "用户反馈回复错误", "target": "llm"}]
    report = await loop.run_cycle(feedback_queue=feedback, owner_authorized=True)

    assert report["mode"] == "deployed"
    assert deployed.get("called") is True
    assert len(report["proposals"]) >= 1
    # 至少一条提案经受控部署产出改进证据
    any_evidence = any(
        p.get("improvement_evidence") is not None for p in report["proposals"]
    )
    assert any_evidence is True
    # 证据聚合结论存在
    assert "evidence_summary" in report
    assert report["evidence_summary"]["total_metrics"] >= 1
