"""R10-01 —— 观测聚合器：把分散信号汇总为统一 ObservationEvent 列表。

同时提供 ``to_dev_issues`` 桥接到 R8 自主开发闭环（DevIssue），让观测到的现实问题
可以直接进入「提案 → 审查 → 部署」流程。观测层只归集、不决策。
"""
from __future__ import annotations

from typing import Any

from .audit_analyzer import collect_from_audit
from .event import ObservationEvent
from .runtime_monitor import collect_from_runtime
from .user_feedback import collect_from_feedback

# 默认最小严重度（低于此的不进闭环，仅留存观测）
_DEFAULT_MIN_SEVERITY = "low"


def collect_all(
    core: Any = None,
    *,
    audit: Any = None,
    audit_denials: list[dict[str, Any]] | None = None,
    feedback_queue: list[dict[str, Any]] | None = None,
    window_sec: int = 86400,
    min_denials: int = 5,
) -> list[ObservationEvent]:
    """聚合全部观测源。

    - audit：AuditLogger 实例（recent() 可用）。若未传则尝试 ``core.audit``。
    - audit_denials：预筛的 DENY 事件（可选，覆盖 AuditLogger 扫描）。
    - feedback_queue：用户/Owner 反馈列表。
    """
    events: list[ObservationEvent] = []

    audit_obj = audit or getattr(core, "audit", None)
    events.extend(collect_from_audit(audit_obj, window_sec=window_sec, min_denials=min_denials))
    if audit_denials:
        # 预筛 DENY 事件直接复用审计分析逻辑（传空 audit，用注入列表）
        events.extend(_events_from_denial_list(audit_denials, min_denials=min_denials))
    if core is not None:
        events.extend(collect_from_runtime(core))
    events.extend(collect_from_feedback(feedback_queue))
    return events


def _events_from_denial_list(denial_events: list[dict[str, Any]],
                            *, min_denials: int = 5) -> list[ObservationEvent]:
    """把预筛的 DENY 事件聚合为 ObservationEvent（与 audit_analyzer 同口径）。"""
    from .audit_analyzer import _DENY_TYPES  # noqa: F401  (保留类型常量引用一致性)
    from .event import EVENT_AUDIT_DENY, SOURCE_AUDIT, ObservationEvent

    by_target: dict[str, int] = {}
    sample: dict[str, dict[str, Any]] = {}
    for ev in denial_events:
        if ev.get("type") in _DENY_TYPES or "denied" in str(ev.get("type", "")):
            tgt = str(ev.get("agent") or ev.get("target") or ev.get("tool") or "unknown")
            by_target[tgt] = by_target.get(tgt, 0) + 1
            sample.setdefault(tgt, ev)
    out: list[ObservationEvent] = []
    for tgt, cnt in by_target.items():
        if cnt < min_denials:
            continue
        s = sample.get(tgt, {})
        out.append(ObservationEvent(
            type=EVENT_AUDIT_DENY, source=SOURCE_AUDIT, severity="high",
            title=f"审查高频拒绝/越权：{tgt}（{cnt} 次）",
            detail="审计证据链显示该目标被反复拒绝或越权尝试，需评估护栏/权限模型缺口。",
            evidence=f"sample_event={s.get('type')} ts={s.get('ts')}",
            target=tgt,
        ))
    return out


def to_dev_issues(events: list[ObservationEvent]) -> list[Any]:
    """把观测事件桥接为 R8 DevIssue，进入自主开发闭环。

    仅把 >= normal 的事件转 Issue；high/critical 优先级更高。
    """
    from anima.agent.issue_sources import (
        ISSUE_SOURCE_AUDIT,
        DevIssue,
        _SEVERITY_ORDER,
    )

    issues: list[DevIssue] = []
    for ev in events:
        rank = _SEVERITY_ORDER.get(ev.severity, 1)
        if rank < _SEVERITY_ORDER.get("normal", 1):
            continue
        issues.append(DevIssue(
            title=f"[观测] {ev.title}",
            detail=f"{ev.detail}\n证据：{ev.evidence}".strip(),
            source=ISSUE_SOURCE_AUDIT,
            capability="task.plan",
            required_scopes=frozenset({"read"}),
            severity="high" if ev.severity in ("high", "critical") else "normal",
        ))
    return issues
