"""睡眠模式（灵魂补丁 A1）—— 纯逻辑、无 LLM 依赖，便于单测。

判定「是否在睡眠时段」只依赖本地时间；回复概率门控用可注入的 rng，
测试可固定随机序列验证 80% 沉默 / 20% 迷糊回复。
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

_BJ = timezone(timedelta(hours=8))


def is_sleep_time(
    now: datetime | None = None,
    start_hour: int = 1,
    end_hour: int = 5,
) -> bool:
    """北京时间 [start_hour, end_hour) 为睡眠时段（默认 01:00-05:00）。

    now 为 datetime（需带时区）；不传则取当前北京时。
    """
    if now is None:
        now = datetime.now(_BJ)
    # 统一转换为北京时区取小时，避免部署机时区错乱
    if now.tzinfo is None:
        now = now.replace(tzinfo=_BJ)
    else:
        now = now.astimezone(_BJ)
    hour = now.hour
    if start_hour <= end_hour:
        return start_hour <= hour < end_hour
    # 跨午夜窗口（如 23-02）：start>end 时两端都算
    return hour >= start_hour or hour < end_hour


def should_sleep_silent(
    silent_prob: float = 0.8,
    rng: random.Random | None = None,
) -> bool:
    """被 @ 且处于睡眠时段时，是否「装睡不回」。

    返回 True = 沉默（不回复）；False = 迷糊回复（概率 = 1 - silent_prob）。
    rng 可注入（如 random.Random(seed)）以便测试复现。
    """
    if rng is None:
        rng = random
    return rng.random() < silent_prob


def sleep_reply_prob(silent_prob: float = 0.8) -> float:
    """迷糊回复概率 = 1 - 沉默概率。"""
    return max(0.0, min(1.0, 1.0 - silent_prob))


def classify_sleep_reply(
    at_me: bool,
    silent_prob: float = 0.8,
    rng: random.Random | None = None,
) -> str:
    """睡眠时段内对一条入站消息的处置决策（仅供调用方在 is_sleep_time 为真后使用）。

    返回：
      - "none"   : 未 @ 本喵，走正常流程（下方闸门通常会 skip）
      - "silent" : @ 了本喵且命中沉默概率 → 装睡不回
      - "sleepy" : @ 了本喵且未命中沉默 → 迷糊回一句
    rng 可注入以便测试复现。
    """
    if not at_me:
        return "none"
    return "silent" if should_sleep_silent(silent_prob, rng) else "sleepy"
