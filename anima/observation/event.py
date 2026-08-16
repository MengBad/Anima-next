"""R10-01 Reality Observation Layer —— 统一观测事件模型。

把分散的现实信号（Runtime 异常 / Audit DENY / 用户反馈 / Agent 失败）
归一化为 ``ObservationEvent``，供 R10-02 评估引擎与 R8 自主开发闭环消费。

设计纪律：
- Observation 只**观测与归集**，不修改生产、不自我授权（No Hidden Authority）。
- 每个事件都带 ``evidence``（可溯源），符合 R6.5 Audit Everything。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

# 观测事件类型（与现实信号一一对应）
EVENT_RUNTIME_ERROR = "runtime_error"
EVENT_AUDIT_DENY = "audit_deny"
EVENT_USER_FEEDBACK = "user_feedback"
EVENT_AGENT_FAILURE = "agent_failure"
EVENT_HEALTH_DEGRADED = "health_degraded"

# 信号来源
SOURCE_MONITOR = "monitor"
SOURCE_AUDIT = "audit"
SOURCE_FEEDBACK = "feedback"
SOURCE_AGENT = "agent"
SOURCE_GITHUB = "github"  # R10-01 暂未接入（需 API token，后续）

_SEVERITY_ORDER = {"low": 0, "normal": 1, "high": 2, "critical": 3}


@dataclass
class ObservationEvent:
    """一条统一观测事件：现实发生了什么、严重程度、证据。"""

    type: str
    source: str
    severity: str = "normal"
    title: str = ""
    detail: str = ""
    evidence: str = ""
    target: str = ""
    ts: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: f"obs-{uuid.uuid4().hex[:8]}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "source": self.source,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "evidence": self.evidence,
            "target": self.target,
            "ts": self.ts,
        }

    @property
    def severity_rank(self) -> int:
        return _SEVERITY_ORDER.get(self.severity, 1)


def severity_rank(sev: str) -> int:
    return _SEVERITY_ORDER.get(sev, 1)
