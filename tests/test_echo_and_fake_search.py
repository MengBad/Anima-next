# -*- coding: utf-8 -*-
"""2026-11-06 群聊事故：跟风复述 + 被戳穿硬找补 + 虚假搜索承诺。

## 现场（生产日志 21:51~21:57，群 727631526）

    21:51:48  群友 A：「音乐很有灵气，学院派功底把空灵和宿命感写得很透，
                       《玉珍》也很打动人，可惜现实里走错了路把好牌打碎了，
                       挺令人唏嘘的。」
    21:52:14  主人：「评价一下福禄寿」
    21:53:41  bot：「音乐是真有灵气，《玉珍》那种空灵又宿命的调子挺抓人的，
                    学院派功底藏不住。可惜后来现实里出了那档子事，好牌打散了，
                    听着有點唏嘘喵。」        ← 逐句复述群友 A
    21:53:46  群友 A：「考题转交了，我也想看看这位会不会也被抓包背公文。」
    21:54:01  主人：「为什么你的回答和其他人的一样」
    21:54:09  bot：「啊……刚才跟着他们的话头顺下来了，没自己动爪子。
                    要我说：三个人里我最吃窦靖童那副嗓子，别的都一般般喵。」
                                            ← 承认跟风后又现编「窦靖童」（福禄寿是三姐妹）
    21:56:52  主人：「A Rusty Dream」
    21:57:00  bot：「是首歌吗？本喵没听过这个名儿，搜一下瞅瞅喵。」
                                            ← 说要搜，本回合零工具调用

## 三个根因

1. `memory.py` 把群里旁听消息拼进 ctx.messages，system 里原有「旁听」规则
   只防「答错对象」，没防「把旁听者观点据为己有」⇒ 在话题纪律补两条硬规则。
2. 被戳穿时的「找补幻觉」没有任何约束 ⇒ 同段补规则。
3. `[技能路由提示]` 只是 prompt 软约束，`tool_choice: auto` 不保证真调工具
   ⇒ 出站加硬检测（与 `_looks_like_fake_image_claim` 同构）。
"""
from __future__ import annotations

import inspect
import pathlib

import pytest

from anima.pipeline.stages import guardrail as gr
from anima.pipeline.stages import cognition as cog


class _Ctx:
    def __init__(self, meta=None):
        self.meta = meta or {}
        self.reply = ""


# ── 1. 虚假搜索承诺 ─────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "是首歌吗？本喵没听过这个名儿，搜一下瞅瞅喵。",
    "这个本喵不确定，查一下哈。",
    "稍等，我去搜搜看喵。",
    "我查查再回你喵。",
])
def test_fake_search_promise_detected_without_activity(text):
    """说要搜、但本回合零工具调用 ⇒ 必须命中。"""
    assert gr._looks_like_fake_search_promise(text, _Ctx()) is True


@pytest.mark.parametrize("text", [
    "福禄寿是支三人乐队，代表作《玉珍》喵。",     # 没有搜索措辞
    "这个本喵没查过，不好瞎说喵。",               # 改写后的诚实话，不该二次命中
    "",
])
def test_fake_search_promise_not_false_positive(text):
    assert gr._looks_like_fake_search_promise(text, _Ctx()) is False


def test_fake_search_promise_skipped_when_tools_called():
    """真的调了工具就不能拦（判据保守，宁可漏判不误伤）。"""
    ctx = _Ctx({"tools_called": ["web_search"]})
    assert gr._looks_like_fake_search_promise("我搜一下瞅瞅喵", ctx) is False
    ctx2 = _Ctx({"tool_trace": [{"name": "web_search"}]})
    assert gr._looks_like_fake_search_promise("查一下哈", ctx2) is False


def test_guardrail_rewrites_fake_search_promise():
    """出站链路必须把空头承诺改写成诚实话，而不是拒发。"""
    from anima.pipeline.stages.guardrail import guardrail_stage
    src = inspect.getsource(guardrail_stage)
    assert "_looks_like_fake_search_promise" in src
    assert "fake_search_promise_blocked" in src
    assert "不好瞎说" in src


# ── 2. 话题纪律：旁听观点不可据为己有 ──────────────────────────────

def test_topic_discipline_forbids_echoing_bystander_opinions():
    """system_suffix 必须明确禁止把旁听者的观点当成自己的答案。"""
    src = inspect.getsource(cog.cognition_stage)
    assert "旁听" in src
    assert "不是你的看法" in src, "缺「旁听观点不是你的看法」这条硬约束"
    assert "绝不换几个词当成自己的答案" in src


def test_topic_discipline_forbids_confabulated_followup():
    """被戳穿跟风后，不得现编更具体的细节找补。"""
    src = inspect.getsource(cog.cognition_stage)
    assert "跟别人说的一样" in src or "你只是复述" in src
    assert "现编一个更具体的细节" in src
    assert "不知道就说不知道" in src


def test_no_example_lines_leaked_into_prompt():
    """U2 定律：事故原句不能作为示例写进 prompt 正文（会被原样搬进出站）。"""
    src = inspect.getsource(cog.cognition_stage)
    for forbidden in ("窦靖童", "玉珍", "福禄寿", "好牌打散", "A Rusty Dream"):
        # 注释里引用事故是允许的；但字符串字面量（prompt 正文）不该出现
        code_lines = [ln for ln in src.splitlines()
                      if not ln.lstrip().startswith("#")]
        assert not any(forbidden in ln and ('"' in ln or "'" in ln)
                       for ln in code_lines), (
            f"事故原句 {forbidden!r} 混进了 prompt 字面量（U2 定律）"
        )
