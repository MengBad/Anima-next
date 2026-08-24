# -*- coding: utf-8 -*-
"""B3 — 提案闭环建议字段(evidence_jury/society_review/ci_dod) 序列化与 attach tests。"""
from anima.agent.self_evolution import EvolutionProposal, EvolutionLoop, EvolutionGate


def _prop() -> EvolutionProposal:
    return EvolutionProposal(id="p1", title="t", diff_summary="d")


def test_to_dict_defaults_opinion_fields_none():
    d = _prop().to_dict()
    assert d["evidence_jury"] is None
    assert d["society_review"] is None
    assert d["ci_dod"] is None


def test_opinion_fields_roundtrip():
    p = _prop()
    p.evidence_jury = {"decision": "keep"}; p.society_review = {"available": True}
    p.ci_dod = {"pass": True, "reasons": []}
    d = p.to_dict()
    assert d["evidence_jury"]["decision"] == "keep"
    assert d["society_review"]["available"] is True
    assert d["ci_dod"]["pass"] is True


async def _mk_loop(bundle):
    class SC:
        async def create_engineering_task(self, goal, creator=0):
            class _:
                id = "t1"
            return _()
    class Dev:
        called = False
        async def run_issue(self, issue, *, owner_authorized=False):
            self.called = True
            class R:
                deploy_ok = True
                deploy_error = ""
                needs_owner_approval = False
            return R()
    gate = EvolutionGate(); dev = Dev()
    return EvolutionLoop(dev, gate=gate, core=None, selfcode_manager=SC(), ci_dod_bundle=bundle), gate, dev


async def test_evolve_blocked_attaches_ci_dod():
    import pytest
    loop, gate, dev = await _mk_loop({"test_passed": False, "frontend_ok": True, "evidence_ok": True})
    out = await loop.evolve("x", auto_deploy=True, owner_authorized=True)
    assert out["decision"] == "blocked_ci_dod"
    pid = out["id"]; p = gate.get(pid)
    assert p is not None and p.ci_dod is not None and p.ci_dod["pass"] is False
    assert dev.called is False
