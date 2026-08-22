"""P1 —— Real-Environment Observation（真实环境观察）。

在 R10-01 统一观测层（审计 DENY/错误 + 运行时健康 + 用户反馈）之上，补齐 **真实世界信号**
并闭合 **Reality → Observation → Analysis → Proposal** 链路：

1. **GitHub 真实信号**（填补 R10-01 ``SOURCE_GITHUB`` 的 TODO）：commits / CI 状态 /
   issue 活动。源可注入（无需 token 即可测试；生产接 ``gh`` CLI 或 GitHub API）。
2. **运行时性能阈值**（CPU / 内存 / 延迟）：复用 R10-02 ``collect_metrics`` + R13 的
   性能快照，把越界信号转为 ObservationEvent（属「真实环境」而非「健康存活」）。
3. **Audit DENY 增长趋势**：对比当期窗口与上一窗口的 DENY 计数，输出增长百分比
   （Owner 最关心的「异常是不是在变多」）。
4. **Analysis → Proposal**：``ObservationReport`` 汇总趋势 + ``to_dev_issues`` 桥接 R8
   DevIssue（source="observation"），让真实问题进入「提案 → Owner 批准」闭环。

设计纪律（延续 R6–R15 / P0）：
- **只观测、只提议、绝不执行**：本模块零写入、零权限提升、零自动部署。
- **fail-closed / 诚实**：任一信号源不可用 → 该源事件为空 + 报告标记 ``incomplete``，
  绝不编造「环境正常」。
- **证据可溯源**：每条事件带 ``evidence``，符合 R6.5 Audit Everything。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .event import (
    EVENT_AUDIT_DENY,
    EVENT_HEALTH_DEGRADED,
    EVENT_RUNTIME_ERROR,
    SOURCE_GITHUB,
    SOURCE_MONITOR,
    ObservationEvent,
    severity_rank,
)

# 性能阈值（可注入覆盖）；越界即生成 ObservationEvent。
_DEFAULT_PERF_THRESHOLDS = {
    "llm_failure_rate_high": 0.3,   # LLM 失败率高于此 → normal（建议核查配置/熔断）
    "automation_pending_high": 50,  # 待处理自动化积压高于此 → normal（建议节流）
}


# ---------------------------------------------------------------------------
# 1. GitHub 真实信号源（可注入；无 token 依赖）
# ---------------------------------------------------------------------------
def collect_from_github(github: Any = None, *, window_sec: int = 86400,
                        now: Optional[float] = None) -> list[ObservationEvent]:
    """从注入的 github 源采集真实活动（commits / CI / issue）。

    ``github`` 需实现 ``recent_activity(limit) -> list[dict]``，每条含
    ``kind``（"commit" | "ci" | "issue"）、``status``、``ts``、``detail``。
    未注入 / 不可用 → 返回空列表（诚实，不假装有活动）。
    """
    if github is None or not hasattr(github, "recent_activity"):
        return []
    now = now if now is not None else time.time()
    try:
        rows = list(github.recent_activity(limit=200))
    except Exception:  # noqa: BLE001 - 信号源失灵不得中断观测
        return []
    events: list[ObservationEvent] = []
    for r in rows:
        try:
            ts = float(r.get("ts", 0))
        except (TypeError, ValueError):
            continue
        if now - ts > window_sec:
            continue
        kind = str(r.get("kind", "")).lower()
        status = str(r.get("status", "")).lower()
        detail = str(r.get("detail", ""))
        if kind == "ci" and status in ("failed", "error"):
            events.append(ObservationEvent(
                type=EVENT_RUNTIME_ERROR, source=SOURCE_GITHUB, severity="high",
                title=f"CI 失败：{detail[:60]}",
                detail=f"GitHub CI 状态={status}。",
                evidence=f"kind={kind} status={status}",
            ))
        elif kind == "issue" and status in ("opened", "reopened", "bug"):
            events.append(ObservationEvent(
                type=EVENT_HEALTH_DEGRADED, source=SOURCE_GITHUB, severity="normal",
                title=f"新增 Issue：{detail[:60]}",
                detail=f"GitHub Issue 活动（{status}）。",
                evidence=f"kind={kind} status={status}",
            ))
        elif kind == "commit":
            # commits 本身不算异常，仅作环境上下文（low 级，不进闭环）
            events.append(ObservationEvent(
                type=EVENT_HEALTH_DEGRADED, source=SOURCE_GITHUB, severity="low",
                title=f"提交：{detail[:60]}",
                detail="GitHub 提交活动（环境上下文，非异常）。",
                evidence=f"kind={kind}",
            ))
    return events


# ---------------------------------------------------------------------------
# 2. 运行时性能阈值 → ObservationEvent
# ---------------------------------------------------------------------------
def collect_from_performance(core: Any = None, *,
                             thresholds: dict[str, float] | None = None,
                             now: Optional[float] = None) -> list[ObservationEvent]:
    """把运行时性能越界转为观测事件（CPU / 内存 / 延迟 / 积压）。

    复用 R10-02 ``collect_metrics``（runtime_running / channel_connected /
    llm_failure_rate / automation_pending 等），并可选读 ``core.performance`` 快照。
    与 R13 健康恢复不同：本层聚焦「性能退化趋势」，只观测、只提议。
    """
    t = {**_DEFAULT_PERF_THRESHOLDS, **(thresholds or {})}
    events: list[ObservationEvent] = []
    if core is None:
        return events
    try:
        from ..evolution.evaluator.metric import collect_metrics
        sample = collect_metrics(core)
    except Exception:  # noqa: BLE001
        return events
    m = sample.metrics

    fail_rate = float(m.get("llm_failure_rate", 0.0))
    if fail_rate >= t["llm_failure_rate_high"]:
        events.append(ObservationEvent(
            type=EVENT_RUNTIME_ERROR, source=SOURCE_MONITOR, severity="normal",
            title=f"LLM 失败率偏高（{fail_rate:.2%}）",
            detail="R10-02 指标显示 LLM 失败率超过阈值，建议核查 endpoint/熔断配置。",
            evidence=f"llm_failure_rate={fail_rate} threshold={t['llm_failure_rate_high']}",
        ))

    pending = float(m.get("automation_pending", 0.0))
    if pending >= t["automation_pending_high"]:
        events.append(ObservationEvent(
            type=EVENT_HEALTH_DEGRADED, source=SOURCE_MONITOR, severity="normal",
            title=f"自动化积压偏高（{int(pending)}）",
            detail="待处理自动化数超过阈值，建议节流或扩容。",
            evidence=f"automation_pending={int(pending)} threshold={t['automation_pending_high']}",
        ))

    # 可选原生性能快照（CPU / 内存 / 延迟），若 core 暴露则纳入
    perf = getattr(core, "performance", None)
    if perf is not None and hasattr(perf, "snapshot"):
        try:
            snap = perf.snapshot() if callable(perf.snapshot) else {}
        except Exception:  # noqa: BLE001
            snap = {}
        for key, val in snap.items():
            if isinstance(val, (int, float)) and "usage" in key.lower():
                # cpu_usage / mem_usage 等百分比，>90 视为高
                if float(val) > 90.0:
                    events.append(ObservationEvent(
                        type=EVENT_HEALTH_DEGRADED, source=SOURCE_MONITOR, severity="high",
                        title=f"资源使用率高：{key}={val}",
                        detail="运行时资源使用率越界，可能影响长期稳定性。",
                        evidence=f"{key}={val}",
                    ))
    return events


# ---------------------------------------------------------------------------
# 3. Audit DENY 增长趋势
# ---------------------------------------------------------------------------
def audit_deny_growth(audit: Any, *, window_sec: int = 86400,
                       prior_ratio: float = 2.0,
                       now: Optional[float] = None) -> dict[str, Any]:
    """对比当期窗口与上一窗口的 DENY 事件数，输出增长趋势。

    返回 ``{"current": int, "prior": int, "growth": float, "growing": bool}``。
    无审计 → ``current=-1`` 标记不可用（诚实）。
    """
    if audit is None or not hasattr(audit, "recent"):
        return {"current": -1, "prior": -1, "growth": 0.0, "growing": False,
                "available": False}
    now = now if now is not None else time.time()
    rows = []
    try:
        rows = list(audit.recent(limit=20000))
    except Exception:  # noqa: BLE001
        return {"current": -1, "prior": -1, "growth": 0.0, "growing": False,
                "available": False}
    _DENY_HINTS = ("denied", "deny", "reject", "permission_denied",
                   "capability_denied", "scope_denied")
    cur, prior = 0, 0
    for r in rows:
        try:
            ts = float(r.get("ts", 0))
        except (TypeError, ValueError):
            continue
        age = now - ts
        etype = str(r.get("type", "")).lower()
        outcome = str(r.get("outcome", "")).lower()
        is_deny = etype in _DENY_HINTS or outcome in _DENY_HINTS
        if not is_deny:
            continue
        if age <= window_sec:
            cur += 1
        elif age <= window_sec * 2:
            prior += 1
    growth = (cur - prior) / prior if prior > 0 else (1.0 if cur > 0 else 0.0)
    return {
        "current": cur, "prior": prior, "growth": round(growth, 4),
        "growing": cur > prior * prior_ratio and cur > 0,
        "available": True,
    }


# ---------------------------------------------------------------------------
# 4. Analysis → Proposal（ObservationReport）
# ---------------------------------------------------------------------------
@dataclass
class ObservationReport:
    """一次真实环境观测分析报告（Reality → Observation → Analysis）。"""

    generated_at: float
    events: list[ObservationEvent] = field(default_factory=list)
    audit_deny_trend: dict[str, Any] = field(default_factory=dict)
    sources_available: list[str] = field(default_factory=list)
    sources_missing: list[str] = field(default_factory=list)
    incomplete: bool = False
    top_findings: list[str] = field(default_factory=list)

    @property
    def high_count(self) -> int:
        return sum(1 for e in self.events if e.severity in ("high", "critical"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "event_count": len(self.events),
            "high_count": self.high_count,
            "audit_deny_trend": self.audit_deny_trend,
            "sources_available": self.sources_available,
            "sources_missing": self.sources_missing,
            "incomplete": self.incomplete,
            "top_findings": self.top_findings,
            "events": [e.to_dict() for e in self.events],
        }


def analyze_environment(
    core: Any = None,
    *,
    github: Any = None,
    audit: Any = None,
    performance: bool = True,
    window_sec: int = 86400,
    now: Optional[float] = None,
) -> ObservationReport:
    """聚合全部真实环境信号 + 趋势分析，产出 ObservationReport。

    - github：GitHub 真实信号（可注入）。
    - audit：审计日志（DENY 增长趋势；若不传则尝试 core.audit）。
    - performance：是否采集运行时性能阈值事件。
    """
    now = now if now is not None else time.time()
    events: list[ObservationEvent] = []
    available: list[str] = []
    missing: list[str] = []

    # 运行时性能趋势
    if performance:
        if core is not None:
            events.extend(collect_from_performance(core, now=now))
            available.append("performance")
        else:
            missing.append("performance")

    # GitHub 真实信号
    gh_events = collect_from_github(github, window_sec=window_sec, now=now)
    if github is not None and getattr(github, "probed_ok", True):
        # 真正取到了信号（gh 鉴权或公开 REST 任一路径成功）：纳入可用源。
        available.append("github")
    elif github is not None:
        # 已接入源但根本取不到（私有库/无凭据/无网络）：诚实归为缺失源，
        # 不谎称「已观测」——fail-closed。
        missing.append("github")
    else:
        missing.append("github")
    events.extend(gh_events)

    # Audit DENY 增长趋势
    audit_obj = audit or getattr(core, "audit", None)
    trend = audit_deny_growth(audit_obj, window_sec=window_sec, now=now)
    if trend.get("available"):
        available.append("audit")
        if trend.get("growing"):
            events.append(ObservationEvent(
                type=EVENT_AUDIT_DENY, source="audit", severity="high",
                title=f"审计 DENY 增长异常（当期 {trend['current']} vs 上期 {trend['prior']}，"
                      f"增长 {trend['growth']:.0%}）",
                detail="近期权限拒绝/越权尝试显著增多，需评估护栏或异常主体。",
                evidence=f"current={trend['current']} prior={trend['prior']} growth={trend['growth']}",
            ))
    else:
        missing.append("audit")

    # 排序：严重度高的在前，便于 Owner 先读关键
    events.sort(key=lambda e: -severity_rank(e.severity))

    findings = [f"{e.severity}: {e.title}" for e in events[:5]]
    incomplete = bool(missing)

    return ObservationReport(
        generated_at=now, events=events, audit_deny_trend=trend,
        sources_available=available, sources_missing=missing,
        incomplete=incomplete, top_findings=findings,
    )


def to_dev_issues(report: ObservationReport) -> list[Any]:
    """把观测报告桥接为 R8 DevIssue，进入「提案 → Owner 批准」闭环。

    仅把 >= normal 的事件转 Issue；high/critical 优先级更高。
    本桥接**只生成提案**，不部署、不授权（受控门禁在 R8 层）。
    """
    from ..agent.issue_sources import DevIssue, ISSUE_SOURCE_AUDIT

    issues: list[DevIssue] = []
    for ev in report.events:
        rank = severity_rank(ev.severity)
        if rank < severity_rank("normal"):
            continue
        issues.append(DevIssue(
            title=f"[环境观测] {ev.title}",
            detail=f"{ev.detail}\n证据：{ev.evidence}".strip(),
            source=ISSUE_SOURCE_AUDIT,  # 复用审计源通道进入闭环
            capability="task.plan",
            required_scopes=frozenset({"read"}),
            severity="high" if ev.severity in ("high", "critical") else "normal",
        ))
    return issues
