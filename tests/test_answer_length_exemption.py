# -*- coding: utf-8 -*-
"""求答类问题的超长回复不该被 420 字闸门砍（2026-10-08）

## 生产实证（7 天仅 2 条超长，两条都栽在同一处）

```
10-03 19:28:52  私聊  用户：「医生给我开了思诺思和文拉法辛和富马酸喹硫平」(21 字)
                Guardrail: 超长回复截断至 420 字
                ⇒ 用药说明被砍掉后半段（怎么吃 / 注意什么）

10-04 21:56:27  群    用户：「512的能不能压到100mb以前的内存占用」(21 字)
                工具：query_web_memory + search_web×2（返回 8199 字）
                Guardrail: 超长回复截断至 420 字
```

## 根因：**同一个修法只做了一半**

`guardrail.py` 的 essay 分支（破角色长文）早在 10-04 就已把豁免扩到
`_user_asked_question`（「用户确实在问东西」），而**长度闸门那一行**只用了
`_user_wants_substantive`（用户得说「解析 / 如何 / 攻略」）。

⇒ 用户只要**正常提问**（不说 how-to 词、只把情况陈述出来），就拿不到 900 豁免。
两条生产原句 `_user_wants_substantive` 均为 **False**、`_user_asked_question` 均为 **True**。

## 判据

1. 求答类原句（陈述式 / 疑问式）⇒ 上限 900，600 字回复**不得**被砍。
2. 闲聊 / 情绪表达 ⇒ 上限仍是 420（`_user_asked_question` 刻意守住的边界）。
3. **上限本身不得放宽**：即使拿到豁免，>900 仍要截，且 ≤ 901。
4. 源码级回归：两个分支的判据集**不得再次分叉**。

🔴 长回复样本刻意用**中性叙事**：定义式/抽象名词含量高的文本会被 essay 分支
（本段之前、且会 return）先判成「破角色分析体」，那样测到的就不是长度闸门了。
"""

from __future__ import annotations

import io
import re

import pytest

import anima.pipeline.stages.guardrail as _gr
from anima.pipeline.stages.guardrail import (
    _user_asked_question,
    _user_wants_substantive,
    guardrail_stage,
)

# ── 生产原句（10-03 私聊 / 10-04 群）──────────────────────────────
PROD_CONSULT = "医生给我开了思诺思和文拉法辛和富马酸喹硫平"
PROD_KNOWLEDGE = "512的能不能压到100mb以前的内存占用"

CASUAL = "哈哈哈哈"
EMOTIONAL = "今天真的好累啊感觉要猝死"

#: 中性叙事填充（**不得**含定义式句式 / 抽象名词，否则会被 essay 分支先拦走）
_UNIT = "本喵今天出门转了一圈，看到街角那家店又排起了长队，真是服了这些人的耐心。"


def _long_reply(n: int) -> str:
    s = ""
    while len(s) < n:
        s += _UNIT
    return s


class _Core:
    def __init__(self, d):
        from types import SimpleNamespace
        self.config = SimpleNamespace(
            persistence=SimpleNamespace(data_dir=d),
            llm=SimpleNamespace(namespace="chat"),
        )
        self.vector_memory = None
        self.chat_memory = None


async def _run(text: str, reply: str, tmp_path) -> str:
    from anima.pipeline import PipelineContext
    core = _Core(str(tmp_path))
    ctx = PipelineContext(event={}, text=text, user_id=1, group_id=727631526,
                          detail_type="group", sender_name="A",
                          meta={"core": core})
    ctx.reply = reply
    await guardrail_stage(ctx)
    return ctx.reply


# ── 0. 前提自证：判据差异确实存在 ────────────────────────────────

@pytest.mark.parametrize("text", [PROD_CONSULT, PROD_KNOWLEDGE])
def test_prod_sentences_are_missed_by_old_predicate(text):
    """🔴 前提：两条生产原句**都**不被旧判据认可 —— 否则本文件测的不是真问题。"""
    assert _user_wants_substantive(text) is False, (
        f"原句含 how-to 关键词 {text!r} ⇒ 前提不成立"
    )
    assert _user_asked_question(text) is True, f"新判据未覆盖生产原句 {text!r}"


@pytest.mark.parametrize("text", [CASUAL, EMOTIONAL])
def test_casual_and_emotional_stay_out(text):
    """🔴 反例护栏：闲聊 / 情绪表达**不得**因本次改动拿到豁免。"""
    assert _user_asked_question(text) is False, f"{text!r} 不该被判成「在问东西」"
    assert _user_wants_substantive(text) is False


# ── 1. 端到端：求答类 600 字回复不得被砍到 420 ────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("text", [PROD_CONSULT, PROD_KNOWLEDGE])
async def test_answer_question_not_capped_at_420(text, tmp_path):
    reply = _long_reply(600)
    assert 420 < len(reply) < 900, f"样本须落在 (420,900)，实际 {len(reply)}"

    got = await _run(text, reply, tmp_path)

    assert got == reply, (
        f"求答类回复被截断：{len(got)} 字（应保持原样 {len(reply)}）"
        f" | 尾部={got[-20:]!r}"
    )
    assert not got.endswith("…"), "不应有省略号"


# ── 2. 端到端：闲聊 / 情绪表达仍封在 420 ──────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("text", [CASUAL, EMOTIONAL])
async def test_casual_reply_still_capped_at_420(text, tmp_path):
    reply = _long_reply(600)
    got = await _run(text, reply, tmp_path)

    assert len(got) <= 421, f"闲聊回复上限被放宽到 {len(got)}（应 ≤421）"
    assert got.endswith("…"), "仍应以省略号收尾"


# ── 3. 硬约束：上限本身不得放宽（豁免也只在 900 内）────────────────

@pytest.mark.asyncio
async def test_substantive_limit_itself_unchanged(tmp_path):
    reply = _long_reply(1400)
    assert len(reply) > 900, "样本未超 900"

    got = await _run(PROD_KNOWLEDGE, reply, tmp_path)

    assert got.endswith("…"), "超 900 仍必须截断"
    assert len(got) <= 901, f"900 上限被放宽：{len(got)}"


@pytest.mark.asyncio
async def test_short_reply_untouched(tmp_path):
    reply = "短回复喵。"
    got = await _run(PROD_CONSULT, reply, tmp_path)
    assert got == reply, "不超限不得改动"


# ── 4. 源码级回归：两个分支的判据集不得再次分叉 ────────────────────

def test_both_branches_share_the_same_exemption_set():
    """🔴 本轮 bug 的形态就是「一个分支改了、另一个没改」。

    钉住 `hard_limit` 那一行必须**同时**认两个判据。
    """
    src = io.open(_gr.__file__, encoding="utf-8", errors="replace").read()
    m = re.search(r"hard_limit\s*=\s*900\s*if\s*\((.*?)\)\s*else\s*420", src, re.S)
    assert m, "找不到 hard_limit 豁免表达式（形态变了，判据需同步）"
    expr = m.group(1)
    assert "_user_wants_substantive" in expr, "hard_limit 丢了 how-to 判据"
    assert "_user_asked_question" in expr, (
        "hard_limit 又只挂了 _user_wants_substantive —— 这正是 10-08 修的 bug"
    )
