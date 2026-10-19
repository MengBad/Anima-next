# -*- coding: utf-8 -*-
"""`[表情选图]` 埋点（2026-10-20）—— 纯观测，不改任何行为。

起因：10-19 22:25–22:48「跑图」事件，bot 三轮连发表情包。
但既有证据只到「同情绪」：`chat_memory` 的 bot 条记的是
`（发了一张表情包：撒娇）`，来自 `StickerManager.describe_paths`，
**返回受控 emotion 词表，不是文件名** ⇒ 无法判断是不是**同一张图**连发。

本埋点把 `sticker_to_send` 的文件 stem（= stickers.json 的 sha1 索引键）落到日志，
使"连发"事后可判定为「同图」还是「同情绪」。

约束（本轮刻意守住）：
  * **不改行为** —— 只是 logger.info，落点仍在既有 `break` 之前；
  * **不改 idle 闸门** —— MEMORY.md 有硬不变量：idle 是反刷屏保护、社交策略非缺陷，
    是否开口子须 owner 裁定。
"""
from __future__ import annotations

import inspect

from anima.pipeline.stages import generation as gen


def test_sticker_selection_emits_trace_log():
    """源码守卫：选到图后必须打 stem。"""
    src = inspect.getsource(gen)
    assert "[表情选图]" in src, "缺少表情选图埋点"
    assert "Path(path).stem" in src, "埋点须记 stem（sh a1 索引键），不是 emotion"


def test_trace_log_is_before_break_and_fail_soft():
    """埋点必须在 break 之前，且被 try/except 包住（观测绝不能搞挂出站）。"""
    src = inspect.getsource(gen)
    i_log = src.index("[表情选图]")
    seg = src[i_log:i_log + 700]
    assert "break" in seg, "埋点应在既有 break 之前"
    assert "except Exception" in seg, "埋点必须 fail-soft"


def test_assignment_order_preserved():
    """埋点不得改变原有顺序：先设 sticker_to_send / 剥路径，再打日志，再 break。"""
    src = inspect.getsource(gen)
    i_set = src.index("ctx.sticker_to_send = path")
    i_strip = src.index("_strip_sticker_path_from_reply", i_set)
    i_log = src.index("[表情选图]")
    i_break = src.index("break", i_log)
    assert i_set < i_strip < i_log < i_break, "赋值→剥路径→埋点→break 的顺序被改动"


def test_no_behaviour_change_outside_logging():
    """本轮唯一改动就是加日志：不得出现新的控制流/赋值。"""
    src = inspect.getsource(gen)
    i_log = src.index("[表情选图]")
    seg = src[i_log:i_log + 600]
    # 日志段内不应有 ctx.* 赋值
    assert "ctx.sticker_to_send =" not in seg
    assert "ctx.reply =" not in seg
