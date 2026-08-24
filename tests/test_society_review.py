# -*- coding: utf-8 -*-
"""R12 评议接入 self_evolution 评审 (society_review) tests。"""
import pytest
from anima.agent.society_review import run_society_review, build_default_forum


@pytest.mark.asyncio
async def test_default_forum_returns_verdict_dict():
    out = await run_society_review("是否合并该自进化提案")
    assert out["available"] is True
    v = out["verdict"]
    assert v["judge_id"] == "judge"
    assert v["decision"] in ("support", "reject", "inconclusive")
    assert isinstance(out["arguments"], list) and len(out["arguments"]) == 3


@pytest.mark.asyncio
async def test_injected_forum_support():
    from anima.agent.multi_agent_society import DebateForum, Debater, Judge
    from anima.agent.orchestrator import AgentIdentity, AgentRegistry
    from anima.agent.multi_agent_society import CAP_DEBATE_ARGUE, CAP_DEBATE_JUDGE
    _r = frozenset({"read"}); _reg = AgentRegistry()
    ds = []
    for i, st in ((0, "for"), (1, "for"), (2, "against")):
        ident = AgentIdentity(agent_id="d" + str(i), role="reviewer", capabilities={CAP_DEBATE_ARGUE: _r})
        _reg.register(ident); ds.append(Debater(ident, st))
    jid = AgentIdentity(agent_id="j", role="judge", capabilities={CAP_DEBATE_JUDGE: _r})
    _reg.register(jid)
    forum = DebateForum(_reg, ds, Judge(jid), quorum=3, rounds=1)
    out = await run_society_review("x", forum=forum)
    assert out["verdict"]["decision"] == "support"


@pytest.mark.asyncio
async def test_fail_open_on_bad_forum():
    class _Bad:
        async def run_debate(self, *a, **k):
            raise RuntimeError("boom")
    out = await run_society_review("x", forum=_Bad())
    assert out["available"] is False
