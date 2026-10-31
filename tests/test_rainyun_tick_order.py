"""雨云 tick 顺序：先自动签到，再兜底提醒（2026-10-31）。

生产实证（10-30 与 10-31 连续两天）：
  09:00:21  「雨云已提醒 owner 签到」   <- 先发提醒
  09:01:00  自动签到实际执行并成功      <- 后跑

两天都是「先被告知没签到，一分钟后自动签到成了」。
根因是 _tick 里 reminder 排在 signin 前面：reminder 做判定时
「今天已经跑过自动签到」这个标记还没写，所以它照常催。

修法：调换顺序。signin 先跑并写标记，reminder 随后看到标记就让位。
本文件锁死这个顺序，防止以后有人「顺手整理」时又换回去。
"""
from __future__ import annotations

import pathlib
import re

AUTONOMY = pathlib.Path(__file__).resolve().parent.parent / "anima" / "core" / "autonomy.py"


def _source() -> str:
    return AUTONOMY.read_text(encoding="utf-8")


def _tick_body() -> str:
    """截取 _tick 函数体（到下一个同级 def 为止）。"""
    src = _source()
    m = re.search(r"\n    async def _tick\(", src)
    assert m, "找不到 _tick 定义"
    start = m.start()
    nxt = src.find("\n    async def ", start + 10)
    return src[start:nxt if nxt > 0 else len(src)]


def test_signin_before_reminder():
    body = _tick_body()
    i_signin = body.find("self._rainyun_signin_tick(")
    i_reminder = body.find("self._rainyun_reminder_tick(")
    assert i_signin > 0, "找不到自动签到调用"
    assert i_reminder > 0, "找不到提醒调用"
    assert i_signin < i_reminder, (
        "顺序错了：必须在自动签到之后再跑提醒，否则提醒看不到「今天已跑过」的标记"
    )


def test_reminder_yields_when_signin_ran():
    """提醒函数里必须有「自动签到今日已跑过就退让」的判定。"""
    src = _source()
    idx = src.find("async def _rainyun_reminder_tick(")
    assert idx > 0
    nxt = src.find("\n    async def ", idx + 10)
    body = src[idx:nxt if nxt > 0 else len(src)]
    assert "_rainyun_signin_mark" in body, (
        "提醒逻辑没检查自动签到标记，会与自动签到同时发消息"
    )
