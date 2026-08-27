# -*- coding: utf-8 -*-
"""#1 主动克制：hook 阈值 0.35 + 值不值得→[skip] 自检。"""
import io
import re


def test_hook_threshold_is_0_35():
    src = io.open("anima/core/autonomy.py", encoding="utf-8").read()
    # 判断阈值收紧为 0.35（不再 0.24）
    assert "score_active_context(history) >= 0.35" in src
    assert "score_active_context(history) >= 0.24" not in src
    assert ">= 0.35" in src


def test_open_question_clears_threshold():
    from anima.core.autonomy import score_active_context
    # 开放问题权重最高，应越过 0.35
    s = score_active_context([{"role": "user", "content": "大家觉得今晚吃什么好呢？"}])
    assert s >= 0.35


def test_casual_chatter_stays_below_threshold():
    from anima.core.autonomy import score_active_context
    # 单纯热闹、无开放问题/接话点 → 低于 0.35，不触发主动
    s = score_active_context([{"role": "user", "content": "哈哈"},
                              {"role": "user", "content": "笑死"},
                              {"role": "user", "content": "嗯嗯"}])
    assert s < 0.35


def test_speak_locked_non_intent_has_self_check():
    src = io.open("anima/core/autonomy.py", encoding="utf-8").read()
    # 非 intent 分支 speaking_rules 含「值不值得→skip」自检
    assert "非说不可" in src
    assert "不是就闭嘴" in src
    assert "一律只输出「[skip]」" in src