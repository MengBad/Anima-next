"""r28 N1（RED）：主动发言的静默出口必须有日志。

生产事实（2026-10-06）：主动发言 **6 次触发、3 次实发**（命中率 50%）。
三次未发的（10:43 / 18:44 / 18:49）journal 里**只有**：

    主动发言人格片段：self_notes=… 字，mood=…，world=… 字

然后**什么都没有** —— 既没有「Token 预算不足」，也没有异常，也没有
低质水话/去重/接地网关的丢弃日志，更没有 WS action。

根因：``autonomy.py`` 主流程里这一行是**完全无日志**的 return：

    if not reply or "[skip]" in reply.lower() or len(reply.strip()) < 4:
        return False

三个语义完全不同的出口（空回复 / 模型主动 skip / 太短）共用一个裸 return，
事后无法归因「到底是配额烧了还是模型不想说」。

修法：抽成纯函数 ``_active_reply_drop_reason``，调用点按原因码落日志。
"""
from __future__ import annotations

import io
import re

import pytest

from anima.core import autonomy as au


def _reason():
    fn = getattr(au, "_active_reply_drop_reason", None)
    assert fn is not None, "autonomy 缺少 _active_reply_drop_reason ⇒ N1 未修"
    return fn


@pytest.mark.parametrize(
    "reply, expected",
    [
        ("", "empty"),
        ("   ", "empty"),
        ("[skip]", "skip_tag"),
        ("[SKIP] 这个话题算了", "skip_tag"),
        ("嗯", "too_short"),
        ("喵", "too_short"),
        ("今天群里好安静啊，都在摸鱼吗", ""),
        ("  哈哈，这个梗我能笑一年  ", ""),
    ],
)
def test_drop_reason(reply, expected):
    assert _reason()(reply) == expected


def test_call_site_logs_the_reason():
    """结构性门禁：调用点必须落日志，否则又变回静默出口。"""
    src = io.open(au.__file__, "r", encoding="utf-8", errors="replace").read()
    # 排除 `def _active_reply_drop_reason(` 这处定义本身，只看真实调用点
    calls = [m for m in re.finditer(r"(?<!def )_active_reply_drop_reason\(", src)]
    assert calls, "源码里没有调用 _active_reply_drop_reason（只有定义）"
    logged = False
    for m in calls:
        tail = src[m.end(): m.end() + 600]
        if "return False" not in tail:
            continue
        head = tail[: tail.index("return False")]
        if "logger." in head:
            logged = True
    assert logged, "调用点在 return False 之前没有 logger 调用 ⇒ 仍是静默出口"
