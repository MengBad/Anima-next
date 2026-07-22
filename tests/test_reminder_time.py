from datetime import datetime, timedelta, timezone

import pytest

from anima.llm.handler import _parse_beijing_reminder_time


def test_reminder_time_interprets_iso_as_beijing_time():
    value = _parse_beijing_reminder_time("2026-07-13T20:30")
    expected = datetime(2026, 7, 13, 20, 30, tzinfo=timezone(timedelta(hours=8))).timestamp()
    assert value == expected


def test_reminder_time_rejects_ambiguous_formats():
    with pytest.raises(ValueError):
        _parse_beijing_reminder_time("2026/07/13 20:30")
