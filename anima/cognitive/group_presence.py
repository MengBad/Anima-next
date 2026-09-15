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


_managers: dict[str, "GroupPresenceManager"] = {}
_default_dir = "data"


def set_group_presence_data_dir(data_dir: str) -> None:
    global _default_dir
    _default_dir = data_dir


def get_group_presence(data_dir=None) -> "GroupPresenceManager":
    """进程级单例（按 data_dir 隔离）。

    深审 P1：simulated_life/autonomy 共 5 个调用点此前 import 不存在的
    get_group_presence，ImportError 被静默吞掉——晚安/晨问/旁观独白/群活跃度
    欲望功能全部失效。与 AnimaCore 的 core.group_presence 共享同一实例。
    """
    global _default_dir
    if data_dir is None:
        data_dir = _default_dir
    if data_dir not in _managers:
        _managers[data_dir] = GroupPresenceManager(data_dir)
    return _managers[data_dir]


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

    def get_active_groups(self, limit: int = 3, within_sec: float = 3600.0) -> list[int]:
        """按最近活跃度返回群 id（message_count 降序），供早安/主动表达挑群。"""
        now = time.time()
        items = [it for it in self.items.values() if now - it.last_seen <= within_sec]
        items.sort(key=lambda it: -it.message_count)
        return [it.group_id for it in items[: max(0, int(limit))]]

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
