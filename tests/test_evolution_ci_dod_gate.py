# -*- coding: utf-8 -*-
"""EvolveLoop CI DoD 落地门 (fail-closed) tests。"""
import pytest
from anima.agent.self_evolution import EvolutionLoop, EvolutionGate


class _SC:
    """产出候选(非空 diff_summary)的 selfcode_manager fake, 使提案过 Reviewer。"""
    async def create_engineering_task(self, goal, creator=0):
        class _:
            id = "t1"
        return _()


class _Dev:
    def __init__(self):
        self.called = False
    async def run_issue(self, issue, *, owner_authorized=False):
        self.called = True
        class _R:
            deploy_ok = False
            deploy_error = ""
            needs_owner_approval = False
        return _R()


@pytest.mark.asyncio
async def test_ci_dod_block_prevents_deploy():
    gate = EvolutionGate(); dev = _Dev()
    loop = EvolutionLoop(dev, gate=gate, core=None, selfcode_manager=_SC(),
                         ci_dod_bundle={"test_passed": False, "frontend_ok": True, "evidence_ok": True})
    out = await loop.evolve("x", auto_deploy=True, owner_authorized=True)
    assert out.get("decision") == "blocked_ci_dod"
    assert dev.called is False          # 未触碰部署闭环
    assert out["ci_dod"]["pass"] is False


@pytest.mark.asyncio
async def test_no_bundle_proceeds_unchanged():
    gate = EvolutionGate(); dev = _Dev()
    loop = EvolutionLoop(dev, gate=gate, core=None, selfcode_manager=_SC(), ci_dod_bundle=None)
    out = await loop.evolve("x", auto_deploy=True, owner_authorized=True)
    assert out.get("decision") == "approved"
    assert dev.called is True


@pytest.mark.asyncio
async def test_unapproved_not_deploy_still():
    gate = EvolutionGate(); dev = _Dev()
    loop = EvolutionLoop(dev, gate=gate, core=None, selfcode_manager=_SC())
    out = await loop.evolve("x", auto_deploy=False, owner_authorized=True)
    assert out.get("decision") == "pending_owner"
    assert dev.called is False
