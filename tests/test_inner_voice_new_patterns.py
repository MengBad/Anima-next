"""P0 回归（2026-07-29）：内心独白过滤词库扩展。

基于线上泄漏日志（21:46 主动/被动发言含独白片段）扩词库后，
整段独白必须被 filter_inner_monologue 识别为 None（调用方抑制发送）。
"""
import time

from anima.core.reply_splitter import filter_inner_monologue


def test_full_monologue_returns_none():
    # 来自线上日志的整段独白：承诺推演 + 对用户的负面内心评价
    text = (
        "之前答应了那家伙一件事，本喵倒要看看需不需要跟进。"
        "哼，那天她就奇奇怪怪的，我看她就是单纯闲得慌想逗我玩喵！"
    )
    assert filter_inner_monologue(text) is None


def test_partial_monologue_keeps_normal_part():
    # 含独白片段但仍有正常回复 → 仅移除独白，保留正常部分
    text = "本喵倒要看看需不需要跟进。好呀，那本喵帮你查一下喵！"
    out = filter_inner_monologue(text)
    assert out is not None
    assert "本喵倒要看看" not in out
    assert "需不需要跟进" not in out
    assert "好呀" in out
    assert "帮你查" in out


def test_negative_inner_eval_removed():
    # 对用户的负面内心评价片段必须移除
    text = "方案做完了。我看她就是单纯闲得慌，懒得理她。"
    out = filter_inner_monologue(text)
    assert out is not None
    assert "我看她就是单纯闲得慌" not in out
    assert "方案做完了" in out
