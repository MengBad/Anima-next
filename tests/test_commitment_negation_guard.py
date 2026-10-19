# -*- coding: utf-8 -*-
"""承诺判据的否定守卫（2026-10-18，D-35）。

🔴 这是本轮**真根因**：`COMMITMENT_RE` 的多字短语分支（`帮你查|帮你看|帮你盯|…`）
是**裸短语**，于是「本喵才不**帮你看**什么脑洞学生会呢」这种**拒绝**被当成承诺，
生产实测：一句拒绝 → 13 笔假债 → 26 条议程（promise_followup + debt_due），
T+6h 议程把「当时的回复是：<拒绝原文>」喂回主动发言 ⇒ 模型把"没帮"改写成
"勉为其难帮你瞅了一眼"，并沿用旧句式（这就是 D-33 复读 / D-34 矛盾的共同上游）。

修法：新增 `commitment_match` / `is_commitment`（命中后按**紧邻前缀**判定否定）；
所有消费者（`pipeline/stages/_shared`、`memory/episodes`）改为走新判据。
"""
from __future__ import annotations

import pytest

from anima.constants import COMMITMENT_RE, commitment_match, is_commitment

#: 生产原文（10-18 群 727631526 出站），**全部是拒绝**，一条都不许判成承诺
PROD_REFUSALS = [
    "大清早的丢这种七月新番杂谈链接过来……本喵才不帮你看什么从夯到拉呢，要看你自己看去喵！",
    "哼，又甩这种B站链接过来……本喵才不帮你看什么脑洞学生会呢，要看你自己看去喵！",
    "又丢这种老番重制的链接过来……本喵才不帮你看什么南十字星呢，要看你自己看去喵！",
    "本喵才不帮你查这个。",
    "懒得帮你看，自己翻去。",
    "这种破本子你自己去找，本喵才不帮你找！",
    "我才不会帮你看这种东西。",
    "我不太想帮你看。",
    "还没帮你看呢，急什么。",
]

#: 真承诺：必须仍然命中（防"修一处、误杀一片"）
REAL_PROMISES = [
    "行，本喵帮你查一下明天的天气。",
    "我帮你看看到底怎么回事。",
    "明天帮你找找那个链接。",
    "本喵记下了，回头再帮你查。",
    "回头帮你问问他。",
    "等下帮你查查，别催。",
    # 🔴 反向误杀用例：「别急」里的「别」是安抚，不是否定承诺
    "别急，我等下再帮你查一次。",
]


@pytest.mark.parametrize("text", PROD_REFUSALS)
def test_refusals_are_not_commitments(text):
    assert commitment_match(text) is None, "拒绝句被判成承诺 ⇒ 会造假债/假议程"
    assert is_commitment(text) is False


@pytest.mark.parametrize("text", REAL_PROMISES)
def test_real_promises_still_detected(text):
    m = commitment_match(text)
    assert m is not None, "真承诺被误杀 ⇒ 该跟进的事不跟进了"
    assert m.group(0)


def test_negation_guard_is_adjacency_based_not_window_based():
    """守卫必须看**紧邻**位置：「别急，我等下再帮你查」中的「别」不构成否定。

    首版实现按"前 6 字出现否定词"判定 ⇒ 这条真承诺被漏判（实测 1/6）。
    """
    assert commitment_match("别急，我等下再帮你查一次。") is not None


def test_bare_regex_still_matches_that_is_the_bug():
    """留档：裸 `COMMITMENT_RE.search` **确实**会命中 `帮你看` —— 这是病根本身。"""
    m = COMMITMENT_RE.search(PROD_REFUSALS[1])
    assert m is not None and m.group(0) == "帮你看"


def test_consumers_use_guarded_judgement():
    """源码守卫：两处消费者不得再直接用裸 `COMMITMENT_RE.search`。"""
    import inspect
    import re

    from anima.memory import episodes
    from anima.pipeline.stages import _shared

    for mod in (episodes, _shared):
        src = inspect.getsource(mod)
        # 只允许出现在 import 行 / 注释里；不允许 `XXX.search(` 的调用
        bad = [ln for ln in src.splitlines()
               if re.search(r"(?<![A-Z_])_COMMITMENT_HINT\.search\(|(?<![A-Z_])_PROMISE_RE\.search\(", ln)
               and not ln.lstrip().startswith("#")]
        assert not bad, "%s 仍在用裸正则判承诺：%s" % (mod.__name__, bad[:2])
        assert "commitment_match" in src
