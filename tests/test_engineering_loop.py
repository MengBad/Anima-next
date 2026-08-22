# -*- coding: utf-8 -*-
"""R19 —— 自主工程闭环快照（Engineering Loop，提案闭环）单元测试。

验证 build_engineering_loop：
- 只读聚合：观测 / 提案 / 演化 / 自评 四路信号归一为闭环快照；
- 提案闭环：绝不执行，可行动项只在 proposed_issues / recommendations；
- fail-closed：信号缺失 -> unavailable 诚实记录，绝不编造；
- HumanAIControlSurface.engineering_loop 在 core 缺失时 fail-closed。
"""
from __future__ import annotations

from unittest import mock

from anima.observation.engineering_loop import (
    EngineeringLoop,
    build_engineering_loop,
)


class _FakeIssue:
    def __init__(self, title):
        self.title = title
        self.detail = ""
        self.source = "self_monitor"
        self.capability = "code.develop"
        self.required_scopes = frozenset({"write"})
        self.severity = "high"

    def to_dict(self):
        return {"title": self.title, "source": self.source, "severity": self.severity}


def _full_signals():
    return dict(
        observation_events=[{"type": "runtime_error", "severity": "high"}],
        proposed_issues=[_FakeIssue("修复空回复")],
        evolution_proposals=[{"id": "p1", "status": "pending_owner"}],
        self_evaluation={"overall": 0.8, "axes": []},
    )


def test_build_loop_all_signals_ok():
    loop = build_engineering_loop(**_full_signals(), now=1000.0)
    assert isinstance(loop, EngineeringLoop)
    assert loop.generated_at == 1000.0
    assert loop.observed_count == 1
    assert loop.pending_owner_count == 1
    assert len(loop.proposed_issues) == 1
    assert loop.self_evaluation == {"overall": 0.8, "axes": []}
    # observe / plan 阶段由注入信号标记覆盖
    assert loop.stages["observe"] is True
    assert loop.stages["plan"] is True


def test_build_loop_no_evolution_pending_when_empty():
    sig = _full_signals()
    sig["evolution_proposals"] = []
    loop = build_engineering_loop(**sig)
    assert loop.pending_owner_count == 0


def test_build_loop_evolution_pending_only_counts_pending_owner():
    sig = _full_signals()
    sig["evolution_proposals"] = [
        {"id": "p1", "status": "pending_owner"},
        {"id": "p2", "status": "approved"},
        {"id": "p3", "decision": "rejected"},
    ]
    loop = build_engineering_loop(**sig)
    assert loop.pending_owner_count == 1


def test_build_loop_no_core_marks_sources_unavailable():
    loop = build_engineering_loop(core=None)
    assert "observation" in loop.unavailable
    assert "proposals" in loop.unavailable
    assert "self_evaluation" in loop.unavailable
    assert loop.loop_health in ("degraded", "unknown")


def test_build_loop_proposed_issues_serialized():
    loop = build_engineering_loop(**_full_signals())
    assert loop.proposed_issues[0]["title"] == "修复空回复"


def test_build_loop_recommendation_on_pending_owner():
    loop = build_engineering_loop(**_full_signals())
    recs = " ".join(loop.recommendations)
    assert "pending_owner" in recs or "Owner" in recs


def test_build_loop_stages_cover_evidence_module():
    # 不依赖 core，纯注入信号；evidence 阶段依据模块探测（anima.evolution.evaluator）。
    loop = build_engineering_loop(**_full_signals())
    # evidence 阶段是否覆盖取决于模块是否存在，这里只断言是 bool 且不抛错。
    assert isinstance(loop.stages["evidence"], bool)


# ----------------------------------------------------------------------
# 控制面集成（fail-closed / 不自我授权）
# ----------------------------------------------------------------------

def test_control_surface_engineering_loop_core_missing():
    from anima.os.control_surface import HumanAIControlSurface
    surf = HumanAIControlSurface(None)
    out = surf.engineering_loop()
    assert out.get("available") is False
    assert "core" in out.get("reason", "")


def test_control_surface_engineering_loop_read_only():
    from anima.os.control_surface import HumanAIControlSurface
    core = mock.Mock()
    core.audit = None
    surf = HumanAIControlSurface(core)
    with mock.patch(
        "anima.observation.engineering_loop.build_engineering_loop",
        return_value=EngineeringLoop(
            generated_at=1.0, loop_health="ok",
            stages={s: True for s in ("observe", "plan", "implement", "test", "review", "deploy", "evidence")},
            observed_count=0, proposed_issues=[], pending_owner_count=0,
            self_evaluation={"overall": 0.9}, unavailable=[], recommendations=[],
        ),
    ):
        out = surf.engineering_loop()
    assert out.get("loop_health") == "ok"
    assert out.get("pending_owner_count") == 0
    # 只陈述、不执行：结果只有状态字段，无任何 write/execute 动作。
    assert "execute" not in out and "deploy_attempted" not in out
