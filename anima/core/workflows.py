"""面向自动化的轻量工作流账本：触发、交付意图与最终结果分离。"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass

from anima.core.persistence import PersistenceProvider


@dataclass
class WorkflowRun:
    kind: str
    source_id: str
    target_group: int
    content: str
    event_key: str = ""
    id: str = ""
    status: str = "triggered"  # triggered|awaiting_delivery|completed|cancelled|failed
    intent_id: str = ""
    attempts: int = 0
    created_at: float = 0.0
    completed_at: float = 0.0
    last_reason: str = ""

    def __post_init__(self) -> None:
        self.id = self.id or f"wf_{uuid.uuid4().hex[:12]}"
        self.kind, self.source_id = str(self.kind)[:64], str(self.source_id)[:120]
        self.target_group = int(self.target_group)
        self.content, self.event_key = str(self.content).strip()[:500], str(self.event_key)[:200]
        self.created_at = float(self.created_at or time.time())
        if self.status not in {"triggered", "awaiting_delivery", "completed", "cancelled", "failed"}:
            self.status = "triggered"


class WorkflowStore:
    def __init__(self, persistence: PersistenceProvider):
        self.persistence = persistence
        self.items: dict[str, WorkflowRun] = {}
        self.restore()

    def restore(self) -> None:
        self.items = {}
        for raw in self.persistence.read_json("workflows.json", []) or []:
            try:
                item = WorkflowRun(**raw)
            except (TypeError, ValueError):
                continue
            self.items[item.id] = item

    def save(self) -> None:
        self.persistence.write_json("workflows.json", [asdict(item) for item in self.items.values()])

    def trigger(self, kind: str, source_id: str, target_group: int, content: str, *, event_key: str = "") -> WorkflowRun:
        if int(target_group) <= 0 or not str(content).strip():
            raise ValueError("工作流必须包含目标群和内容")
        for item in self.items.values():
            if (item.status in {"triggered", "awaiting_delivery"} and item.kind == kind
                    and item.source_id == str(source_id) and item.target_group == int(target_group)
                    and item.event_key == str(event_key) and item.content == str(content).strip()):
                return item
        item = WorkflowRun(kind, source_id, target_group, content, event_key)
        self.items[item.id] = item
        self.save()
        return item

    def bind_intent(self, workflow_id: str, intent_id: str) -> bool:
        item = self.items.get(workflow_id)
        if item is None or item.status not in {"triggered", "awaiting_delivery"}:
            return False
        item.intent_id, item.status, item.attempts = str(intent_id), "awaiting_delivery", item.attempts + 1
        self.save()
        return True

    def complete_by_intent(self, intent_id: str, reason: str = "QQ 出站确认成功") -> int:
        changed = 0
        for item in self.items.values():
            if item.intent_id == str(intent_id) and item.status == "awaiting_delivery":
                item.status, item.completed_at, item.last_reason = "completed", time.time(), str(reason)[:240]
                changed += 1
        if changed:
            self.save()
        return changed

    def cancel(self, workflow_id: str, reason: str = "Owner 取消") -> bool:
        item = self.items.get(workflow_id)
        if item is None or item.status in {"completed", "cancelled", "failed"}:
            return False
        item.status, item.completed_at, item.last_reason = "cancelled", time.time(), str(reason)[:240]
        self.save()
        return True

    def fail_by_intent(self, intent_id: str, reason: str = "关联意图失败或过期") -> int:
        """意图过期/失败时同步关闭等待交付的工作流，避免悬挂。"""
        changed = 0
        for item in self.items.values():
            if item.intent_id == str(intent_id) and item.status in {"triggered", "awaiting_delivery"}:
                item.status, item.completed_at, item.last_reason = "failed", time.time(), str(reason)[:240]
                changed += 1
        if changed:
            self.save()
        return changed

    def list(self, limit: int = 100) -> list[WorkflowRun]:
        return sorted(self.items.values(), key=lambda item: item.created_at, reverse=True)[:max(1, min(int(limit), 500))]

    def summary(self) -> dict[str, int]:
        result = {key: 0 for key in ("triggered", "awaiting_delivery", "completed", "cancelled", "failed")}
        for item in self.items.values(): result[item.status] = result.get(item.status, 0) + 1
        result["active"] = result["triggered"] + result["awaiting_delivery"]
        return result
