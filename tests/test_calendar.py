from datetime import datetime, timedelta, timezone

from anima.cognitive.calendar import CalendarStore
from anima.core.persistence import PersistenceProvider


def _beijing_ts(day: str, hour: int, minute: int) -> float:
    return datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}:00").replace(
        tzinfo=timezone(timedelta(hours=8))
    ).timestamp()


def test_daily_calendar_persists_and_only_triggers_once_per_day(tmp_path):
    store = CalendarStore(PersistenceProvider(str(tmp_path)))
    event = store.add_daily(123, "08:30", "早安")
    before = _beijing_ts("2026-07-14", 8, 29)
    at_time = _beijing_ts("2026-07-14", 8, 30)
    assert store.due(before) == []
    assert [item.id for item in store.due(at_time)] == [event.id]
    assert store.due(at_time + 3600) == []
    restored = CalendarStore(PersistenceProvider(str(tmp_path)))
    assert restored.due(at_time + 7200) == []
    assert [item.id for item in restored.due(_beijing_ts("2026-07-15", 8, 31))] == [event.id]


def test_calendar_cancel_and_invalid_time(tmp_path):
    store = CalendarStore(PersistenceProvider(str(tmp_path)))
    event = store.add_daily(123, "00:05", "喝水")
    assert store.cancel(event.id)
    assert store.due(_beijing_ts("2026-07-14", 23, 59)) == []
    try:
        store.add_daily(123, "25:00", "坏时间")
    except ValueError:
        pass
    else:
        raise AssertionError("非法时间必须拒绝")


def test_weekly_calendar_only_triggers_matching_weekday(tmp_path):
    store = CalendarStore(PersistenceProvider(str(tmp_path)))
    event = store.add_weekly(123, 1, "09:00", "周一例会")
    monday = _beijing_ts("2026-07-13", 9, 0)  # Monday
    tuesday = _beijing_ts("2026-07-14", 9, 0)
    assert [item.id for item in store.weekly_due(monday)] == [event.id]
    assert store.weekly_due(monday + 60) == []
    assert store.weekly_due(tuesday) == []
