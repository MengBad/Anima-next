"""R10-01 —— 从运行时健康状态派生观测事件。

只读 ``core.health_history`` / ``core.evaluation`` 等既有健康信号，归一化为
``ObservationEvent``；不修改生产、不触发自愈（自愈属于 R13，本层只观测）。
"""
from __future__ import annotations

from typing import Any

from .event import (
    EVENT_HEALTH_DEGRADED,
    SOURCE_MONITOR,
    ObservationEvent,
)


def collect_from_runtime(core: Any, *, now: float | None = None) -> list[ObservationEvent]:
    """把运行时健康信号转观测事件。

    触发条件：
    - 进程未运行 / 渠道未连接 → high
    - 评估出现注意力项（evaluation_attention） → normal
    """
    events: list[ObservationEvent] = []
    health = getattr(core, "health_history", None)
    health_dict = health.health() if health is not None and hasattr(health, "health") else {}

    running = bool(health_dict.get("running"))
    channel = bool(health_dict.get("channel_connected"))

    if not running:
        events.append(ObservationEvent(
            type=EVENT_HEALTH_DEGRADED, source=SOURCE_MONITOR, severity="high",
            title="运行时进程未运行", detail="health_history 显示 running=False。",
            evidence=f"health_keys={sorted(health_dict.keys())}",
        ))
    if not channel:
        events.append(ObservationEvent(
            type=EVENT_HEALTH_DEGRADED, source=SOURCE_MONITOR, severity="high",
            title="QQ 渠道未连接", detail="health_history 显示 channel_connected=False。",
            evidence=f"health_keys={sorted(health_dict.keys())}",
        ))

    evaluation = getattr(core, "evaluation", None)
    eval_dict = evaluation.health() if evaluation is not None and hasattr(evaluation, "health") else {}
    attention = eval_dict.get("evaluation_attention")
    if attention:
        events.append(ObservationEvent(
            type=EVENT_HEALTH_DEGRADED, source=SOURCE_MONITOR, severity="normal",
            title="评估出现需关注项", detail=str(attention),
            evidence=f"evaluation={eval_dict.get('status')}",
        ))
    return events
