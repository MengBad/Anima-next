"""R12 Multi-Agent Society 测试：多 Agent 辩论与裁判（Debate & Judge）。

验证：独立身份能力边界（debate.argue / debate.judge）、无裁判权 fail-closed 出
inconclusive、法定人数门槛、共识计分、建议性裁决不授权任何动作。纯确定性、无 LLM。
"""

import pytest

from anima.agent.multi_agent_society import (
    CAP_DEBATE_ARGUE,
    CAP_DEBATE_JUDGE,
    DebateForum,
    DebateRecord,
    Debater,
    Argument,
    Judge,
    Verdict,
    AgentIdentity,
    AgentRegistry,
    SCOPE_READ,
)


def _id(agent_id, role, caps):
    return AgentIdentity(agent_id=agent_id, role=role, capabilities=caps)


def _two_debaters():
    reg = AgentRegistry()
    a = _id("agent-debater-a", "debater", {CAP_DEBATE_ARGUE: SCOPE_READ})
    b = _id("agent-debater-b", "debater", {CAP_DEBATE_ARGUE: SCOPE_READ})
    reg.register(a); reg.register(b)
    da = Debater(a, stance="for")
    db = Debater(b, stance="against")
    return reg, da, db


def _judge(with_cap=True):
    jid = _id("agent-judge", "judge",
              {CAP_DEBATE_JUDGE: SCOPE_READ} if with_cap else {})
    return Judge(jid)


@pytest.mark.asyncio
async def test_run_debate_collects_arguments():
    reg, da, db = _two_debaters()
    forum = DebateForum(reg, [da, db], _judge(), rounds=2)
    rec = await forum.run_debate("是否开启夜间自主优化", "context-x")
    # 2 debater × 2 round = 4 条发言
    assert len(rec.arguments) == 4
    assert rec.rounds == 2
    # 发言权受能力约束（二者都有 argue 能力，皆发言）


@pytest.mark.asyncio
async def test_debater_without_argue_cap_silent():
    reg = AgentRegistry()
    a = _id("agent-debater-a", "debater", {})  # 无 argue 能力
    b = _id("agent-debater-b", "debater", {CAP_DEBATE_ARGUE: SCOPE_READ})
    reg.register(a); reg.register(b)
    da = Debater(a, stance="for"); db = Debater(b, stance="against")
    forum = DebateForum(reg, [da, db], _judge(), rounds=1)
    rec = await forum.run_debate("q")
    # 只有 b 有发言权
    assert len(rec.arguments) == 1
    assert rec.arguments[0].debater_id == "agent-debater-b"


def test_judge_without_cap_inconclusive_fail_closed():
    j = _judge(with_cap=False)
    assert j.may_judge() is False
    rec = DebateRecord(question="q", context="", rounds=1, arguments=[
        Argument(debater_id="x", stance="for", text="t", round=1),
    ])
    v = j.judge(rec)
    assert v.decision == "inconclusive"
    assert v.judge_id == "agent-judge"


@pytest.mark.asyncio
async def test_judge_support_when_for_wins():
    reg, da, db = _two_debaters()
    db.stance = "neutral"  # a=for, b=neutral → for 占优
    forum = DebateForum(reg, [da, db], _judge(), rounds=1, quorum=2)
    rec = await forum.run_debate("q")
    v = forum.arbitrate(rec)
    assert v.decision == "support"
    assert v.consensus_score > 0
    assert v.participated == 2


@pytest.mark.asyncio
async def test_judge_reject_when_against_wins():
    reg = AgentRegistry()
    a = _id("agent-debater-a", "debater", {CAP_DEBATE_ARGUE: SCOPE_READ})
    b = _id("agent-debater-b", "debater", {CAP_DEBATE_ARGUE: SCOPE_READ})
    reg.register(a); reg.register(b)
    # 两名都持 against
    da = Debater(a, stance="against"); db = Debater(b, stance="against")
    forum = DebateForum(reg, [da, db], _judge(), rounds=1, quorum=2)
    rec = await forum.run_debate("q")
    v = forum.arbitrate(rec)
    assert v.decision == "reject"
    assert v.consensus_score < 0


@pytest.mark.asyncio
async def test_quorum_not_met_inconclusive():
    reg, da, db = _two_debaters()
    # 法定人数设为 3，但只有 2 个 debater → 不足
    forum = DebateForum(reg, [da, db], _judge(), rounds=1, quorum=3)
    rec = await forum.run_debate("q")
    v = forum.arbitrate(rec)
    assert v.decision == "inconclusive"
    assert "法定人数" in v.rationale


def test_verdict_is_advisory_only():
    # 裁决对象不含任何授权/部署字段：明确仅为建议
    v = Verdict(decision="support", consensus_score=0.5, rationale="r",
                judge_id="agent-judge", participated=2)
    d = v.to_dict()
    assert set(d.keys()) == {"decision", "consensus_score", "rationale", "judge_id", "participated"}
    assert "approve" not in d and "deploy" not in d
