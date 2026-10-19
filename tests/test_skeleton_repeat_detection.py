# -*- coding: utf-8 -*-
"""骨架复读检测（2026-10-18，D-33 层二的度量）。

为什么另建判据而不是复用 `difflib`：实体替换把 ratio 压到阈值以下 ——
生产实测 `00:32` vs `06:00` = **0.744**、`00:32` vs `08:57` = **0.690**，
都低于主动发言去重用的 0.76，于是"漏过"；而三句**去标点后头尾逐字相同**。

本文件钉死两件事：
  1. `skeleton_parts` / `skeleton_duplicates` 能把这三句判为"同骨架"；
  2. 本轮**只检测、不改写**（改写需要额外一次 LLM 重生成，属下一轮决策）。
"""
from __future__ import annotations

import inspect

from anima.pipeline.stages.guardrail import skeleton_duplicates, skeleton_parts

#: 生产原文三条（同一句式家族）
L1 = "哼，又甩这种B站链接过来……本喵才不帮你看什么脑洞学生会呢，要看你自己看去喵！"
L2 = "又丢这种老番重制的链接过来……本喵才不帮你看什么南十字星呢，要看你自己看去喵！"
L3 = "大清早的又甩这种变身美少女的B站链接过来……本喵才不看什么女友坐不住了，要看你自己看去喵！"
#: 无关句（不得被误判）
OTHER = "好饿啊，谁下面给我吃"


def test_skeleton_parts_strips_punctuation():
    head, tail = skeleton_parts(L1)
    assert "，" not in head and "。" not in tail
    assert tail == "要看你自己看去喵"


def test_three_production_lines_share_tail():
    """三句实体全不同，但**尾巴逐字相同** ⇒ 必须被识别。"""
    for line in (L2, L3):
        dup = skeleton_duplicates(line, [L1])
        assert len(dup["tail"]) == 1, "同骨架句未被识别为尾部重复"


def test_repeated_head_is_reported():
    dup = skeleton_duplicates(L1, [L1])
    assert len(dup["head"]) == 1 and len(dup["tail"]) == 1


def test_unrelated_line_not_flagged():
    dup = skeleton_duplicates(OTHER, [L1, L2, L3])
    assert dup["head"] == [] and dup["tail"] == []


def test_short_replies_compare_as_whole():
    assert skeleton_parts("哦") == ("哦", "哦")
    assert skeleton_duplicates("哦", ["哦"])["head"] == ["哦"]


def test_empty_inputs_are_safe():
    assert skeleton_duplicates("", [L1]) == {"head": [], "tail": []}
    assert skeleton_duplicates(L1, []) == {"head": [], "tail": []}
    assert skeleton_duplicates(L1, ["", "   "]) == {"head": [], "tail": []}


def test_detection_is_log_only_not_rewrite():
    """本轮纪律：检测只写日志。改写（重生成）须先拿到基线再决策。"""
    from anima.pipeline.stages import guardrail as g

    src = inspect.getsource(g.guardrail_stage)
    assert "[骨架复读]" in src, "必须有可观测锚点"
    i = src.index("skeleton_duplicates(")
    j = src.index("骨架复读检测失败", i)
    block = src[i:j]
    assert "ctx.reply =" not in block, "检测块不得改写正文"


# ── D-38：兜底/罐头话术不得计入复读 ──────────────────────────────────────────
# 生产实证（2026-10-18 20:52 / 21:05）：`[骨架复读]` 首次命中的样本是
# 「本喵这边卡壳了喵，这条没接住，真不是故意晾着你。」——这是
# `agent/react._NO_TOOL_FALLBACKS` 的罐头句（池 7 句），不是模型复读。

def test_canned_reply_detection_covers_all_pools():
    from anima.agent.react import (
        _NO_TOOL_FALLBACKS, _TOOL_FAILED_FALLBACKS, _TOOL_OK_FALLBACKS,
    )
    from anima.pipeline.stages.generation import (
        _DEGRADE_FALLBACKS, _TOOL_NO_RESULT_FALLBACK,
    )
    from anima.pipeline.stages.guardrail import (
        _ANALYSIS_ESSAY_FALLBACKS, _MECHANICAL_REWRITES, _is_canned_reply,
    )

    for pool in (_NO_TOOL_FALLBACKS, _TOOL_FAILED_FALLBACKS, _TOOL_OK_FALLBACKS,
                 _DEGRADE_FALLBACKS, _MECHANICAL_REWRITES, _ANALYSIS_ESSAY_FALLBACKS):
        for line in pool:
            assert _is_canned_reply(line), "罐头句未被识别：%r" % (line,)
    assert _is_canned_reply(_TOOL_NO_RESULT_FALLBACK)


def test_production_sample_is_canned_not_repeat():
    """上产原文：这两条被判成「复读」是假的 —— 它们是同一个池的两句。"""
    from anima.pipeline.stages.guardrail import _is_canned_reply

    assert _is_canned_reply("本喵这边卡壳了喵，这条没接住，真不是故意晾着你。")
    assert _is_canned_reply("本喵刚刚脑子卡了一下，等会儿再跟你说嘛…")


def test_normal_reply_is_not_canned():
    from anima.pipeline.stages.guardrail import _is_canned_reply

    assert not _is_canned_reply("哼，大清早的又甩这种B站链接过来……要看你自己看去喵！")
    assert not _is_canned_reply("")
    assert not _is_canned_reply(None)


def test_detection_block_skips_canned_before_logging():
    """源码守卫：跳过判定必须出现在 `[骨架复读]` 日志**之前**。"""
    from anima.pipeline.stages import guardrail as g

    src = inspect.getsource(g.guardrail_stage)
    i_canned = src.index("_is_canned_reply(ctx.reply)")
    i_log = src.index("[骨架复读]")
    assert i_canned < i_log, "兜底排除必须早于日志，否则仍会误报"
