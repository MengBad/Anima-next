"""R10-02 Evolution Evaluation Engine 测试。

验证 Before/After 度量、改进/退化判定、基准编排、聚合结论。纯函数/mock，不依赖 LLM/网络。
"""

import pytest
import tempfile
from pathlib import Path

from anima.evolution.evaluator import (
    Benchmark,
    ImprovementEvidence,
    MetricSample,
    collect_metrics,
    compare_metric,
    compare_samples,
    summarize_verdict,
)
from anima.agent.self_evolution import EvolutionLoop


class _Core:
    """最小 core mock，提供 collect_metrics 所需的健康/评估接口。"""

    def __init__(self, running=True, connected=True, req=100, fail=10, samples=50, pending=0):
        self._running = running

        class _Ch:
            def health(self):
                return {"connected": connected}
        self.gateway = type("G", (), {"channel": _Ch()})()

        class _LLM:
            def health(self):
                return {"request_count": req, "failure_count": fail}
        self.llm = _LLM()

        class _Hist:
            def health(self):
                return {"samples": samples}
        self.health_history = _Hist()

        class _WF:
            def summary(self):
                return {"active": pending}
        self.workflows = _WF()


def test_collect_metrics_basic():
    m = collect_metrics(_Core(), context="before")
    assert m.context == "before"
    assert m.metrics["runtime_running"] == 1.0
    assert m.metrics["channel_connected"] == 1.0
    assert m.metrics["llm_request_count"] == 100.0
    assert m.metrics["llm_failure_count"] == 10.0
    assert abs(m.metrics["llm_failure_rate"] - 0.1) < 1e-9
    assert m.metrics["health_samples"] == 50.0


def test_compare_metric_higher_better_improved():
    ev = compare_metric("health_samples", 50.0, 80.0)
    assert ev.direction == "improved"
    assert ev.verdict == "effective"
    assert ev.delta == 30.0


def test_compare_metric_lower_better_improved():
    # 失败率下降 = 改进（lower_better 语义）
    ev = compare_metric("llm_failure_rate", 0.2, 0.05)
    assert ev.direction == "improved"
    assert ev.verdict == "effective"


def test_compare_metric_regressed():
    ev = compare_metric("channel_connected", 1.0, 0.0)
    assert ev.direction == "regressed"
    assert ev.verdict == "harmful"


def test_compare_metric_neutral_ineffective():
    ev = compare_metric("runtime_running", 1.0, 1.0)
    assert ev.direction == "neutral"
    assert ev.verdict == "ineffective"


def test_compare_samples_only_common_keys():
    before = {"a": 1.0, "b": 2.0}
    after = {"a": 5.0}  # b 缺失，不应对比
    evs = compare_samples(before, after)
    assert len(evs) == 1
    assert evs[0].metric_key == "a"


def test_summarize_verdict_harmful_priority():
    evs = [
        ImprovementEvidence("x", 1, 2, direction="improved", verdict="effective"),
        ImprovementEvidence("y", 1, 0, direction="regressed", verdict="harmful"),
    ]
    s = summarize_verdict(evs)
    assert s["overall"] == "harmful"
    assert s["effective"] == 1 and s["harmful"] == 1


def test_benchmark_capture_before_after():
    b = Benchmark()
    b.capture_before(_Core(req=100, fail=20))  # 失败率 0.2
    report = b.capture_after(_Core(req=100, fail=5))  # 失败率 0.05
    assert report["ok"] is True
    assert any(e["metric_key"] == "llm_failure_rate" and e["verdict"] == "effective"
               for e in report["evidence"])
    assert report["summary"]["overall"] in ("effective", "ineffective")


def test_benchmark_missing_before():
    b = Benchmark()
    report = b.capture_after(_Core())
    assert report["ok"] is False
    assert report["reason"] == "no_before_snapshot"


def test_benchmark_evaluate_pair_offline():
    b = Benchmark()
    report = b.evaluate_pair({"health_samples": 10.0}, {"health_samples": 40.0})
    assert report["ok"] is True
    assert report["summary"]["overall"] == "effective"
    assert report["evidence"][0]["direction"] == "improved"


def test_benchmark_regression_detected():
    b = Benchmark()
    # 部署后失败率反而上升 → harmful
    report = b.evaluate_pair(
        {"llm_failure_rate": 0.1}, {"llm_failure_rate": 0.3}
    )
    assert report["summary"]["overall"] == "harmful"


def _loop_handlers():
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
async def test_evolution_loop_produces_improvement_evidence_with_core():
    """EvolutionLoop.evolve 持有 core 时，部署后产出 improvement_evidence 并落盘。"""
    from anima.agent.devloop import DevLoopOrchestrator, make_r8_identity_registry

    tmp_data_dir = tempfile.mkdtemp(prefix="anima-evo-evidence-")

    deployed = {}

    async def _deploy_fn(issue, pipeline):
        deployed["called"] = True
        return "deployed"

    # core mock：Before/After 采集所需接口；这里让 After 失败率更低以体现改进
    class _Core:
        def __init__(self, fail):
            self._fail = fail

            class _Ch:
                def health(self):
                    return {"connected": True}
            self.gateway = type("G", (), {"channel": _Ch()})()

            class _LLM:
                def health(self):
                    return {"request_count": 100, "failure_count": self._fail}
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

    # core mock：Before/After 采集所需接口（部署后失败率由 fail 决定）
    core = _Core(fail=5)

    class _FakeSC:
        async def create_engineering_task(self, goal, creator):
            return type("T", (), {"id": "task-mock"})()

    dev = DevLoopOrchestrator(make_r8_identity_registry(), _loop_handlers(), deploy_fn=_deploy_fn)
    loop = EvolutionLoop(dev, selfcode_manager=_FakeSC(), core=core)
    # evolve 内部持 core，部署前后自动采集 Before/After 并产出 ImprovementEvidence
    res = await loop.evolve("优化回复延迟", target_files=("anima/tools/x.py",),
                            auto_deploy=True, owner_authorized=True)
    assert res["decision"] == "approved"
    assert deployed.get("called") is True
    # 即便 Before/After 同值（neutral），evolve 仍应产出 improvement_evidence 字段
    assert "improvement_evidence" in res
    ie = res["improvement_evidence"]
    assert ie is not None
    assert "evidence" in ie and "summary" in ie
