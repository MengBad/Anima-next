# -*- coding: utf-8 -*-
"""R17 —— GitHub 真实信号源单元测试（确定性，mock 掉 gh CLI 与 REST）。

验证 GitHubSignalSource：
- gh 可用时归一化 commits/CI/issues；
- gh 缺失时回退公开 REST（commits/issues 取得到，CI 诚实留空）；
- 任一源失败 fail-closed 返回空列表；
- 不接触任何明文凭据。
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
    with mock.patch("subprocess.run", side_effect=FileNotFoundError):
        assert _resolve_repo(None) == "MengBad/Anima-next"


def test_resolve_repo_does_not_truncate_trailing_t():
    # 回归：仓库名以 "t" 结尾（如 Anima-next）不得被 .rstrip("/.git") 误删。
    def _fake_run(args, **kwargs):
        if args[:2] == ["git", "rev-parse"]:
            return mock.Mock(returncode=0, stdout="/repo", stderr="")
        if args[:3] == ["git", "-C", "/repo", "config"]:
            return mock.Mock(
                returncode=0,
                stdout="https://github.com/MengBad/Anima-next.git",
                stderr="",
            )
        return mock.Mock(returncode=1, stdout="", stderr="")

    with mock.patch("subprocess.run", side_effect=_fake_run):
        assert _resolve_repo(None) == "MengBad/Anima-next"


def test_resolve_repo_does_not_truncate_trailing_t_direct_ssh():
    # SSH 形态 + 以 t 结尾：git@github.com:MengBad/Anima-next.git
    def _fake_run(args, **kwargs):
        if args[:2] == ["git", "rev-parse"]:
            return mock.Mock(returncode=0, stdout="/repo", stderr="")
        if args[:3] == ["git", "-C", "/repo", "config"]:
            return mock.Mock(
                returncode=0,
                stdout="git@github.com:MengBad/Anima-next.git",
                stderr="",
            )
        return mock.Mock(returncode=1, stdout="", stderr="")

    with mock.patch("subprocess.run", side_effect=_fake_run):
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


def test_source_unavailable_when_gh_missing_rest_ok():
    # gh 缺失 -> available()=False，但 REST 回退仍可取公开 commits/issues
    gh_missing = FileNotFoundError()
    commits = '[{"sha":"deadbeefcafe","commit":{"message":"fix: 心跳丢帧","author":{"date":"2026-08-21T10:00:00Z"}}}]'
    issues = '[{"title":"崩溃","state":"open","created_at":"2026-08-21T12:00:00Z"}]'

    captured = {}

    def _runner(args, *a, **k):
        s = " ".join(str(x) for x in args)
        captured.setdefault("calls", []).append(s)
        if "auth" in s:
            raise gh_missing
        return _fake_run(1, "")  # gh api 一律失败

    class _Resp:
        def read(self):
            url = captured.get("last_url", "")
            if "commits" in url:
                return commits.encode()
            if "issues" in url:
                return issues.encode()
            return b"[]"

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

    def _urlopen(req, timeout=20):
        captured["last_url"] = getattr(req, "full_url", "")
        return _Resp()

    with mock.patch.dict("os.environ", {"ANIMA_GITHUB_TOKEN": ""}),          mock.patch("subprocess.run", side_effect=_runner),          mock.patch("urllib.request.urlopen", side_effect=_urlopen):
        src = GitHubSignalSource(repo="o/r")
        assert src.available() is False  # gh 缺失且无 token
        rows = src.recent_activity()
        kinds = {r["kind"] for r in rows}
        # gh 缺失 -> 无 CI（需鉴权），但 commits + issues 经 REST 取回
        assert kinds == {"commit", "issue"}
        assert all(r["ts"] is not None for r in rows)


def test_source_gh_preferred_over_rest():
    commits = '[{"sha":"deadbeefcafe","commit":{"message":"fix","author":{"date":"2026-08-21T10:00:00Z"}}}]'
    runs = '[{"name":"CI","head_branch":"main","conclusion":"failure","created_at":"2026-08-21T11:00:00Z"}]'
    issues = '[{"title":"崩溃","state":"open","created_at":"2026-08-21T12:00:00Z"}]'

    def _runner(args, *a, **k):
        s = " ".join(str(x) for x in args)
        if "auth" in s:
            return _fake_run(0, "")
        if "commits" in s:
            return _fake_run(0, commits)
        if "actions/runs" in s:
            return _fake_run(0, runs)
        if "issues" in s:
            return _fake_run(0, issues)
        return _fake_run(1, "")

    with mock.patch("subprocess.run", side_effect=_runner),          mock.patch("urllib.request.urlopen", side_effect=AssertionError("REST 不应被调用")):
        src = GitHubSignalSource(repo="o/r")
        assert src.available() is True
        rows = src.recent_activity()
        kinds = {r["kind"] for r in rows}
        assert kinds == {"commit", "ci", "issue"}
        ci = [r for r in rows if r["kind"] == "ci"][0]
        assert ci["status"] == "failed"


def test_source_skips_pull_requests_in_issues():
    issues = '[{"title":"PR","state":"open","created_at":"2026-08-21T12:00:00Z","pull_request":{}}]'
    with mock.patch("subprocess.run", side_effect=lambda args, *a, **k: (
            _fake_run(0, "") if "auth" in " ".join(map(str, args)) else _fake_run(0, issues))):
        src = GitHubSignalSource(repo="o/r")
        rows = src.recent_activity()
        assert all(r["kind"] != "issue" for r in rows)


def test_source_rest_failure_fail_closed():
    # gh 缺失且 REST 也失败 -> 空列表，不抛异常
    def _runner(args, *a, **k):
        if "auth" in " ".join(map(str, args)):
            raise FileNotFoundError()
        return _fake_run(1, "")

    class _Bad:
        def __enter__(self):
            raise OSError("network down")
        def __exit__(self, *e):
            return False

    with mock.patch("subprocess.run", side_effect=_runner),          mock.patch("urllib.request.urlopen", side_effect=_Bad()):
        src = GitHubSignalSource(repo="o/r")
        assert src.recent_activity() == []


def test_token_unlocks_available_without_gh():
    # 配置了 ANIMA_GITHUB_TOKEN -> available()=True，即便 gh 缺失
    runs = '[{"name":"CI","head_branch":"main","conclusion":"failure","created_at":"2026-08-21T11:00:00Z"}]'
    commits = '[{"sha":"deadbeefcafe","commit":{"message":"fix","author":{"date":"2026-08-21T10:00:00Z"}}}]'

    def _runner(args, *a, **k):
        raise FileNotFoundError()  # gh 永远缺失

    captured = {}

    class _Resp:
        def read(self):
            url = captured.get("last_url", "")
            if "actions/runs" in url:
                return runs.encode()
            if "commits" in url:
                return commits.encode()
            return b"[]"

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

    def _urlopen(req, timeout=20):
        captured["last_url"] = getattr(req, "full_url", "")
        captured["headers"] = dict(getattr(req, "headers", {}))
        return _Resp()

    with mock.patch.dict("os.environ", {"ANIMA_GITHUB_TOKEN": "ghp_test_token"}),          mock.patch("subprocess.run", side_effect=_runner),          mock.patch("urllib.request.urlopen", side_effect=_urlopen):
        src = GitHubSignalSource(repo="o/r")
        assert src.available() is True  # token 注入即视为可用
        assert src.token == "ghp_test_token"
        rows = src.recent_activity()
        kinds = {r["kind"] for r in rows}
        # 无 gh：commits（REST）+ ci（带 token 的 REST 回退）均可取，不取 issues 因本夹具未给
        assert "commit" in kinds
        assert "ci" in kinds
        # 带鉴权请求头应发送 Bearer
        assert captured["headers"].get("Authorization") == "Bearer ghp_test_token"

