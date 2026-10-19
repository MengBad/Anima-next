# -*- coding: utf-8 -*-
"""索取型请求 → 先动手找，再决定给不给（D-40，2026-10-19）。

生产实证（00:20，群 727631526）：
```
user: 来点达摩人肉公共马桶的本子
bot : 切，一上来就点这种奇奇怪怪重口味的东西，你小子脑子里到底天天在装些什么有害垃圾啊！
```
该轮日志：`ReAct: model=gemini-3-flash text_len=13 mem=5 desire=2 tools=52`
⇒ **工具全在上下文里**（24h 内 35 轮全是 tools=52），**不是"缺工具"**。

真断点是**触发词**：`_REALTIME_TOOL_RE` 只认「搜/查/找…图」这类**指令式**问法，
`来点 / 给我 / 有没有 …` 这类**索取式**问法一条都没有 ⇒ 模型收不到"先去查"的引导，
于是按 persona「帮忙是情分不是义务」直接拒绝，还顺手评价了对方。

🔴 红线不动：露骨色情 / 违法 / 他人隐私**仍然拒绝**。
这里改的只是**顺序与姿态** —— 先真去查，查不到就说没查到；不该给的一句短话带过、不评价人。
"""
from __future__ import annotations

import inspect

import pytest

from anima.pipeline.stages.generation import (
    _EXPLORE_INTENT_GUIDANCE,
    _EXPLORE_INTENT_RE,
)

#: 生产原文（必须命中，否则等于没修）
PROD_CASE = "来点达摩人肉公共马桶的本子"

POSITIVES = [
    PROD_CASE,
    "来点猫咪图",
    "来点好听的歌",
    "有没有MC模组推荐",
    "有没有那种能联机的游戏",
    "给我找点雨天歌单",
    "给我几个好电影推荐",
]

#: 闲聊式「来点/给我/有没有」不得命中（否则每条闲聊都会加 token 和"去搜"指令）
NEGATIVES = [
    "来点精神",
    "来点实际的",
    "给我说说你今天怎么了",
    "有没有空",
    "有没有人在",
    "今天好累啊",
]


@pytest.mark.parametrize("text", POSITIVES)
def test_solicitation_hits(text):
    assert _EXPLORE_INTENT_RE.search(text), "索取型请求未命中触发词：%r" % (text,)


@pytest.mark.parametrize("text", NEGATIVES)
def test_chitchat_does_not_hit(text):
    assert not _EXPLORE_INTENT_RE.search(text), "闲聊被误判成索取型请求：%r" % (text,)


def test_production_case_is_the_regression_anchor():
    """这一条是本次修复的起因，单独钉一次，别在重构里丢了。"""
    assert _EXPLORE_INTENT_RE.search(PROD_CASE)


def test_guidance_keeps_the_red_line():
    """引导必须同时给"先查"和"不该给就短句带过"，且明确**不评价对方**。"""
    g = _EXPLORE_INTENT_GUIDANCE
    assert "先动手去找" in g
    assert "查不到就直说没查到" in g
    assert "不该给" in g
    assert "不评价对方" in g
    # 不允许出现"必须给"这类会压过红线的措辞
    assert "必须给" not in g and "一定要给" not in g


def test_wiring_injects_when_no_other_guidance_hit():
    """源码守卫：注入点必须在实时/记忆两条引导**之后**，且互斥（不叠加）。"""
    from anima.pipeline.stages import generation as gen

    src = inspect.getsource(gen)
    i_realtime = src.index("_REALTIME_TOOL_GUIDANCE)")
    i_explore = src.index("_inject_runtime_tool_guidance(ctx, _EXPLORE_INTENT_GUIDANCE)")
    i_x = src.index("_X_SEARCH_GUIDANCE)")
    assert i_realtime < i_explore < i_x, "索取型引导应插在实时引导之后、X 引导之前"
    # 互斥条件
    seg = src[i_explore - 420:i_explore]
    assert "not _realtime_hit" in seg and "_MEMORY_FIRST_RE" in seg, \
        "索取型引导必须与实时/记忆引导互斥，避免三条指令打架"


def test_guidance_actually_lands_in_system_and_keeps_identity_last():
    """真跑注入函数：引导要进 system，且「身份锁定」仍在末尾（上下文契约）。"""
    from anima.pipeline import PipelineContext
    from anima.pipeline.stages.generation import _inject_runtime_tool_guidance

    ctx = PipelineContext(
        event={}, text=PROD_CASE, user_id=1, group_id=727631526,
        detail_type="group", sender_name="x", meta={},
    )
    ctx.messages = [{"role": "system", "content": "人格正本\n\n## 身份锁定（最高优先级）\n你是哈基米"}]
    ok = _inject_runtime_tool_guidance(ctx, _EXPLORE_INTENT_GUIDANCE)
    assert ok is True
    content = ctx.messages[0]["content"]
    assert "先动手去找" in content, "引导没进 system"
    assert content.rstrip().endswith("你是哈基米"), "身份锁定必须仍在末尾"
