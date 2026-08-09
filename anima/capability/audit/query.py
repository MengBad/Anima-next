"""F8.7 —— 审计只读查询 / 重放（与写侧 Protocol 分离）。

``AuditSink`` Protocol 是纯写契约（``(event) -> None``，F8.3 冻结）。查询/重放不属于
协议，也不能改协议——因此它们是 sink 实现**自带**的只读方法（R3，用户 Q3/Q5 裁定）。

``AuditQuery`` 不写任何东西，只读 store 并过滤：
- ``query()`` 支持按 capability_id / outcome / selected_provider / 时间范围 /
  execution_key 过滤（可组合），返回 ``list[ResolutionEvent]``；
- ``replay(execution_key)`` 按身份锚重建恰好一条事件（execution_key 是审计与
  execution ledger 对账的 I3 身份锚，见 governance/audit.py 模块文档）。

查询是纯函数式过滤，不修改 store 状态。
"""
from __future__ import annotations

from typing import Any

from anima.capability.governance.audit import ResolutionEvent, ResolutionOutcome
from anima.capability.audit.storage import JsonlAuditStore


class AuditQuery:
    """对 ``JsonlAuditStore`` 的只读查询面（写侧在 sink / store，这里只读）。"""

    def __init__(self, store: JsonlAuditStore) -> None:
        self._store = store

    def query(
        self,
        *,
        capability_id: str | None = None,
        outcome: ResolutionOutcome | None = None,
        selected_provider: str | None = None,
        start: float | None = None,
        end: float | None = None,
        execution_key: tuple | None = None,
        limit: int | None = None,
    ) -> list[ResolutionEvent]:
        """按过滤键筛选事件。所有键可组合；``start/end`` 基于 ``event.timestamp``。

        - ``start``：``timestamp >= start``（含）
        - ``end``：``timestamp <= end``（含）
        - ``execution_key``：精确匹配整个 tuple（I3 身份锚）
        """
        events = self._store.read_events()
        if capability_id is not None:
            events = [e for e in events if e.capability_id == capability_id]
        if outcome is not None:
            events = [e for e in events if e.outcome == outcome]
        if selected_provider is not None:
            events = [e for e in events if e.selected_provider == selected_provider]
        if start is not None:
            events = [e for e in events if e.timestamp >= start]
        if end is not None:
            events = [e for e in events if e.timestamp <= end]
        if execution_key is not None:
            events = [e for e in events if e.execution_key == execution_key]
        if limit is not None:
            events = events[:limit]
        return events

    def replay(self, execution_key: tuple) -> ResolutionEvent | None:
        """按 execution_key 重建恰好一条事件；无命中返回 None。"""
        hits = [e for e in self._store.read_events() if e.execution_key == execution_key]
        return hits[0] if hits else None
