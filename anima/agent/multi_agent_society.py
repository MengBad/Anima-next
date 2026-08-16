"""R12 —— Multi-Agent Society：多 Agent 辩论与裁判（Debate & Judge）。

把 R7 的独立身份 / RBAC 能力边界复用到「多个 Agent 就同一议题辩论、由裁判 Agent 仲裁」
的社会化决策场景。**纯架构增强、不碰记忆自动写入、不自我授权任何生产动作。**

设计纪律（延续 R6–R10）：
- 每个 Debater / Judge 都是独立 ``AgentIdentity``，其发言权由 R6.3 RBAC 的 capability/scope 决定。
- Judge 必须持有 ``debate.judge`` 能力（scope=read）才允许给出裁决；否则判定 ``inconclusive``
  （fail-closed：无裁判权 → 无结论，绝不默认放行）。
- 裁判结论是**建议性**的：它不批准任何部署 / 不修改任何代码，只产出 ``Verdict`` 供上层
  （R9 审批门 / Owner）参考。self-improvement ≠ self-authorization 在此同样成立。
- 辩论内容生成可注入（``argue_fn``）；默认提供确定性桩，使整模块可单测、可复现、无需 LLM。

数据流：run_debate（多 Agent 多轮发言）→ DebateRecord → arbitrate（Judge 仲裁）→ Verdict。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .orchestrator import AgentIdentity, AgentRegistry

# R12 能力边界（沿用 R6.3 RBAC 风格）
CAP_DEBATE_ARGUE = "debate.argue"
CAP_DEBATE_JUDGE = "debate.judge"
SCOPE_READ = frozenset({"read"})


@dataclass
class Argument:
    """一条辩论发言。"""

    debater_id: str
    stance: str          # for | against | neutral
    text: str
    round: int

    def to_dict(self) -> dict[str, Any]:
        return {"debater_id": self.debater_id, "stance": self.stance,
                "text": self.text, "round": self.round}


@dataclass
class DebateRecord:
    """一次辩论的完整记录（可审计、可 replay）。"""

    question: str
    context: str
    rounds: int
    arguments: list[Argument] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "context": self.context,
            "rounds": self.rounds,
            "arguments": [a.to_dict() for a in self.arguments],
        }


@dataclass
class Verdict:
    """裁判裁决（建议性，不授权任何动作）。"""

    decision: str        # support | reject | inconclusive
    consensus_score: float
    rationale: str
    judge_id: str
    participated: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "consensus_score": round(self.consensus_score, 4),
            "rationale": self.rationale,
            "judge_id": self.judge_id,
            "participated": self.participated,
        }


class Debater:
    """一个有独立身份与立场的辩论参与者。"""

    def __init__(self, identity: AgentIdentity, stance: str,
                 argue_fn: Callable[..., Awaitable[str]] | None = None) -> None:
        self.identity = identity
        self.stance = stance
        self._argue_fn = argue_fn

    async def argue(self, question: str, context: str, round_no: int) -> str:
        """产出一条发言（可注入 LLM；默认确定性桩）。"""
        if self._argue_fn is not None:
            return await self._argue_fn(question, context, self.stance, round_no)
        # 确定性桩：用身份与立场拼出可复现陈述
        return f"[{self.identity.agent_id}] 立场={self.stance}：关于「{question}」，需评估风险与收益。"


class Judge:
    """裁判 Agent：须持有 ``debate.judge`` 能力才允许裁决。"""

    def __init__(self, identity: AgentIdentity) -> None:
        self.identity = identity

    def may_judge(self) -> bool:
        """是否持有裁判能力（fail-closed：无能力→不可裁决）。"""
        return self.identity.may(CAP_DEBATE_JUDGE, SCOPE_READ)

    def judge(self, record: DebateRecord, *, quorum: int = 2) -> Verdict:
        """基于辩论记录给出建议性裁决。无裁判权 → inconclusive。"""
        if not self.may_judge():
            return Verdict(
                decision="inconclusive", consensus_score=0.0,
                rationale="裁判 Agent 未持有 debate.judge 能力（fail-closed：无结论）。",
                judge_id=self.identity.agent_id,
            )
        # 统计各 debater 的最终立场（取其最后一条发言）
        last_by: dict[str, str] = {}
        for a in record.arguments:
            last_by[a.debater_id] = a.stance
        participated = len(last_by)
        if participated < quorum:
            return Verdict(
                decision="inconclusive", consensus_score=0.0,
                rationale=f"参与辩论的 Agent 数 {participated} < 法定人数 {quorum}，结论不成立。",
                judge_id=self.identity.agent_id, participated=participated,
            )
        for_count = sum(1 for s in last_by.values() if s == "for")
        against_count = sum(1 for s in last_by.values() if s == "against")
        neutral_count = participated - for_count - against_count
        # 共识度 = (for - against) / participated，映射到 [-1, 1]
        consensus = (for_count - against_count) / participated if participated else 0.0
        if for_count > against_count:
            decision = "support"
        elif against_count > for_count:
            decision = "reject"
        else:
            decision = "inconclusive"
        rationale = (
            f"for={for_count}, against={against_count}, neutral={neutral_count}；"
            f"共识度={consensus:+.2f}（>=0 倾向支持，<0 倾向否决）。"
        )
        return Verdict(
            decision=decision, consensus_score=consensus, rationale=rationale,
            judge_id=self.identity.agent_id, participated=participated,
        )


class DebateForum:
    """多 Agent 辩论论坛：组织辩论 + 裁判仲裁。"""

    def __init__(self, registry: AgentRegistry, debaters: list[Debater], judge: Judge,
                 *, quorum: int = 2, rounds: int = 1) -> None:
        self._registry = registry
        self._debaters = debaters
        self._judge = judge
        self.quorum = quorum
        self.rounds = rounds

    async def run_debate(self, question: str, context: str = "") -> DebateRecord:
        """组织多轮辩论，汇总为 DebateRecord。"""
        record = DebateRecord(question=question, context=context, rounds=self.rounds)
        for rnd in range(1, self.rounds + 1):
            for debater in self._debaters:
                # 发言权受 R6.3 能力边界约束（无 debate.argue 能力则跳过该发言）
                if not debater.identity.may(CAP_DEBATE_ARGUE, SCOPE_READ):
                    continue
                text = await debater.argue(question, context, rnd)
                record.arguments.append(Argument(
                    debater_id=debater.identity.agent_id, stance=debater.stance,
                    text=text, round=rnd,
                ))
        return record

    def arbitrate(self, record: DebateRecord) -> Verdict:
        """由裁判 Agent 对辩论记录给出建议性裁决。"""
        return self._judge.judge(record, quorum=self.quorum)
