"""可持久化的主动表达议程。"""
from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass

from anima.core.persistence import PersistenceProvider

logger = logging.getLogger("anima.cognitive.agenda")


def is_garbled_text(text: str) -> bool:
    """检测文本是否包含编码损坏/乱码。

    典型场景：JSON 文件编码损坏后 content 变成 "??????,????" 或 "ï¿½ï¿½ï¿½"。
    """
    if not text or not text.strip():
        return True
    s = text.strip()
    # 1. Unicode 替换字符 U+FFFD
    if '\ufffd' in s:
        return True
    # 2. 几乎全是问号/标点（编码损坏的典型特征）
    stripped = re.sub(r'[\s\u3000]', '', s)  # 去空白
    if len(stripped) >= 3:
        qmarks = sum(1 for c in stripped if c in '?\uff1f')  # 半角+全角问号
        if qmarks / len(stripped) > 0.5:
            return True
    # 3. 可读字符（CJK + 字母 + 数字）占比过低
    if len(s) >= 5:
        readable = sum(1 for c in s if '\u4e00' <= c <= '\u9fff' or c.isalnum())
        if readable / len(s) < 0.2:
            return True
    return False


@dataclass
class AgendaItem:
    group_id: int
    text_intent: str
    priority: float = 0.5
    not_before_at: float = 0.0
    expires_at: float = 0.0
    id: str = ""
    status: str = "pending"
    intent_id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = uuid.uuid4().hex[:12]
        self.group_id = int(self.group_id)
        self.priority = max(0.0, min(1.0, float(self.priority)))
        self.not_before_at = max(0.0, float(self.not_before_at or 0.0))
        self.expires_at = max(0.0, float(self.expires_at or 0.0))
        self.text_intent = self.text_intent.strip()[:500]
        # P1: 乱码/无意义内容不允许进入议程队列
        if is_garbled_text(self.text_intent):
            raise ValueError(f"议程文本为乱码或无意义内容: {self.text_intent[:60]!r}")


class AgendaQueue:
    """原子落盘的 FIFO/优先级混合队列。"""

    def __init__(self, persistence: PersistenceProvider):
        self.persistence = persistence
        self.items: list[AgendaItem] = []
        self.restore()

    def restore(self) -> None:
        self.items = []
        for raw in self.persistence.read_json("agenda.json", []) or []:
            try:
                self.items.append(AgendaItem(**raw))
            except (TypeError, ValueError):
                continue
        self._prune_expired(save=False)

    def save(self) -> None:
        self.persistence.write_json("agenda.json", [asdict(item) for item in self.items])

    def push(self, item: AgendaItem) -> AgendaItem:
        if item.group_id <= 0 or not item.text_intent:
            raise ValueError("议程必须包含有效群号和表达意图")
        self.items.append(item)
        self.save()
        return item

    def peek_ready(self, group_id: int | None = None, now: float | None = None) -> AgendaItem | None:
        now = time.time() if now is None else now
        self._prune_expired(now)
        candidates = [
            item for item in self.items
            if item.status == "pending" and item.not_before_at <= now
            and (group_id is None or item.group_id == group_id)
        ]
        return max(candidates, key=lambda item: item.priority, default=None)

    def ready_items(self, group_id: int | None = None,
                    now: float | None = None) -> list["AgendaItem"]:
        """返回全部「到点可发」的议程（未排序）。

        P1-4：``peek_ready`` 只给最高优先级那一条，队首被挡（群 autonomy=false、
        全局冷却、claim 失败）时整轮就什么都不做。给 autonomy 一个可排序的完整
        候选集，由 ``core.proactive.rank_agenda`` 排序 + 逐个尝试。
        """
        now = time.time() if now is None else now
        self._prune_expired(now)
        return [
            item for item in self.items
            if item.status == "pending" and item.not_before_at <= now
            and (group_id is None or item.group_id == group_id)
        ]

    def consume(self, item_id: str) -> bool:
        return self._set_status(item_id, "consumed")

    def cancel(self, item_id: str) -> bool:
        return self._set_status(item_id, "cancelled")

    def pending(self) -> list[AgendaItem]:
        self._prune_expired()
        return [item for item in self.items if item.status == "pending"]

    def _set_status(self, item_id: str, status: str) -> bool:
        for item in self.items:
            if item.id == item_id and item.status == "pending":
                item.status = status
                self.save()
                return True
        return False

    def _prune_expired(self, now: float | None = None, *, save: bool = True) -> None:
        now = time.time() if now is None else now
        changed = False
        for item in self.items:
            if item.status == "pending" and item.expires_at and item.expires_at <= now:
                item.status = "expired"
                changed = True
        if changed and save:
            self.save()
