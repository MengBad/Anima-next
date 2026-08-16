"""R8 闭环的「发现问题」来源采集器。

把**自监控信号**与 **Owner 指令**转成标准 ``DevIssue``，喂给 ``DevLoopOrchestrator``
形成完整自主开发闭环（Issue 采集 → 规划 → 开发 → 测试 → Review → 受控部署）。

设计纪律：
- 本模块**只读采集**，绝不触发任何修改/部署；产出的 DevIssue 需经 Owner 授权
  才进入 DevLoopOrchestrator（受控门禁在 R8 层）。
- 严重度过滤：低严重度默认不进闭环，避免把偶发噪声当开发任务。
"""

from __future__ import annotations

from typing import Any

from .devloop import (
    ISSUE_SOURCE_AUDIT,
    ISSUE_SOURCE_OWNER,
    ISSUE_SOURCE_SELF_MONITOR,
    DevIssue,
)


def _issue_from_selfcode_suggestions(suggestions: list[dict[str, Any]]) -> list[DevIssue]:
    """SelfCode 效果评估建议（高失败率/长期闲置工具）→ 优化类 Issue。"""
    out: list[DevIssue] = []
    for s in suggestions:
        name = str(s.get("name", ""))
        reason = str(s.get("reason", ""))
        if not name:
            continue
        out.append(DevIssue(
            title=f"优化/下线低效自生成工具：{name}",
            detail=f"SelfCode 效果评估建议：{reason}（calls={s.get('calls')}, "
                   f"fail={s.get('fail')}, age_h={s.get('age_hours')}）",
            source=ISSUE_SOURCE_SELF_MONITOR,
            capability="code.develop",
            required_scopes=frozenset({"write"}),
            severity="low",
        ))
    return out


def _issue_from_audit_denials(denial_events: list[dict[str, Any]]) -> list[DevIssue]:
    """审计链中的高频 DENY / 越权尝试 → 权限模型或护栏改进 Issue。"""
    out: list[DevIssue] = []
    # 按 agent/role 聚合越权尝试次数
    by_target: dict[str, int] = {}
    for ev in denial_events:
        tgt = str(ev.get("agent") or ev.get("target") or "unknown")
        by_target[tgt] = by_target.get(tgt, 0) + 1
    for tgt, cnt in by_target.items():
        if cnt < 5:
            continue  # 偶发噪声不进闭环
        out.append(DevIssue(
            title=f"审查高频越权/拒绝：{tgt}（{cnt} 次）",
            detail="审计证据链显示该目标被反复拒绝或越权尝试，需评估护栏/权限模型缺口。",
            source=ISSUE_SOURCE_AUDIT,
            capability="task.plan",
            required_scopes=frozenset({"read"}),
            severity="high",
        ))
    return out


def collect_self_monitor_issues(
    selfcode_manager: Any = None,
    audit_denials: list[dict[str, Any]] | None = None,
    *,
    min_severity: str = "low",
) -> list[DevIssue]:
    """采集自监控信号 → DevIssue 列表。

    - selfcode_manager：可选，调用其 ``evaluate_tools()`` 获取低效工具建议。
    - audit_denials：可选，R6.5 审计证据中筛选出的 DENY 事件列表。
    """
    issues: list[DevIssue] = []
    if selfcode_manager is not None and hasattr(selfcode_manager, "evaluate_tools"):
        try:
            suggestions = selfcode_manager.evaluate_tools()
            issues.extend(_issue_from_selfcode_suggestions(suggestions or []))
        except Exception:
            pass
    if audit_denials:
        issues.extend(_issue_from_audit_denials(audit_denials))
    return _filter_severity(issues, min_severity)


def collect_owner_issues(owner_queue: list[dict[str, Any]] | None = None) -> list[DevIssue]:
    """采集 Owner 显式提交的开发指令 → DevIssue。

    owner_queue：可注入的指令列表（如 WebUI/运维侧写入的待开发需求）。
    每条含 ``title`` / ``detail`` / 可选 ``capability`` / ``severity``。
    """
    out: list[DevIssue] = []
    for item in (owner_queue or []):
        title = str(item.get("title", "")).strip()
        if not title:
            continue
        out.append(DevIssue(
            title=title,
            detail=str(item.get("detail", "")),
            source=ISSUE_SOURCE_OWNER,
            capability=str(item.get("capability", "code.develop")),
            required_scopes=frozenset(item.get("required_scopes", ["write"])),
            severity=str(item.get("severity", "normal")),
        ))
    return out


_SEVERITY_ORDER = {"low": 0, "normal": 1, "high": 2}


def _filter_severity(issues: list[DevIssue], min_severity: str) -> list[DevIssue]:
    """按最低严重度过滤（默认保留 low 及以上）。"""
    threshold = _SEVERITY_ORDER.get(min_severity, 0)
    return [i for i in issues if _SEVERITY_ORDER.get(i.severity, 1) >= threshold]
