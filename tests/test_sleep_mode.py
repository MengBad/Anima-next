"""A1 睡眠模式：02:00 @bot 80% 沉默 / 20% 迷糊回复（确定性边界 + 分布）。"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from anima.cognitive.sleep_state import (
    is_sleep_time,
    should_sleep_silent,
    classify_sleep_reply,
    sleep_reply_prob,
)

_BJ = timezone(timedelta(hours=8))


def _dt(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, 30, hour, minute, tzinfo=_BJ)


def test_is_sleep_time_window():
    # 默认窗口 01:00-05:00（含起点，不含终点）
    assert is_sleep_time(_dt(1)) is True
    assert is_sleep_time(_dt(2)) is True
    assert is_sleep_time(_dt(4, 59)) is True
    assert is_sleep_time(_dt(5)) is False   # 终点不含
    assert is_sleep_time(_dt(12)) is False
    assert is_sleep_time(_dt(0)) is False


def test_is_sleep_time_custom_window():
    # 跨午夜窗口 23-02
    assert is_sleep_time(_dt(23), start_hour=23, end_hour=2) is True
    assert is_sleep_time(_dt(1), start_hour=23, end_hour=2) is True
    assert is_sleep_time(_dt(3), start_hour=23, end_hour=2) is False


def test_classify_sleep_reply_not_at_me_passes_through():
    # 未 @ 本喵 → 走正常流程，不受概率影响
    assert classify_sleep_reply(False, silent_prob=0.8) == "none"
    assert classify_sleep_reply(False, silent_prob=1.0) == "none"


def test_classify_sleep_reply_deterministic_boundaries():
    # silent_prob=1.0 → 永远 silent；0.0 → 永远 sleepy（确定性，便于断言）
    assert classify_sleep_reply(True, silent_prob=1.0) == "silent"
    assert classify_sleep_reply(True, silent_prob=0.0) == "sleepy"


def test_should_sleep_silent_distribution():
    # 大样本下沉默比例接近 0.8（±容差），验证「80% 不回复」语义
    import random
    rng = random.Random(12345)
    n = 5000
    silent = sum(1 for _ in range(n) if should_sleep_silent(0.8, rng))
    ratio = silent / n
    assert 0.75 < ratio < 0.85, f"沉默比例 {ratio} 偏离 0.8"


def test_sleep_reply_prob_complement():
    assert abs(sleep_reply_prob(0.8) - 0.2) < 1e-9


def test_quiet_hours_default_window_cross_midnight():
    """quiet_hours 默认窗口 23:00-07:00（跨午夜）：23/00/06 在窗，07 出窗。"""
    from anima.core.config import AutonomyConfig
    cfg = AutonomyConfig()
    # 默认 23-7 跨午夜
    assert is_sleep_time(_dt(23), cfg.quiet_hours_start_hour, cfg.quiet_hours_end_hour) is True
    assert is_sleep_time(_dt(0), cfg.quiet_hours_start_hour, cfg.quiet_hours_end_hour) is True
    assert is_sleep_time(_dt(6, 59), cfg.quiet_hours_start_hour, cfg.quiet_hours_end_hour) is True
    assert is_sleep_time(_dt(7), cfg.quiet_hours_start_hour, cfg.quiet_hours_end_hour) is False
    assert is_sleep_time(_dt(12), cfg.quiet_hours_start_hour, cfg.quiet_hours_end_hour) is False


def test_quiet_hours_defaults_disabled():
    """quiet_hours 默认关闭（不打扰既有行为），allow_direct 默认豁免点名。"""
    from anima.core.config import AutonomyConfig
    cfg = AutonomyConfig()
    assert cfg.quiet_hours_enabled is False
    assert cfg.quiet_hours_allow_direct is True
