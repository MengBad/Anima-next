"""高危工具的持久化 owner 审批队列。"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from anima.core.persistence import PersistenceProvider


@dataclass
class PendingApproval:
    tool: str
    args: dict[str, Any]
    context: dict[str, Any]
    permission_level: int
    expires_at: float
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "pending"
    created_at: float = field(default_factory=time.time)
    reviewed_at: float | None = None
    reviewer: int | None = None

    @property
    def args_digest(self) -> str:
        raw = json.dumps(self.args, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class ApprovalStore:
    """仅保存必要的可重放参数；对外状态绝不回显原参数。"""

    def __init__(self, persistence: PersistenceProvider):
        self.persistence = persistence
        self.items: dict[str, PendingApproval] = {}
        self._load()

    def _load(self) -> None:
        for raw in self.persistence.read_json("pending_approvals.json", []) or []:
            try:
                item = PendingApproval(**raw)
                self.items[item.id] = item
            except (TypeError, ValueError):
                continue
        self._expire()

    def _save(self) -> None:
        self.persistence.write_json("pending_approvals.json", [asdict(item) for item in self.items.values()])

    def create(self, tool: str, args: dict[str, Any], context: dict[str, Any],
               permission_level: int, ttl_sec: float) -> PendingApproval:
        item = PendingApproval(
            tool=tool, args=dict(args), context=dict(context), permission_level=int(permission_level),
            expires_at=time.time() + max(30.0, min(float(ttl_sec), 86400.0)),
        )
        self.items[item.id] = item
        self._save()
        return item

    def get_pending(self, approval_id: str) -> PendingApproval | None:
        self._expire()
        item = self.items.get(approval_id)
        return item if item and item.status == "pending" else None

    def mark(self, approval_id: str, status: str, reviewer: int) -> PendingApproval:
        item = self.get_pending(approval_id)
        if item is None:
            raise ValueError("未找到待审批项目或审批已过期")
        item.status = status
        item.reviewer = int(reviewer)
        item.reviewed_at = time.time()
        self._save()
        return item

    def pending(self) -> list[PendingApproval]:
        self._expire()
        return [item for item in self.items.values() if item.status == "pending"]

    def _expire(self) -> None:
        now = time.time()
        changed = False
        for item in self.items.values():
            if item.status == "pending" and item.expires_at <= now:
                item.status = "expired"
                changed = True
        if changed:
            self._save()
