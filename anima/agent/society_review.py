"""R12 评审适配 —— 多 Agent 评议进自进化评审(建议性, fail-closed)。

确定性论坛(不需 LLM): 3 个独立身份的 Debater(for/against/neutral) + Judge,
立场投票裁决, 产出 Verdict 供 Owner/上层参考。纯建议, 不授权任何动作。
"""
from __future__ import annotations

from typing import Any

from .multi_agent_society import (
    CAP_DEBATE_ARGUE, CAP_DEBATE_JUDGE, DebateForum, Debater, Judge,
)
from .orchestrator import AgentIdentity, AgentRegistry

_READ = frozenset({"read"})


def build_default_forum() -> DebateForum:
    """构造确定性论坛: for/against/neutral 各一 + judge。"""
    reg = AgentRegistry()
    debaters = []
    for aid, stance in (("reviewer_for", "for"), ("reviewer_against", "against"), ("reviewer_neutral", "neutral")):
        ident = AgentIdentity(agent_id=aid, role="reviewer", capabilities={CAP_DEBATE_ARGUE: _READ})
        reg.register(ident)
        debaters.append(Debater(ident, stance))
    judge_ident = AgentIdentity(agent_id="judge", role="judge", capabilities={CAP_DEBATE_JUDGE: _READ})
    reg.register(judge_ident)
    return DebateForum(reg, debaters, Judge(judge_ident), quorum=3, rounds=1)


async def run_society_review(goal: str, context: str = "", *, forum=None) -> dict:
    """对一次自进化目标跑多 Agent 评议, 返回建议性裁决 dict。

    forum 缺省用确定性论坛; 构造/辩论失败返回 available=False(fail-open), 不抛。
    """
    try:
        f = forum if forum is not None else build_default_forum()
        record = await f.run_debate(goal, context)
        verdict = f.arbitrate(record)
        return {
            "available": True,
            "verdict": verdict.to_dict(),
            "arguments": [a.to_dict() for a in record.arguments],
        }
    except Exception as exc:  # noqa: BLE001 - 评议失败只隔离
        return {"available": False, "reason": type(exc).__name__}
