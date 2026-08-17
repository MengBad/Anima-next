"""P3 — Real Agent Society 单元测试（确定性，无 LLM）。"""
from __future__ import annotations

import pytest

from anima.agent import agent_society as soc
from anima.agent.multi_agent_society import DebateForum, DebateRecord, Argument
from anima.agent.orchestrator import AgentIdentity


# ---------------------------------------------------------------------------
# 1. 角色专精发言：不同角色产出不同专业视角
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_role_argue_fn_specialized():
    fn_sec = soc.role_argue_fn("security")
    fn_arch = soc.role_argue_fn("architect")
    a = await fn_sec("要不要改权限模型", "", "against", 1)
    b = await fn_arch("要不要改权限模型", "", "neutral", 1)
    assert "安全" in a and "against" in a
    assert "架构" in b
    assert a != b  # 视角不同


# ---------------------------------------------------------------------------
# 2. 默认社会阵容：六类 Agent，Judge 持 debate.judge
# ---------------------------------------------------------------------------
def test_build_default_society_roster():
    registry, debaters, judge = soc.build_default_society()
    ids = {d.identity.agent_id for d in debaters}
    assert ids == {"architect", "security", "research", "coding", "critic"}
    # 每个 debater 都有 debate.argue 能力
    for d in debaters:
        assert d.identity.may(soc.CAP_DEBATE_ARGUE, soc.SCOPE_READ)
    # Judge 持 debate.judge
    assert judge.may_judge() is True
    assert "judge" in {a.agent_id for a in registry.list()}


# ---------------------------------------------------------------------------
# 3. 加权裁决：security against 主导 → reject（降错优先，避免多数暴政）
# ---------------------------------------------------------------------------
def test_weighted_arbitrate_security_against_dominates():
    registry, debaters, judge = soc.build_default_society()
    forum = DebateForum(registry, debaters, judge, quorum=3, rounds=1)
    # 构造记录：security/critic/architect against，research/coding for
    rec = DebateRecord(question="改 sandbox 隔离策略", context="", rounds=1)
    rec.arguments = [
        Argument("architect", "against", "x", 1),
        Argument("security", "against", "x", 1),
        Argument("research", "for", "x", 1),
        Argument("coding", "for", "x", 1),
        Argument("critic", "against", "x", 1),
    ]
    v = soc.weighted_arbitrate(forum, rec)
    # 3 against（含 security 权重2 + architect/critic 1.5）vs 2 for（权重1）→ 加权共识 < 阈值
    assert v.decision == "reject"
    assert v.weighted_consensus < 0
    assert len(v.breakdown) == 5


# ---------------------------------------------------------------------------
# 4. 加权裁决：多数 for 且无强约束反对 → support
# ---------------------------------------------------------------------------
def test_weighted_arbitrate_majority_for_supports():
    registry, debaters, judge = soc.build_default_society()
    forum = DebateForum(registry, debaters, judge, quorum=3, rounds=1)
    rec = DebateRecord(question="新增观测指标", context="", rounds=1)
    rec.arguments = [
        Argument("architect", "for", "x", 1),
        Argument("security", "for", "x", 1),      # security 不反对
        Argument("research", "for", "x", 1),
        Argument("coding", "for", "x", 1),
        Argument("critic", "neutral", "x", 1),
    ]
    v = soc.weighted_arbitrate(forum, rec)
    assert v.decision == "support"
    assert v.weighted_consensus > 0


# ---------------------------------------------------------------------------
# 5. 未达法定人数 → inconclusive（沿用 R12 fail-closed）
# ---------------------------------------------------------------------------
def test_weighted_arbitrate_quorum_inconclusive():
    registry, debaters, judge = soc.build_default_society()
    forum = DebateForum(registry, debaters, judge, quorum=3, rounds=1)
    rec = DebateRecord(question="q", context="", rounds=1)
    rec.arguments = [Argument("security", "against", "x", 1)]  # 仅 1 人 → < quorum
    v = soc.weighted_arbitrate(forum, rec)
    assert v.decision == "inconclusive"
    assert v.participated == 1


# ---------------------------------------------------------------------------
# 6. 无裁判权 → inconclusive
# ---------------------------------------------------------------------------
def test_weighted_arbitrate_no_judge_cap_inconclusive():
    # 用一个无 debate.judge 的 identity 造 Judge
    no_judge = AgentIdentity(agent_id="novice", role="novice",
                             capabilities={soc.CAP_DEBATE_ARGUE: soc.SCOPE_READ})
    from anima.agent.multi_agent_society import Judge as _Judge
    judge = _Judge(no_judge)
    registry, debaters, _ = soc.build_default_society()
    forum = DebateForum(registry, debaters, judge, quorum=3, rounds=1)
    rec = DebateRecord(question="q", context="", rounds=1)
    rec.arguments = [Argument("security", "against", "x", 1),
                     Argument("coding", "for", "x", 1),
                     Argument("research", "for", "x", 1)]
    v = soc.weighted_arbitrate(forum, rec)
    assert v.decision == "inconclusive"


# ---------------------------------------------------------------------------
# 7. 端到端便捷函数：默认社会辩论 + 加权裁决
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_run_society_debate_end_to_end():
    record, verdict = await soc.run_society_debate("是否引入自动依赖升级", "P5 自治维护场景")
    # 默认阵容中 security/critic 默认 against，故整体更可能 reject 或 inconclusive
    assert verdict.decision in ("support", "reject", "inconclusive")
    assert verdict.judge_id == "judge"
    assert len(record.arguments) >= 5  # 5 个 debater 各发言
