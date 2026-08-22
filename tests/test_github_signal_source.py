# -*- coding: utf-8 -*-
"""R17 —— GitHub 真实信号源单元测试（确定性，mock 掉 gh CLI）。

验证 GitHubSignalSource 在 gh 可用时把 commits/CI/issues 归一化为 ObservationEvent
所需的 recent_activity 结构；以及 gh 缺失 / CLI 失败时 fail-closed 返回空列表。
"""
from __future__ import annotations

import subprocess
from unittest import mock

import pytest

from anima.observation.github_source import (
    GitHubSignalSource,
    _gh_api,
    _iso_to_ts,
    _resolve_repo,
)


def test_iso_to_ts_parses_utc():
    assert _iso_to_ts("2024-01-01T00:00:00Z") is not None
    assert _iso_to_ts("") is None
    assert _iso_to_ts(None) is None


def test_resolve_repo_explicit_wins():
    assert _resolve_repo("owner/repo") == "owner/repo"


def test_resolve_repo_fallback_when_git_missing():
    # 无显式仓库且 git 反推失败 → 回退默认仓库（不报错）
    with mock.patch("subprocess.run", side_effect=FileNotFoundError):
        assert _resolve_repo(None) == "MengBad/Anima-next"


def test_gh_api_returns_none_on_failure():
    with mock.patch("subprocess.run", return_value=mock.Mock(
            returncode=1, stdout="", stderr="")):
        assert _gh_api("o/r", "commits") is None


def test_gh_api_parses_list():
    payload = '[{"sha":"abc"},{"sha":"def"}]'
    with mock.patch("subprocess.run", return_value=mock.Mock(
            returncode=0, stdout=payload, stderr="")):
        data = _gh_api("o/r", "commits")
        assert isinstance(data, list) and len(data) == 2


# --------------------------------------------------------------------------
# GitHubSignalSource
# --------------------------------------------------------------------------

def _fake_run(returncode, stdout):
    return mock.Mock(returncode=returncode, stdout=stdout, stderr="")


def test_source_unavailable_when_gh_missing():
    with mock.patch("subprocess.run", side_effect=FileNotFoundError):
        src = GitHubSignalSource(repo="o/r")
        assert src.available() is False
        assert src.recent_activity() == []


def test_source_recent_activity_normalizes_events():
    # gh auth status OK；三类 API 各返回一条样本
    auth = _fake_run(0, "")
    commits = '[{"sha":"deadbeefcafe","commit":{"message":"fix: 心跳丢帧","author":{"date":"2026-08-21T10:00:00Z"}}}]'
    runs = '[{"name":"CI","head_branch":"main","conclusion":"failure","created_at":"2026-08-21T11:00:00Z"}]'
    issues = '[{"title":"崩溃","state":"open","created_at":"2026-08-21T12:00:00Z"}]'

    def _runner(args, *a, **k):
        s = " ".join(str(x) for x in args)
        if "auth" in s:
            return auth
        if "commits" in s:
            return _fake_run(0, commits)
        if "actions/runs" in s:
            return _fake_run(0, runs)
        if "issues" in s:
            return _fake_run(0, issues)
        return _fake_run(1, "")

    with mock.patch("subprocess.run", side_effect=_runner):
        src = GitHubSignalSource(repo="o/r")
        assert src.available() is True
        rows = src.recent_activity()
        kinds = {r["kind"] for r in rows}
        assert kinds == {"commit", "ci", "issue"}
        ci = [r for r in rows if r["kind"] == "ci"][0]
        assert ci["status"] == "failed"
        commit = [r for r in rows if r["kind"] == "commit"][0]
        assert "deadbeef" in commit["detail"]
        assert commit["ts"] is not None


def test_source_skips_pull_requests_in_issues():
    issues = '[{"title":"PR","state":"open","created_at":"2026-08-21T12:00:00Z","pull_request":{}}]'
    def _runner(args, *a, **k):
        s = " ".join(str(x) for x in args)
        if "auth" in s:
            return _fake_run(0, "")
        return _fake_run(0, issues)

    with mock.patch("subprocess.run", side_effect=_runner):
        src = GitHubSignalSource(repo="o/r")
        rows = src.recent_activity()
        # PR 被排除，不应产生 issue 行
        assert all(r["kind"] != "issue" for r in rows)
