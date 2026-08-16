"""R8 Issue 来源采集器测试。"""

import pytest

from anima.agent.issue_sources import (
    collect_owner_issues,
    collect_self_monitor_issues,
)


class _FakeSelfCode:
    def evaluate_tools(self):
        # 真实 evaluate_tools() 只返回触发建议的工具；ok_tool 不在此列。
        return [
            {"name": "flaky_tool", "reason": "high_fail_rate fail=7/8",
             "calls": 8, "fail": 7, "age_hours": 100},
        ]


def test_self_monitor_collects_selfcode_suggestions():
    """SelfCode 高失败率工具 → 生成优化 Issue。"""
    issues = collect_self_monitor_issues(_FakeSelfCode())
    titles = [i.title for i in issues]
    assert any("flaky_tool" in t for t in titles)
    # ok_tool 失败率低，不应触发
    assert not any("ok_tool" in t for t in titles)


def test_self_monitor_filters_by_severity():
    """低严重度过滤：min_severity=normal 时，selfcode 建议（severity=low）被剔除。"""
    issues = collect_self_monitor_issues(_FakeSelfCode(), min_severity="normal")
    assert all(i.severity in ("normal", "high") for i in issues)
    assert not any(i.severity == "low" for i in issues)


def test_audit_denials_aggregate_high_freq():
    """审计高频越权（>=5 次）→ high 严重度 Issue；偶发噪声被忽略。"""
    denials = [{"agent": "agent-x", "target": "agent-x"} for _ in range(7)]
    denials.append({"agent": "agent-y", "target": "agent-y"})  # 仅 1 次
    issues = collect_self_monitor_issues(audit_denials=denials)
    titles = " ".join(i.title for i in issues)
    assert "agent-x" in titles
    assert "agent-y" not in titles  # 偶发不进闭环


def test_owner_issues_collected():
    """Owner 指令 → DevIssue，capability/severity 透传。"""
    queue = [
        {"title": "支持定时提醒", "detail": "用户需要", "capability": "code.develop",
         "severity": "normal"},
        {"title": "", "detail": "空标题应被忽略"},
    ]
    issues = collect_owner_issues(queue)
    assert len(issues) == 1
    assert issues[0].title == "支持定时提醒"
    assert issues[0].source == "owner"
    assert issues[0].severity == "normal"
