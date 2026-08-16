"""R10-01 —— 从用户/Owner 反馈派生观测事件。"""
from __future__ import annotations

from typing import Any

from .event import (
    EVENT_USER_FEEDBACK,
    SOURCE_FEEDBACK,
    ObservationEvent,
)

# 反馈严重度映射（用户明确标记为 bug/不满 → high）
_HIGH_KEYWORDS = ("bug", "错误", "失败", "崩溃", "不对", "拒绝", "报错", "异常")


def collect_from_feedback(queue: list[dict[str, Any]] | None = None) -> list[ObservationEvent]:
    """把反馈条目归一化为 ObservationEvent。

    queue 条目含 ``title`` / ``detail`` / 可选 ``severity`` / ``target``。
    """
    events: list[ObservationEvent] = []
    for item in (queue or []):
        title = str(item.get("title", "")).strip()
        if not title:
            continue
        raw_sev = str(item.get("severity", "")).strip().lower()
        if raw_sev in ("low", "normal", "high", "critical"):
            severity = raw_sev
        else:
            severity = "high" if any(k in title.lower() or k in str(item.get("detail", "")) for k in _HIGH_KEYWORDS) else "normal"
        events.append(ObservationEvent(
            type=EVENT_USER_FEEDBACK,
            source=SOURCE_FEEDBACK,
            severity=severity,
            title=title,
            detail=str(item.get("detail", "")),
            evidence=str(item.get("evidence", "")),
            target=str(item.get("target", "")),
        ))
    return events
