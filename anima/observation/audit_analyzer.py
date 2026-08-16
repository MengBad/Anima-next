"""R10-01 —— 从审计链派生观测事件（高频 DENY / 越权 / 异常）。"""
from __future__ import annotations

import time
from typing import Any

from .event import (
    EVENT_AUDIT_DENY,
    EVENT_RUNTIME_ERROR,
    SOURCE_AUDIT,
    ObservationEvent,
)

# 视为「拒绝/越权」的审计事件类型
_DENY_TYPES = {
    "permission_denied", "capability_denied", "scope_denied",
    "selfcode_rejected", "webui_task_rejected", "webui_evolution_rejected",
}
# 视为「运行时异常」的审计事件类型
_ERROR_TYPES = {"exception", "runtime_error", "tool_error", "agent_failure"}


def collect_from_audit(audit: Any, *, window_sec: int = 86400,
                       min_denials: int = 5) -> list[ObservationEvent]:
    """扫描最近 ``window_sec`` 的审计事件，聚合为观测事件。

    - 高频 DENY（同一 target 被拒 >= min_denials 次）→ high 级 ObservationEvent。
    - 显式异常事件 → 按类型归一为 ObservationEvent。
    """
    if audit is None or not hasattr(audit, "recent"):
        return []
    now = time.time()
    raw = audit.recent(limit=2000)
    events: list[ObservationEvent] = []

    # 高频 DENY 聚合
    by_target: dict[str, int] = {}
    deny_samples: dict[str, dict[str, Any]] = {}
    for ev in raw:
        if (now - float(ev.get("ts", 0))) > window_sec:
            continue
        if ev.get("type") in _DENY_TYPES:
            tgt = str(ev.get("agent") or ev.get("target") or ev.get("tool") or "unknown")
            by_target[tgt] = by_target.get(tgt, 0) + 1
            deny_samples.setdefault(tgt, ev)

    for tgt, cnt in by_target.items():
        if cnt < min_denials:
            continue
        sample = deny_samples.get(tgt, {})
        events.append(ObservationEvent(
            type=EVENT_AUDIT_DENY,
            source=SOURCE_AUDIT,
            severity="high",
            title=f"审查高频拒绝/越权：{tgt}（{cnt} 次/窗口）",
            detail="审计证据链显示该目标被反复拒绝或越权尝试，需评估护栏/权限模型缺口。",
            evidence=f"sample_event={sample.get('type')} ts={sample.get('ts')}",
            target=tgt,
        ))

    # 显式异常事件（每条一事件，便于可追溯）
    for ev in raw:
        if (now - float(ev.get("ts", 0))) > window_sec:
            continue
        if ev.get("type") in _ERROR_TYPES:
            events.append(ObservationEvent(
                type=EVENT_RUNTIME_ERROR,
                source=SOURCE_AUDIT,
                severity="normal",
                title=str(ev.get("message") or ev.get("type") or "运行时异常"),
                detail=str(ev.get("detail") or ""),
                evidence=f"type={ev.get('type')} ts={ev.get('ts')}",
                target=str(ev.get("agent") or ev.get("tool") or ""),
            ))

    return events
