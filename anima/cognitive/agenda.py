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
    # 来源标签（promise_followup / commitment / dream / system …）。
    # 🔴 2026-09-20 补：`propose_agenda(source=…)` 原先只进了 subject 与 audit，
    # **没落进 AgendaItem** ⇒ 盘上的 agenda.json 完全看不出条目来自哪条通道，
    # 清理假承诺时只能靠"文本形状"反推（见 reports/2026-09-20_intent_fix_p0.md）。
    # 加为**带默认值的尾字段**：旧文件缺该键 → 默认 "system"，`AgendaItem(**raw)` 不炸。
    source: str = "system"
    #: 🔴 2026-10-26：**这条议程背后的资料在哪**（如探索笔记 id）。
    #:
    #: 生产事故（10-26 09:02）：
    #:   09:02:26 它分享「诶本喵刚刷到一个好玩的，法国那边又开始抗议了，抓了一千多人呢。」
    #:   09:02:40 群友追「@MengKing 发」  ← 想看详情
    #:   09:02:47 它回「发什么发，当本喵是自动复读机呢喵……」
    #:
    #: 根因：`_maybe_share` 只把 `summary[:60]` 拼进 intent，**笔记原文没有跟着走**
    #: ⇒ 它说那句话时手上只有 60 字，被追问细节自然答不上，只能当成"让我复读"而拒绝。
    #:
    #: `note_id` 让「议程 → 原始资料」可回溯：追问时可以按 id 取回完整笔记。
    #: 尾部带默认值：旧 agenda.json 缺该键也不炸。
    note_id: str = ""

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

    #: 终态条目（`consumed` / `cancelled` / `expired`）的保留上限；`pending` **全留**。
    #: 🔴 2026-09-20：此前 `save()` 全量写 `self.items`，而 `_prune_expired()` 只把
    #:   pending 转成 expired、**从不删除** ⇒ `agenda.json` 只增不减（对比
    #:   `episodes[-1000:]` / `commitments[-500:]` 都有上限）。终态条目已无行为作用，
    #:   只占盘、拉长排查时的噪声。
    _TERMINAL_KEEP = 200

    def save(self) -> None:
        terminal_idx = [i for i, item in enumerate(self.items) if item.status != "pending"]
        overflow = len(terminal_idx) - self._TERMINAL_KEEP
        if overflow > 0:
            # 丢**最旧**的终态，保留原顺序（`peek_ready` 取 max priority，顺序不敏感，
            # 但重排会让"和上一轮日志对比"变难）。
            drop = set(terminal_idx[:overflow])
            self.items = [it for i, it in enumerate(self.items) if i not in drop]
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
