# -*- coding: utf-8 -*-
"""#6 早晨统一：梦境分享改走 agenda（not_before_at=次日8点）。"""
import io
from datetime import datetime, timedelta, timezone

_BJ = timezone(timedelta(hours=8))


def _next_8am(now: float) -> float:
    _now_bj = datetime.fromtimestamp(now, _BJ)
    _next8 = (_now_bj + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
    return _next8.timestamp()


def test_next_8am_computation():
    now = datetime(2026, 8, 28, 15, 0, 0, tzinfo=_BJ).timestamp()
    nxt = datetime.fromtimestamp(_next_8am(now), _BJ)
    assert (nxt.year, nxt.month, nxt.day) == (2026, 8, 29)
    assert (nxt.hour, nxt.minute) == (8, 0)
    assert nxt > datetime.fromtimestamp(now, _BJ)


def test_morning_dream_share_uses_agenda():
    src = io.open("anima/core/autonomy.py", encoding="utf-8").read()
    assert 'source="morning_dream_share"' in src
    assert 'not_before_at=_next8_ts' in src
    assert 'propose_agenda' in src
    i = src.index("if dream and dream.get")
    dream_block = src[i:i + 2000]
    assert "self._speak(gid, now, _share)" not in dream_block


def test_dream_share_fail_closed_without_agenda():
    src = io.open("anima/core/autonomy.py", encoding="utf-8").read()
    assert 'hasattr(self.core, "propose_agenda")' in src
    assert "跳过梦境分享" in src
