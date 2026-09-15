"""主体意图账本：让自主表达拥有唯一、可审计的生命周期。"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from anima.core.persistence import PersistenceProvider


OPEN_STATUSES = frozenset({"queued", "selected", "dispatched"})
TERMINAL_STATUSES = frozenset({"completed", "cancelled", "expired", "failed"})


@dataclass
class IntentRecord:
    kind: str
    source: str
    target_kind: str
    target_id: int
    content: str
    priority: float = 0.5
    not_before_at: float = 0.0
    expires_at: float = 0.0
    id: str = ""
    status: str = "queued"
    created_at: float = 0.0
    selected_at: float = 0.0
    completed_at: float = 0.0
    last_reason: str = ""
    attempts: int = 0

    def __post_init__(self) -> None:
        self.id = self.id or f"int_{uuid.uuid4().hex[:12]}"
        self.kind, self.source = str(self.kind or "expression")[:64], str(self.source or "system")[:64]
        self.target_kind, self.target_id = str(self.target_kind or "group")[:32], int(self.target_id)
        self.content = str(self.content or "").strip()[:500]
        self.priority = max(0.0, min(1.0, float(self.priority)))
        self.not_before_at, self.expires_at = max(0.0, float(self.not_before_at or 0.0)), max(0.0, float(self.expires_at or 0.0))
        self.created_at = float(self.created_at or time.time())
        if self.status not in OPEN_STATUSES | TERMINAL_STATUSES:
            self.status = "queued"


class IntentLedger:
    """持久化意图状态机；不保存 LLM prompt 或聊天原文。"""

    def __init__(self, persistence: PersistenceProvider, path: str = "intent_ledger.json"):
        self.persistence, self.path, self.items = persistence, path, {}
        self.on_expired = None  # optional Callable[[str], None]
        self.restore()

    def restore(self) -> None:
        self.items = {}
        for raw in self.persistence.read_json(self.path, []) or []:
            try:
                item = IntentRecord(**raw)
            except (TypeError, ValueError):
                continue
            self.items[item.id] = item
        self.expire(save=False)

    def save(self) -> None:
        self.persistence.write_json(self.path, [asdict(item) for item in self.items.values()])

    def create(self, *, kind: str, source: str, target_kind: str, target_id: int,
               content: str, priority: float = 0.5, not_before_at: float = 0.0,
               expires_at: float = 0.0) -> IntentRecord:
        if target_id <= 0 or not str(content).strip():
            raise ValueError("意图必须包含有效目标和内容")
        self.expire(save=False)
        normalized = str(content).strip()
        now_ts = time.time()
        try:
            from difflib import SequenceMatcher
        except Exception:  # pragma: no cover
            SequenceMatcher = None
        for item in self.items.values():
            if (item.status in OPEN_STATUSES and item.kind == kind and item.source == source
                    and item.target_kind == target_kind and item.target_id == int(target_id)):
                if item.content == normalized:
                    return item
                # 深审 P2-4：同源同目标 24h 内高相似内容（换措辞重复承诺）拒绝
                # 重复建档——content 全等去重挡不住动态文本
                try:
                    if (now_ts - float(item.created_at or 0) < 86400
                            and SequenceMatcher is not None
                            and SequenceMatcher(None, item.content, normalized).ratio() >= 0.8):
                        return item
                except Exception:
                    pass
        item = IntentRecord(kind, source, target_kind, target_id, normalized, priority, not_before_at, expires_at)
        self.items[item.id] = item
        self.save()
        return item

    def get(self, intent_id: str) -> IntentRecord | None:
        return self.items.get(intent_id)

    def claim(self, intent_id: str, now: float | None = None) -> IntentRecord | None:
        now = time.time() if now is None else now
        self.expire(now)
        item = self.items.get(intent_id)
        if item is None or item.status != "queued" or item.not_before_at > now:
            return None
        item.status, item.selected_at, item.attempts = "selected", now, item.attempts + 1
        self.save()
        return item

    def mark_dispatched(self, intent_id: str, reason: str = "已提交出站") -> bool:
        """出站动作已发出但尚未确认完成时标记为 dispatched。"""
        item = self.items.get(intent_id)
        if item is None or item.status != "selected":
            return False
        item.status, item.last_reason = "dispatched", str(reason)[:240]
        self.save()
        return True

    def release(self, intent_id: str, reason: str = "", *, max_attempts: int = 0) -> bool:
        """出站未成功时回到 queued；超过 max_attempts 则标记 failed，避免死循环。"""
        item = self.items.get(intent_id)
        if item is None or item.status not in {"selected", "dispatched"}:
            return False
        reason_text = str(reason)[:240]
        if max_attempts > 0 and int(item.attempts or 0) >= int(max_attempts):
            item.status, item.completed_at, item.last_reason = (
                "failed", time.time(), f"连续 {item.attempts} 次未满足出站条件：{reason_text}"[:240],
            )
            self.save()
            return True
        item.status, item.last_reason = "queued", reason_text
        self.save()
        return True

    def complete(self, intent_id: str, reason: str = "") -> bool:
        item = self.items.get(intent_id)
        if item is None or item.status in TERMINAL_STATUSES:
            return False
        item.status, item.completed_at, item.last_reason = "completed", time.time(), str(reason)[:240]
        self.save()
        return True

    def fail(self, intent_id: str, reason: str = "") -> bool:
        item = self.items.get(intent_id)
        if item is None or item.status in TERMINAL_STATUSES:
            return False
        item.status, item.completed_at, item.last_reason = "failed", time.time(), str(reason)[:240]
        self.save()
        return True

    def cancel(self, intent_id: str, reason: str = "") -> bool:
        item = self.items.get(intent_id)
        if item is None or item.status in TERMINAL_STATUSES:
            return False
        item.status, item.completed_at, item.last_reason = "cancelled", time.time(), str(reason)[:240]
        self.save()
        return True

    def expire(self, now: float | None = None, *, save: bool = True) -> int:
        now = time.time() if now is None else now
        changed = 0
        expired_ids: list[str] = []
        for item in self.items.values():
            if item.status in OPEN_STATUSES and item.expires_at and item.expires_at <= now:
                item.status, item.completed_at, item.last_reason = "expired", now, "超过有效期"
                expired_ids.append(item.id)
                changed += 1
        if changed and save:
            self.save()
        if expired_ids and callable(self.on_expired):
            for intent_id in expired_ids:
                try:
                    self.on_expired(intent_id)
                except Exception:
                    pass
        return changed

    def list(self, *, include_terminal: bool = True, limit: int = 80) -> list[IntentRecord]:
        self.expire()
        values = [item for item in self.items.values() if include_terminal or item.status in OPEN_STATUSES]
        values.sort(key=lambda item: (item.status in TERMINAL_STATUSES, -item.priority, -item.created_at))
        return values[:max(1, min(int(limit), 500))]

    def summary(self) -> dict[str, int]:
        self.expire()
        out = {status: 0 for status in OPEN_STATUSES | TERMINAL_STATUSES}
        for item in self.items.values():
            out[item.status] = out.get(item.status, 0) + 1
        out["open"] = sum(out.get(status, 0) for status in OPEN_STATUSES)
        return out


class SubjectOrchestrator:
    """主体层的唯一表达入口；工作流和承诺会复用同一账本。"""

    def __init__(self, ledger: IntentLedger):
        self.ledger = ledger

    def propose_expression(self, group_id: int, content: str, *, source: str,
                           priority: float = 0.5, not_before_at: float = 0.0,
                           expires_at: float = 0.0) -> IntentRecord:
        return self.ledger.create(kind="expression", source=source, target_kind="group", target_id=group_id,
                                  content=content, priority=priority, not_before_at=not_before_at,
                                  expires_at=expires_at)

    def claim_agenda(self, agenda_item: Any, now: float | None = None) -> IntentRecord | None:
        intent_id = str(getattr(agenda_item, "intent_id", "") or "")
        if not intent_id:
            legacy = self.propose_expression(int(agenda_item.group_id), str(agenda_item.text_intent),
                                             source="legacy_agenda", priority=float(getattr(agenda_item, "priority", .5)),
                                             not_before_at=float(getattr(agenda_item, "not_before_at", 0.0)),
                                             expires_at=float(getattr(agenda_item, "expires_at", 0.0)))
            agenda_item.intent_id, intent_id = legacy.id, legacy.id
        return self.ledger.claim(intent_id, now)
