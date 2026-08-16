"""R10-01 Reality Observation Layer 测试。

验证统一观测事件模型与各采集源：审计 DENY 聚合、运行时健康、用户反馈、聚合器桥接 R8 DevIssue。
全部纯函数/mock，不依赖 LLM/网络/生产部署。
"""

import time

import pytest

from anima.observation import (
    EVENT_AUDIT_DENY,
    EVENT_HEALTH_DEGRADED,
    EVENT_USER_FEEDBACK,
    SOURCE_AUDIT,
    SOURCE_FEEDBACK,
    SOURCE_MONITOR,
    ObservationEvent,
    collect_all,
    collect_from_audit,
    collect_from_feedback,
    collect_from_runtime,
    to_dev_issues,
)


class _FakeAudit:
    def __init__(self, rows):
        self._rows = rows

    def recent(self, limit=1000):
        return self._rows


def _deny_events(target, n, span=100):
    now = time.time()
    return [
        {"ts": now - span + i, "type": "permission_denied", "agent": target, "tool": "x"}
        for i in range(n)
    ]


def test_observation_event_to_dict_roundtrip():
    ev = ObservationEvent(type=EVENT_AUDIT_DENY, source=SOURCE_AUDIT, severity="high",
                          title="t", detail="d", evidence="e", target="agent-x")
    d = ev.to_dict()
    assert d["type"] == EVENT_AUDIT_DENY and d["severity"] == "high"
    assert d["target"] == "agent-x" and "id" in d and "ts" in d


def test_audit_analyzer_aggregates_high_frequency_denials():
    rows = _deny_events("agent-bad", 6)  # >=5 触发
    events = collect_from_audit(_FakeAudit(rows), window_sec=10_000_000, min_denials=5)
    deny = [e for e in events if e.type == EVENT_AUDIT_DENY]
    assert len(deny) == 1
    assert deny[0].severity == "high"
    assert "agent-bad" in deny[0].title
    assert deny[0].target == "agent-bad"


def test_audit_analyzer_ignores_low_frequency_noise():
    rows = _deny_events("agent-rare", 3)  # <5 不触发
    events = collect_from_audit(_FakeAudit(rows), window_sec=10_000_000, min_denials=5)
    assert [e for e in events if e.type == EVENT_AUDIT_DENY] == []


def test_audit_analyzer_window_filter():
    # 旧事件（超出窗口）不应被聚合
    rows = [{"ts": 1, "type": "permission_denied", "agent": "old", "tool": "x"}]
    events = collect_from_audit(_FakeAudit(rows), window_sec=100, min_denials=1)
    assert [e for e in events if e.type == EVENT_AUDIT_DENY] == []


def test_runtime_monitor_detects_down():
    class _Core:
        class _HH:
            def health(self):
                return {"running": False, "channel_connected": False}
        health_history = _HH()
    events = collect_from_runtime(_Core())
    degraded = [e for e in events if e.type == EVENT_HEALTH_DEGRADED]
    assert len(degraded) == 2
    assert all(e.severity == "high" for e in degraded)


def test_runtime_monitor_healthy_no_events():
    class _Core:
        class _HH:
            def health(self):
                return {"running": True, "channel_connected": True}
        health_history = _HH()
    assert collect_from_runtime(_Core) == []


def test_user_feedback_normalizes():
    q = [
        {"title": "希望支持夜间静默", "detail": "晚上太吵"},
        {"title": "回复总是报错bug", "detail": "崩溃"},
    ]
    events = collect_from_feedback(q)
    assert len(events) == 2
    assert events[0].severity == "normal"
    assert events[1].severity == "high"  # 命中 _HIGH_KEYWORDS


def test_collect_all_aggregates_and_bridges_to_dev_issues():
    rows = _deny_events("agent-loop", 5)
    core = None  # runtime_monitor 无 core 时不报错
    events = collect_all(
        core,
        audit=_FakeAudit(rows),
        feedback_queue=[{"title": "加个天气工具"}],
        window_sec=10_000_000, min_denials=5,
    )
    types = {e.type for e in events}
    assert EVENT_AUDIT_DENY in types
    assert EVENT_USER_FEEDBACK in types
    issues = to_dev_issues(events)
    # 至少高频 DENY（high）应转为 DevIssue
    assert any("观测" in i.title for i in issues)
    assert any(i.severity == "high" for i in issues)


def test_collect_all_handles_none_inputs():
    # 不传任何源也不应抛错
    assert collect_all(None) == []
