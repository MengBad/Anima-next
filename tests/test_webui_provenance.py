# -*- coding: utf-8 -*-
"""R16: /api/provenance 端点公开只读部署溯源。

验证 WebUI 溯源端点回传运行版本 / 构建提交 / 部署时间 / 构建原因(提交主题),
使「现在跑的是哪个版本、从什么部署、为什么」可经 HTTP 查询(AGPL 透明)。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from aiohttp import web

from anima import __version__ as ANIMA_VERSION

_REPO_ROOT = Path(__file__).resolve().parents[1]  # tests/.. == 仓库根
_VERSION_JSON = _REPO_ROOT / "version.json"


def _fake_core():
    return SimpleNamespace(
        config=SimpleNamespace(webui=SimpleNamespace(enabled=True, host="127.0.0.1", port=0)),
        audit=SimpleNamespace(emit=lambda *a, **k: None),
        _owner=None,
    )


def _server(core):
    from anima.webui.server import WebUIServer
    with mock.patch("anima.webui.server.ConfigService"):
        return WebUIServer(core)


def _mk_request(server, query=None):
    return SimpleNamespace(query=dict(query or {}))


@pytest.mark.asyncio
async def test_provenance_endpoint_returns_expected_keys():
    # 放置已知 version.json, 清理时恢复/删除, 保证确定性且不留痕。
    prior = _VERSION_JSON.read_text(encoding="utf-8") if _VERSION_JSON.exists() else None
    fake_v = {
        "version": "9.9.9-test",
        "commit": "deadbeefcafe",
        "branch": "release/test",
        "dirty": False,
        "commit_subject": "build: 修复心跳丢帧 (R16)",
        "deployed_at": "2026-08-21T12:00:00Z",
        "source": "deploy",
        "source_archive_sha256": "sha256:abc123",
    }
    _VERSION_JSON.write_text(json.dumps(fake_v), encoding="utf-8")
    try:
        # 已固化的 commit_subject 优先, 不应再去查 git(无 .git 的生产机也是这条路径)。
        with mock.patch("subprocess.run") as sp:
            server = _server(_fake_core())
            resp = await server.provenance(_mk_request(server))
        sp.assert_not_called()
        assert resp.status == 200
        body = json.loads(resp.body)
        for key in ("version", "commit", "branch", "dirty", "commit_subject",
                    "deployed_at", "source", "source_archive_sha256",
                    "built_at", "reason_commit_subject", "explanation"):
            assert key in body, f"missing {key}"
        assert body["version"] == "9.9.9-test"
        assert body["commit"] == "deadbeefcafe"
        assert body["branch"] == "release/test"
        assert body["dirty"] is False
        assert body["commit_subject"] == "build: 修复心跳丢帧 (R16)"
        assert body["reason_commit_subject"] == "build: 修复心跳丢帧 (R16)"
        assert body["deployed_at"] == "2026-08-21T12:00:00Z"
        assert body["source"] == "deploy"
        assert body["source_archive_sha256"] == "sha256:abc123"
        assert body["built_at"] is None  # 无 git 回退时为空
        assert body["explanation"] == "deploy"
    finally:
        if prior is None:
            _VERSION_JSON.unlink(missing_ok=True)
        else:
            _VERSION_JSON.write_text(prior, encoding="utf-8")


@pytest.mark.asyncio
async def test_provenance_endpoint_falls_back_to_git_when_no_baked_subject():
    # version.json 无 commit_subject 时, 本地开发机回退到 git log 实时查询。
    prior = _VERSION_JSON.read_text(encoding="utf-8") if _VERSION_JSON.exists() else None
    fake_v = {
        "version": "9.9.9-test",
        "commit": "deadbeefcafe",
        "branch": "release/test",
        "dirty": False,
        "deployed_at": "2026-08-21T12:00:00Z",
        "source": "deploy",
    }
    _VERSION_JSON.write_text(json.dumps(fake_v), encoding="utf-8")
    try:
        fake_git = SimpleNamespace(
            returncode=0,
            stdout="build: 实时 git 主题\x1f1755776000\n",
        )
        with mock.patch("subprocess.run", return_value=fake_git):
            server = _server(_fake_core())
            resp = await server.provenance(_mk_request(server))
        body = json.loads(resp.body)
        assert body["commit_subject"] == "build: 实时 git 主题"
        assert body["reason_commit_subject"] == "build: 实时 git 主题"
        assert body["built_at"] == 1755776000.0
    finally:
        if prior is None:
            _VERSION_JSON.unlink(missing_ok=True)
        else:
            _VERSION_JSON.write_text(prior, encoding="utf-8")


@pytest.mark.asyncio
async def test_provenance_endpoint_without_version_file_falls_back():
    # version.json 不存在时, commit 回退 development, version 回退包版本, 不报错。
    prior = _VERSION_JSON.read_text(encoding="utf-8") if _VERSION_JSON.exists() else None
    _VERSION_JSON.unlink(missing_ok=True)
    try:
        # 无 version.json -> git 分支不应被执行(commit=="development" 短路)。
        with mock.patch("subprocess.run") as sp:
            server = _server(_fake_core())
            resp = await server.provenance(_mk_request(server))
        sp.assert_not_called()
        assert resp.status == 200
        body = json.loads(resp.body)
        assert body["commit"] == "development"
        assert body["version"] == ANIMA_VERSION
        assert body["commit_subject"] == ""
        assert body["reason_commit_subject"] == ""
        assert body["explanation"] == "local/development"
    finally:
        if prior is not None:
            _VERSION_JSON.write_text(prior, encoding="utf-8")
