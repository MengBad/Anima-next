"""P3 —— Real Agent Society（真实多 Agent 协作，多视角降错）。

在 R12 辩论框架之上，把「确定性桩 argue_fn」升级为 **角色专精的多视角发言**：
Architect / Security / Research / Coding / Critic / Judge 六类 Agent 各自从自身专业立场
评估同一议题，从而**多角度暴露不同错误模式**（设计缺陷 / 安全风险 / 证据不足 / 实现陷阱 /
审查遗漏），显著降低单视角盲点。

设计纪律（延续 R6–R15 / P0–P2，尤其 No Hidden Authority / Audit Everything）：
- 复用 R12 的 ``Debater`` / ``Judge`` / ``DebateForum`` 与 R6.3 RBAC 能力边界：
  发言需 ``debate.argue``、裁决需 ``debate.judge``，无能力则跳过/``inconclusive``。
- 裁判结论是**建议性**的，不批准任何部署、不修改任何代码；只供 R9 审批门 / Owner 参考。
- 多视角加权：安全/架构等强约束视角的 ``against`` 权重更高（降错优先），避免多数暴政
  把风险提案放行。
- 全部确定性、可单测、无需 LLM（真实接线时 `argue_fn` 可由 LLM 注入，但不改变框架纪律）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .multi_agent_society import (
    CAP_DEBATE_ARGUE,
    CAP_DEBATE_JUDGE,
    DebateForum,
    DebateRecord,
    Judge,
    SCOPE_READ,
    Verdict,
    Debater,
)
from .orchestrator import AgentIdentity, AgentRegistry, make_agent_registry


# ---------------------------------------------------------------------------
# 角色专精的多视角发言（P3 核心：把 R12 桩升级为专业立场）
# ---------------------------------------------------------------------------
# 各角色的发言模板：从自身专业视角评估「该不该做 / 怎么做更稳」。
_ROLE_PERSPECTIVES: dict[str, str] = {
    "architect": (
        "架构视角：评估方案的整体设计一致性、可维护性与长期演进成本。"
        "关注是否引入耦合、是否偏离既有模块边界、是否可被后续自治闭环安全消费。"
    ),
    "security": (
        "安全视角：评估权限边界、是否触碰 CORE_PROTECTED、是否有隐藏授权路径、"
        "审计是否完整。任何涉及生产/权限/安全的改动默认从严，fail-closed。"
    ),
    "research": (
        "研究视角：评估是否有历史证据支撑该方向（参考演化影响评分与观测趋势），"
        "是否已有更优先例，避免重复造轮或凭直觉决策。"
    ),
    "coding": (
        "实现视角：评估改动的可测试性、回滚路径、对运行态的影响面，"
        "以及是否落入已知脆弱模块（如沙箱/工具注册/持久化）。"
    ),
    "critic": (
        "批判视角：主动寻找论证漏洞、未考虑的失败模式、被忽略的副作用，"
        "对乐观结论保持怀疑，要求证据而非承诺。"
    ),
    "judge": (
        "裁判视角：综合各方立场与证据，给出建议性裁决（不授权任何执行）。"
    ),
}


def role_argue_fn(role: str) -> Callable[..., Awaitable[str]]:
    """返回一个按角色生成专业发言的 argue_fn（确定性、可复现、无需 LLM）。

    真实部署时可由 LLM 替换本函数（签名相同），但不改变 P3 的框架纪律。
    """
    perspective = _ROLE_PERSPECTIVES.get(role, "通用视角：从自身立场评估该议题。")

    async def _fn(question: str, context: str, stance: str, round_no: int) -> str:
        base = f"[{role}] 立场={stance}：{perspective}"
        if context:
            base += f" 上下文摘要：{context[:80]}"
        base += f" 议题：「{question}」"
        return base

    return _fn


# 角色 → 默认立场（可覆盖）；强约束角色默认带审视倾向，避免盲目支持。
_ROLE_DEFAULT_STANCE: dict[str, str] = {
    "security": "against",   # 安全默认从严
    "critic": "against",     # 批判默认挑刺
    "architect": "neutral",
    "research": "neutral",
    "coding": "neutral",
    "judge": "neutral",
}


# 视角权重：强约束视角（安全/架构/批判）的 against 在裁决中权重更高（降错优先）。
_PERSPECTIVE_WEIGHT: dict[str, float] = {
    "security": 2.0,
    "architect": 1.5,
    "critic": 1.5,
    "research": 1.0,
    "coding": 1.0,
    "judge": 1.0,
}


# ---------------------------------------------------------------------------
# 默认社会阵容（六类 Agent，正确 RBAC 能力边界）
# ---------------------------------------------------------------------------
def _make_identity(agent_id: str, role: str, *, is_judge: bool = False) -> AgentIdentity:
    caps = {CAP_DEBATE_ARGUE: SCOPE_READ}
    if is_judge:
        caps[CAP_DEBATE_JUDGE] = SCOPE_READ
    return AgentIdentity(agent_id=agent_id, role=role, capabilities=caps)


def build_default_society() -> tuple[AgentRegistry, list[Debater], Judge]:
    """构建默认真实 Agent 社会：Architect/Security/Research/Coding/Critic + Judge。

    返回 (registry, debaters, judge)，可直接喂给 ``DebateForum``。
    每个 Debater 用 ``role_argue_fn`` 注入专业发言；Judge 持有 ``debate.judge``。
    """
    roster = {
        "architect": "architect",
        "security": "security",
        "research": "research",
        "coding": "coding",
        "critic": "critic",
    }
    identities = {aid: _make_identity(aid, role) for aid, role in roster.items()}
    registry = make_agent_registry(identities)
    debaters = [
        Debater(identities[aid], stance=_ROLE_DEFAULT_STANCE[role],
                argue_fn=role_argue_fn(role))
        for aid, role in roster.items()
    ]
    judge_identity = _make_identity("judge", "judge", is_judge=True)
    registry.register(judge_identity)
    judge = Judge(judge_identity)
    return registry, debaters, judge


# ---------------------------------------------------------------------------
# 多视角加权裁决（P3：降错优先）
# ---------------------------------------------------------------------------
@dataclass
class PerspectiveBreakdown:
    """每条建议性裁决的逐视角明细（可审计、可 replay）。"""

    role: str
    stance: str
    weight: float

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "stance": self.stance, "weight": self.weight}


@dataclass
class WeightedVerdict(Verdict):
    """在 R12 Verdict 之上附加逐视角明细与加权共识度（建议性）。"""

    breakdown: list[PerspectiveBreakdown] = field(default_factory=list)
    weighted_consensus: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["weighted_consensus"] = round(self.weighted_consensus, 4)
        d["breakdown"] = [b.to_dict() for b in self.breakdown]
        return d


def weighted_arbitrate(forum: DebateForum, record: DebateRecord,
                       *, strong_against_threshold: float = -0.34) -> WeightedVerdict:
    """基于辩论记录给出**多视角加权**建议性裁决。

    - 加权共识度 = Σ(weight × sign(stance)) / Σ(weight)，映射到 [-1, 1]。
    - 强约束视角（security/critic/architect）权重更高，其 ``against`` 更能压低共识，
      避免多数暴政放行风险提案（降错优先）。
    - 无裁判权 / 未达法定人数 → inconclusive（沿用 R12 fail-closed）。
    - 加权共识 < 阈值（如 -0.34，约等于一个 security against 主导）→ reject。
    """
    base = forum.arbitrate(record)
    if base.decision == "inconclusive":
        return WeightedVerdict(
            decision="inconclusive", consensus_score=base.consensus_score,
            rationale=base.rationale, judge_id=base.judge_id,
            participated=base.participated,
        )

    # 逐视角加权
    last_by: dict[str, str] = {}
    for a in record.arguments:
        last_by[a.debater_id] = a.stance
    breakdown: list[PerspectiveBreakdown] = []
    weighted_sum = 0.0
    weight_total = 0.0
    for aid, stance in last_by.items():
        role = aid  # agent_id 与 role 同名（默认阵容）
        w = _PERSPECTIVE_WEIGHT.get(role, 1.0)
        sign = 1.0 if stance == "for" else (-1.0 if stance == "against" else 0.0)
        weighted_sum += w * sign
        weight_total += w
        breakdown.append(PerspectiveBreakdown(role=role, stance=stance, weight=w))
    weighted_consensus = (weighted_sum / weight_total) if weight_total else 0.0

    if weighted_consensus < strong_against_threshold:
        decision = "reject"
    elif weighted_consensus > 0:
        decision = "support"
    else:
        decision = "inconclusive"

    rationale = (
        f"加权共识度={weighted_consensus:+.2f}（强约束视角权重更高，降错优先）；"
        f"原始共识度={base.consensus_score:+.2f}。"
    )
    return WeightedVerdict(
        decision=decision, consensus_score=base.consensus_score,
        rationale=rationale, judge_id=base.judge_id,
        participated=base.participated, breakdown=breakdown,
        weighted_consensus=weighted_consensus,
    )


# ---------------------------------------------------------------------------
# 便捷：一次跑完「默认社会辩论 + 加权裁决」
# ---------------------------------------------------------------------------
async def run_society_debate(question: str, context: str = "", *, rounds: int = 1,
                             quorum: int = 3) -> tuple[DebateRecord, WeightedVerdict]:
    """用默认六类 Agent 社会就议题辩论并给出加权裁决（建议性、不授权执行）。"""
    registry, debaters, judge = build_default_society()
    forum = DebateForum(registry, debaters, judge, quorum=quorum, rounds=rounds)
    record = await forum.run_debate(question, context)
    verdict = weighted_arbitrate(forum, record)
    return record, verdict
