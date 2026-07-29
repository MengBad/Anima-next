"""P1 回归（2026-07-29）：时间线感知——旧话题不延续（防翻旧账）。"""
import time

from anima.cognitive.timeline_manager import TimelineManager


def _epoch(hhmm: str, base: float) -> float:
    """把 'HH:MM' 映射到 base 当天同一时刻的 epoch。"""
    h, m = hhmm.split(":")
    struct = time.localtime(base)
    return time.mktime(
        (struct.tm_year, struct.tm_mon, struct.tm_mday, int(h), int(m), 0, 0, 0, struct.tm_isdst)
    )


def test_is_stale_topic_true_for_2h_old():
    now = _epoch("21:46", time.time())
    old = _epoch("19:33", time.time())
    assert (now - old) > 30 * 60  #  sanity：确实 >30min
    assert TimelineManager.is_stale_topic(now, old) is True


def test_is_stale_topic_false_for_fresh():
    now = _epoch("21:46", time.time())
    recent = _epoch("21:40", time.time())
    assert (now - recent) <= 30 * 60
    assert TimelineManager.is_stale_topic(now, recent) is False


def test_filter_stale_context_drops_old_message():
    now = _epoch("21:46", time.time())
    old_ts = _epoch("19:33", time.time())
    messages = [
        {"role": "user", "content": "你那天坏掉了", "time": old_ts},
        {"role": "bot", "content": "本喵没坏掉", "time": old_ts},
        {"role": "user", "content": "哈基米", "time": now},
    ]
    fresh = TimelineManager.filter_stale_context(messages, now)
    # 19:33 的旧话题被丢弃，仅保留当前消息
    assert len(fresh) == 1
    assert fresh[0]["content"] == "哈基米"


def test_filter_stale_context_keeps_explicitly_referenced():
    now = _epoch("21:46", time.time())
    old_ts = _epoch("19:33", time.time())
    messages = [
        {"role": "user", "content": "你那天坏掉了", "time": old_ts, "is_explicitly_referenced": True},
        {"role": "user", "content": "哈基米", "time": now},
    ]
    fresh = TimelineManager.filter_stale_context(messages, now)
    assert len(fresh) == 2  # 被显式引用的旧消息保留
