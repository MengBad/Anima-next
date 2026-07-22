"""最小本地日历：每日事件只转换为 Agenda，不直接发送 QQ。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import time
import uuid

from anima.core.persistence import PersistenceProvider

_BEIJING = timezone(timedelta(hours=8))


@dataclass
class DailyCalendarEvent:
    group_id: int
    hour: int
    minute: int
    text: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    enabled: bool = True
    last_triggered_day: str = ""
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self.group_id = int(self.group_id)
        self.hour, self.minute = int(self.hour), int(self.minute)
        self.text = self.text.strip()[:500]
        if self.group_id <= 0 or not 0 <= self.hour <= 23 or not 0 <= self.minute <= 59 or not self.text:
            raise ValueError("日历事件参数无效")


@dataclass
class WeeklyCalendarEvent(DailyCalendarEvent):
    weekday: int = 1  # ISO: 1=周一 ... 7=周日

    def __post_init__(self) -> None:
        super().__post_init__()
        self.weekday = int(self.weekday)
        if not 1 <= self.weekday <= 7:
            raise ValueError("星期必须是 1-7（周一到周日）")


class CalendarStore:
    def __init__(self, persistence: PersistenceProvider):
        self.persistence = persistence
        self.items: list[DailyCalendarEvent] = []
        for raw in persistence.read_json("calendar_daily.json", []) or []:
            try:
                self.items.append(DailyCalendarEvent(**raw))
            except (TypeError, ValueError):
                continue
        self.weekly_items: list[WeeklyCalendarEvent] = []
        for raw in persistence.read_json("calendar_weekly.json", []) or []:
            try:
                self.weekly_items.append(WeeklyCalendarEvent(**raw))
            except (TypeError, ValueError):
                continue

    def save(self) -> None:
        self.persistence.write_json("calendar_daily.json", [asdict(item) for item in self.items])
        self.persistence.write_json("calendar_weekly.json", [asdict(item) for item in self.weekly_items])

    def add_daily(self, group_id: int, hhmm: str, text: str) -> DailyCalendarEvent:
        try:
            hour_text, minute_text = hhmm.split(":", 1)
            item = DailyCalendarEvent(group_id, int(hour_text), int(minute_text), text)
        except (TypeError, ValueError) as exc:
            raise ValueError("时间必须是 HH:MM（北京时间）") from exc
        self.items.append(item)
        self.save()
        return item

    def add_weekly(self, group_id: int, weekday: int, hhmm: str, text: str) -> WeeklyCalendarEvent:
        try:
            hour_text, minute_text = hhmm.split(":", 1)
            item = WeeklyCalendarEvent(group_id, int(hour_text), int(minute_text), text, weekday=int(weekday))
        except (TypeError, ValueError) as exc:
            raise ValueError("格式为 <星期1-7> <HH:MM>，时间为北京时间") from exc
        self.weekly_items.append(item)
        self.save()
        return item

    def cancel(self, event_id: str) -> bool:
        for item in self.items:
            if item.id == event_id and item.enabled:
                item.enabled = False
                self.save()
                return True
        for item in self.weekly_items:
            if item.id == event_id and item.enabled:
                item.enabled = False
                self.save()
                return True
        return False

    def due(self, now: float | None = None) -> list[DailyCalendarEvent]:
        current = datetime.fromtimestamp(time.time() if now is None else now, _BEIJING)
        day = current.date().isoformat()
        due = [
            item for item in self.items
            if item.enabled and item.last_triggered_day != day
            and (current.hour, current.minute) >= (item.hour, item.minute)
        ]
        for item in due:
            item.last_triggered_day = day
        if due:
            self.save()
        return due

    def weekly_due(self, now: float | None = None) -> list[WeeklyCalendarEvent]:
        current = datetime.fromtimestamp(time.time() if now is None else now, _BEIJING)
        day = current.date().isoformat()
        due = [
            item for item in self.weekly_items
            if item.enabled and item.weekday == current.isoweekday() and item.last_triggered_day != day
            and (current.hour, current.minute) >= (item.hour, item.minute)
        ]
        for item in due:
            item.last_triggered_day = day
        if due:
            self.save()
        return due
