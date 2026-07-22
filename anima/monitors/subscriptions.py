"""通用订阅状态机：持久化、状态迁移和去重，不含任何平台抓取代码。"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from anima.core.persistence import PersistenceProvider


@dataclass
class Subscription:
    platform: str
    subject_id: str
    target_groups: list[int]
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    enabled: bool = True
    poll_interval_sec: int = 120
    template: str = "本喵蹲到啦，{name} 开播了：{title}\n{url}"
    last_state: str = "unknown"
    last_event_key: str = ""
    updated_at: float = field(default_factory=time.time)


class SubscriptionStore:
    def __init__(self, persistence: PersistenceProvider, path: str = "monitors/subscriptions.json"):
        self.persistence, self.path = persistence, path
        self.items: dict[str, Subscription] = {}

    def restore(self) -> None:
        raw = self.persistence.read_json(self.path, {"items": []}) or {"items": []}
        self.items = {}
        for item in raw.get("items", []):
            try:
                sub = Subscription(**item)
                self.items[sub.id] = sub
            except (TypeError, ValueError):
                continue

    def save(self) -> None:
        self.persistence.write_json(self.path, {"items": [asdict(item) for item in self.items.values()]})

    def add(self, sub: Subscription) -> Subscription:
        if not sub.target_groups or any(not isinstance(g, int) or g <= 0 for g in sub.target_groups):
            raise ValueError("目标群必须是有效 QQ 群号")
        self.items[sub.id] = sub; self.save(); return sub

    def remove(self, sub_id: str) -> bool:
        removed = self.items.pop(sub_id, None) is not None
        if removed: self.save()
        return removed

    def transition(self, sub_id: str, state: str, event_key: str) -> bool:
        """仅在状态变为 live 且本场 event_key 未通知时返回 True。"""
        sub = self.items[sub_id]
        notify = sub.enabled and state == "live" and (sub.last_state != "live" or sub.last_event_key != event_key)
        sub.last_state, sub.last_event_key, sub.updated_at = state, event_key, time.time()
        self.save()
        return notify

    def transition_on_change(self, sub_id: str, state: str, event_key: str) -> bool:
        """首轮只建立基线；后续状态或内容指纹变化才通知。"""
        sub = self.items[sub_id]
        notify = sub.enabled and sub.last_state != "unknown" and (
            sub.last_state != state or sub.last_event_key != event_key
        )
        sub.last_state, sub.last_event_key, sub.updated_at = state, event_key, time.time()
        self.save()
        return notify
