# -*- coding: utf-8 -*-
"""出站时段词纠偏（2026-10-18，D-32）。

病因（生产实证 10-17 23:56 群聊出站原文）：
    「哼，大清早的又丢这种椎名もた的歌单链接过来……」
真实时间 23:56，注入的 `{time_context}` 是「周六 深夜11点56分」。
⇒ 链路给了、模型没听（P2 在 persona:33 删词列举删的是**示例**，管不住自造词）。
修法：出站护栏做确定性纠偏 —— 只换词，不删句、不整段改写。

本文件同时钉死**跨夜判据**：`大红大紫` 的坑在 10-18 Track 2 出过一次 ——
把「大晚上」的时间带写成 (18,23)，于是 00:xx / 01:xx 说「大晚上」被判成错配
（虚报 3/8，真值 1/8）。任何时段词时间带必须显式允许跨夜。
"""
from __future__ import annotations

import pytest

from anima.pipeline.stages.guardrail import (
    _PERIOD_WORD_WINDOWS,
    _hour_in_window,
    _period_word_for_hour,
    fix_period_words,
)

#: 10-17 23:56 的真实出站正文（生产原文，勿改写）
PROD_2356 = "哼，大清早的又丢这种椎名もた的歌单链接过来……本喵才不听什么总是在你离开之前呢，要听你自己听去喵！"
#: 10-18 03:54 的真实出站正文
PROD_0354 = "大清早的丢这种怪链接过来……本喵才不帮你看什么小北他喵的配音呢"


def test_production_case_2356_is_corrected():
    """23:56 说「大清早」⇒ 必须被换成当晚的说法。"""
    out, fixes = fix_period_words(PROD_2356, hour=23)
    assert "大清早" not in out, "深夜不应保留「大清早」"
    assert "大晚上" in out
    assert fixes == [("大清早", "大晚上")]


def test_production_case_0354_is_corrected():
    out, fixes = fix_period_words(PROD_0354, hour=3)
    assert "大清早" not in out
    assert "大半夜" in out
    assert fixes == [("大清早", "大半夜")]


def test_correct_words_are_left_alone():
    """06:00 的「大清早」是**对的**，一个字都不能动。"""
    text = "大清早的提这个地狱笑话，罪羊要是听见了估计能顺着网线爬过来挠我喵。"
    out, fixes = fix_period_words(text, hour=6)
    assert out == text
    assert fixes == []


@pytest.mark.parametrize("hour,text", [
    (0, "哼，大晚上不睡觉满脑子都在想什么乱七八糟的呢！"),
    (1, "哼，大晚上不睡觉跑来晒你的方块世界"),
    (4, "大晚上的还在群里蹦跶"),
    (20, "大晚上的不睡觉"),
])
def test_cross_midnight_evening_window(hour, text):
    """「大晚上」18:00 → 次日 04:59 都成立（跨夜带，10-18 的坑）。"""
    out, fixes = fix_period_words(text, hour=hour)
    assert fixes == [], f"{hour} 点说「大晚上」不应被判错配"
    assert out == text


@pytest.mark.parametrize("hour", [22, 23, 0, 1, 3, 4])
def test_midnight_window(hour):
    """「大半夜」21:00 → 次日 04:59 成立。"""
    out, fixes = fix_period_words("大半夜的还不睡", hour=hour)
    assert fixes == []


def test_noon_word_at_midnight_is_corrected():
    out, fixes = fix_period_words("大中午的吃什么饭啊", hour=2)
    assert "大中午" not in out
    assert "大半夜" in out


def test_replacement_is_idempotent():
    """纠偏后必须在自己的时间带内 —— 再跑一次不应改动（防抖）。"""
    for hour in range(24):
        out, _ = fix_period_words(PROD_2356, hour=hour)
        again, fixes2 = fix_period_words(out, hour=hour)
        assert again == out, f"{hour} 点纠偏不幂等：{out!r} → {again!r}"
        assert fixes2 == []


def test_longer_word_wins():
    """「大早上」不得被更短的规则拆开替换成「大XX早上」。"""
    out, fixes = fix_period_words("大早上的就吵", hour=23)
    assert "大早上" not in out
    assert out.startswith("大晚上")


def test_empty_and_none_safe():
    assert fix_period_words("", hour=3) == ("", [])
    assert fix_period_words(None, hour=3) == (None, [])  # type: ignore[arg-type]


def test_every_replacement_lands_inside_its_own_window():
    """`_period_word_for_hour` 的返回值必须落在该词自己的时间带内（自洽）。"""
    for hour in range(24):
        word = _period_word_for_hour(hour)
        assert word in _PERIOD_WORD_WINDOWS, f"{word} 未登记时间带"
        assert _hour_in_window(hour, _PERIOD_WORD_WINDOWS[word]), \
            f"{hour} 点得到「{word}」，但它不在自己的时间带内"


def test_window_helper_handles_wrap():
    assert _hour_in_window(23, (18, 5)) is True
    assert _hour_in_window(0, (18, 5)) is True
    assert _hour_in_window(4, (18, 5)) is True
    assert _hour_in_window(5, (18, 5)) is False
    assert _hour_in_window(12, (18, 5)) is False
    assert _hour_in_window(6, (5, 10)) is True
    assert _hour_in_window(10, (5, 10)) is False


def test_guardrail_stage_wires_fix_before_other_rewrites():
    """源码守卫：纠偏必须挂在 `guardrail_stage` 里，且在早段（不是某条分支内部）。"""
    import inspect

    from anima.pipeline.stages import guardrail as g

    src = inspect.getsource(g.guardrail_stage)
    assert "fix_period_words(" in src, "guardrail_stage 必须调用时段纠偏"
    # 位置守卫：纠偏出现在「危险内容检查」之前 —— 避免被后续整段替换绕开。
    assert src.index("fix_period_words(") < src.index("DANGEROUS_PATTERNS")
