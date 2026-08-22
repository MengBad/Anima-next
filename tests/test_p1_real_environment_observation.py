"""P1 — Real-Environment Observation 单元测试（确定性，无 LLM）。"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from anima.observation import environment as env
from anima.observation.github_source import GitHubSignalSource


class _FakeGitHub:
    def __init__(self, rows):
        self._rows = rows

    def recent_activity(self, limit=200):
        return list(self._rows)


class _FakeAudit:
    def __init__(self, rows=None):
        self._rows = rows or []

    def recent(self, limit=100):
        return list(self._rows)


class _FakeCore:
    def __init__(self, audit=None, metrics=None, performance=None):
        self.audit = audit
        self._metrics = metrics or {}
        self.performance = performance

    def __getattr__(self, name):
        # 让 collect_metrics 通过 getattr(core, "llm") 等失败而走兜底，返回空事件
        raise AttributeError(name)


# ---------------------------------------------------------------------------
# 1. GitHub 真实信号：CI 失败 → high，issue → normal，commit → low
# ---------------------------------------------------------------------------
def test_github_ci_failure_high():
    now = 1_700_000_000
    gh = _FakeGitHub([
        {"kind": "ci", "status": "failed", "ts": now - 100, "detail": "lint broken"},
        {"kind": "issue", "status": "opened", "ts": now - 200, "detail": "crash on startup"},
        {"kind": "commit", "status": "ok", "ts": now - 300, "detail": "fix typo"},
        {"kind": "ci", "status": "failed", "ts": now - 90000, "detail": "old fail"},  # 超窗口
    ])
    events = env.collect_from_github(gh, window_sec=86400, now=now)
    assert len(events) == 3
    types = {e.title[:4] for e in events}
    assert any(e.severity == "high" for e in events)
    assert any("提交" in e.title for e in events)


def test_github_missing_source_empty():
    events = env.collect_from_github(None, now=1_700_000_000)
    assert events == []


# ---------------------------------------------------------------------------
# 2. 运行时性能阈值：llm_failure_rate 越界 → normal 事件
# ---------------------------------------------------------------------------
def test_performance_threshold_failure_rate():
    now = 1_700_000_000

    class _LLM:
        def health(self):
            return {"request_count": 10, "failure_count": 5}  # 50% 失败率

    class _Core:
        llm = _LLM()

    events = env.collect_from_performance(_Core(), now=now)
    assert any("LLM 失败率" in e.title for e in events)


def test_performance_no_core_empty():
    assert env.collect_from_performance(None) == []


# ---------------------------------------------------------------------------
# 3. Audit DENY 增长趋势
# ---------------------------------------------------------------------------
def test_audit_deny_growth_detected():
    now = 1_700_000_000
    rows = [{"type": "permission_denied", "ts": now - i * 10, "outcome": "denied"} for i in range(10)]
    # 上期（上一窗口）只有 1 次
    rows += [{"type": "permission_denied", "ts": now - 90000, "outcome": "denied"}]
    trend = env.audit_deny_growth(_FakeAudit(rows), window_sec=86400, now=now)
    assert trend["available"] is True
    assert trend["current"] == 10
    assert trend["prior"] == 1
    assert trend["growing"] is True


def test_audit_deny_growth_no_audit_unavailable():
    trend = env.audit_deny_growth(None)
    assert trend["available"] is False
    assert trend["current"] == -1


# ---------------------------------------------------------------------------
# 4. analyze_environment 聚合 + 排序 + incomplete 标记
# ---------------------------------------------------------------------------
def test_analyze_environment_aggregates_and_ranks():
    now = 1_700_000_000
    gh = _FakeGitHub([{"kind": "ci", "status": "failed", "ts": now - 100, "detail": "x"}])
    rows = [{"type": "permission_denied", "ts": now - i * 10, "outcome": "denied"} for i in range(10)]
    rows += [{"type": "permission_denied", "ts": now - 90000, "outcome": "denied"}]
    core = _FakeCore(audit=_FakeAudit(rows), metrics={"llm_failure_rate": 0.5})
    report = env.analyze_environment(core, github=gh, audit=core.audit, now=now)
    # github + audit 都可用；performance 因 core 缺少 llm 等接口返回空（不报错）
    assert "github" in report.sources_available
    assert "audit" in report.sources_available
    assert report.high_count >= 2  # CI 失败 + DENY 增长
    # 严重度高在前
    assert report.events[0].severity in ("high", "critical")
    assert "growing" in report.audit_deny_trend


def test_analyze_environment_incomplete_when_audit_missing():
    report = env.analyze_environment(None, github=None, audit=None, now=1_700_000_000)
    assert report.incomplete is True
    assert "audit" in report.sources_missing


# ---------------------------------------------------------------------------
# 5. to_dev_issues 桥接 R8（只生成提案，不部署）
# ---------------------------------------------------------------------------
def test_to_dev_issues_bridges_high_severity():
    now = 1_700_000_000
    gh = _FakeGitHub([{"kind": "ci", "status": "failed", "ts": now - 100, "detail": "x"}])
    report = env.analyze_environment(_FakeCore(), github=gh, audit=_FakeAudit([]), now=now)
    issues = env.to_dev_issues(report)
    # CI 失败（high）→ 至少 1 条 Issue；low 级 commit 不进闭环
    assert len(issues) >= 1
    assert all(i.severity in ("high", "normal") for i in issues)


def test_to_dev_issues_skips_low():
    now = 1_700_000_000
    # 仅 commit（low）→ 不应产生 Issue
    gh = _FakeGitHub([{"kind": "commit", "status": "ok", "ts": now - 100, "detail": "x"}])
    report = env.analyze_environment(_FakeCore(), github=gh, audit=_FakeAudit([]), now=now)
    assert env.to_dev_issues(report) == []


# ---------------------------------------------------------------------------
# 6. R17 诚实契约：真实源 probe 失败（私有库/无凭据/无网络）必须归 sources_missing
# ---------------------------------------------------------------------------
def test_github_signal_source_probed_ok_false_on_failure():
    # 私有/不可达仓库：gh 缺失且 REST 404 → recent_activity 返回 [] 且 probed_ok=False
    src = GitHubSignalSource.__new__(GitHubSignalSource)
    src.repo = "private/does-not-exist"
    src.probed_ok = False
    # 直接验证 analyze_environment 把 probed_ok=False 的源归入 missing（不谎称可用）
    report = env.analyze_environment(
        _FakeCore(), github=src, audit=_FakeAudit([]), now=1_700_000_000)
    assert "github" in report.sources_missing
    assert "github" not in report.sources_available
    assert report.incomplete is True


def test_github_signal_source_probed_ok_true_when_data():
    # 注入一个探针成功（probed_ok=True）的源 → 应纳入 available，不再 missing
    src = _FakeGitHub([])
    src.probed_ok = True
    report = env.analyze_environment(
        _FakeCore(), github=src, audit=_FakeAudit([]), now=1_700_000_000)
    assert "github" in report.sources_available
    assert "github" not in report.sources_missing
    assert report.incomplete is False
