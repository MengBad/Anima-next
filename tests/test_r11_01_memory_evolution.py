"""R11-01 Memory Evolution Engine 测试。

验证确定性规则：高失败率→降级提案、高成功率+正向反馈→晋升提案、
反复失败+负反馈→纠正笔记提案、正向模式→强化记忆提案；以及桥接 R8 DevIssue。
纯规则、无 LLM，可复现。
"""

import pytest

from anima.agent.memory_evolution import (
    ISSUE_SOURCE_EVOLUTION,
    MemoryEvolutionEngine,
    OptimizationProposal,
)


def _outcomes(*, target="skill_x", n=10, success=0, fail=0, pos=0, neg=0):
    out = []
    for _ in range(success):
        out.append({"target": target, "success": True, "feedback": "positive"})
    for _ in range(fail):
        out.append({"target": target, "success": False, "feedback": "negative"})
    # 剩余样本（无明确成败）补足 n
    while len(out) < n:
        out.append({"target": target, "feedback": "positive" if pos > 0 else ("negative" if neg > 0 else "")})
        if pos > 0:
            pos -= 1
        elif neg > 0:
            neg -= 1
    return out


def test_high_failure_rate_proposes_deprecate():
    eng = MemoryEvolutionEngine()
    oc = _outcomes(target="bad_tool", n=10, fail=7, neg=3)
    props = eng.propose_optimizations(oc)
    kinds = [p.kind for p in props]
    assert "skill_deprecate" in kinds
    assert "memory_prune" in kinds  # fail>=min 且 neg>=3


def test_high_success_rate_with_pos_feedback_proposes_promote():
    eng = MemoryEvolutionEngine()
    # 10 样本，8 成功 + 3 正向反馈（部分重叠），无失败
    oc = []
    for _ in range(8):
        oc.append({"target": "good_tool", "success": True, "feedback": "positive"})
    for _ in range(2):
        oc.append({"target": "good_tool", "success": False, "feedback": "positive"})
    props = eng.propose_optimizations(oc)
    kinds = [p.kind for p in props]
    assert "skill_promote" in kinds
    assert "note_suggest" in kinds


def test_low_samples_not_enough():
    eng = MemoryEvolutionEngine()
    oc = _outcomes(target="rare", n=3, fail=3)  # 样本不足 min_samples=5
    props = eng.propose_optimizations(oc)
    assert props == []


def test_neutral_outcomes_no_proposal():
    eng = MemoryEvolutionEngine()
    # 成功率低但失败率也低（边界），无正向反馈，不应触发 deprecate/promote
    oc = _outcomes(target="mid", n=10, success=4, fail=3, pos=0, neg=0)
    props = eng.propose_optimizations(oc)
    kinds = [p.kind for p in props]
    assert "skill_deprecate" not in kinds
    assert "skill_promote" not in kinds


def test_to_dev_issues_bridges_r8():
    eng = MemoryEvolutionEngine()
    oc = _outcomes(target="bad_tool", n=10, fail=7, neg=3)
    props = eng.propose_optimizations(oc)
    issues = eng.to_dev_issues(props)
    assert len(issues) == len(props)
    for i in issues:
        assert i.source == ISSUE_SOURCE_EVOLUTION
        assert i.title.startswith("[记忆演化]")


def test_proposal_to_dict_serializable():
    p = OptimizationProposal(kind="skill_promote", target="t", rationale="r",
                             evidence={"a": 1}, confidence=0.5)
    d = p.to_dict()
    assert d["kind"] == "skill_promote"
    assert d["confidence"] == 0.5
    assert d["evidence"]["a"] == 1
