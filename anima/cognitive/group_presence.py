"""群在场状态：为主动性和表达上下文提供可解释、可持久的轻量状态机。"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass

from anima.core.persistence import PersistenceProvider


@dataclass
class GroupPresence:
    group_id: int
    state: str = "NOT_PRESENT"
    message_count: int = 0
    last_seen: float = 0.0
    last_summoned: float = 0.0


class GroupPresenceManager:
    def __init__(self, data_dir: str):
        self.persistence = PersistenceProvider(data_dir)
        self.items: dict[int, GroupPresence] = {}
        self._load()

    def _load(self) -> None:
        for raw in self.persistence.read_json("group_presence.json", []) or []:
            try:
                item = GroupPresence(**raw)
                self.items[item.group_id] = item
            except (TypeError, ValueError):
                continue

    def _save(self) -> None:
        self.persistence.write_json("group_presence.json", [asdict(item) for item in self.items.values()])

    def observe(self, group_id: int, *, summoned: bool = False) -> GroupPresence:
        now = time.time()
        item = self.items.setdefault(int(group_id), GroupPresence(group_id=int(group_id)))
        item.message_count += 1
        item.last_seen = now
        if summoned:
            item.state = "SUMMONED"
            item.last_summoned = now
        elif item.message_count >= 5:
            item.state = "GETTING_FAMILIAR"
        else:
            item.state = "OBSERVATION"
        self._save()
        return item

    def get(self, group_id: int | None) -> GroupPresence | None:
        return self.items.get(int(group_id)) if group_id else None

    def fragment(self, group_id: int | None) -> str:
        item = self.get(group_id)
        if not item:
            return ""
        labels = {"NOT_PRESENT": "未在场", "SUMMONED": "被呼唤", "GETTING_FAMILIAR": "逐渐熟悉", "OBSERVATION": "安静观察"}
        return f"当前群在场状态：{labels.get(item.state, item.state)}。"
