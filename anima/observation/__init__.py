"""R10-01 Reality Observation Layer —— 统一现实观测层。

把 Runtime 异常 / Audit DENY / 用户反馈 / Agent 失败 归一化为 ObservationEvent，
供 R10-02 评估引擎与 R8 自主开发闭环消费。观测层只归集、不自我授权。
"""
from .event import (
    EVENT_AGENT_FAILURE,
    EVENT_AUDIT_DENY,
    EVENT_HEALTH_DEGRADED,
    EVENT_RUNTIME_ERROR,
    EVENT_USER_FEEDBACK,
    SOURCE_AGENT,
    SOURCE_AUDIT,
    SOURCE_FEEDBACK,
    SOURCE_MONITOR,
    ObservationEvent,
    severity_rank,
)
from .collector import collect_all, to_dev_issues
from .audit_analyzer import collect_from_audit
from .runtime_monitor import collect_from_runtime
from .user_feedback import collect_from_feedback

__all__ = [
    "ObservationEvent",
    "severity_rank",
    "collect_all",
    "to_dev_issues",
    "collect_from_audit",
    "collect_from_runtime",
    "collect_from_feedback",
    "EVENT_RUNTIME_ERROR",
    "EVENT_AUDIT_DENY",
    "EVENT_USER_FEEDBACK",
    "EVENT_AGENT_FAILURE",
    "EVENT_HEALTH_DEGRADED",
    "SOURCE_MONITOR",
    "SOURCE_AUDIT",
    "SOURCE_FEEDBACK",
    "SOURCE_AGENT",
]
