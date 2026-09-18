# -*- coding: utf-8 -*-
"""R16 闭环4：配置/权限漂移检测。

验证 ConfigDriftMonitor 基线比对 + 严重度分级 + 审计去重 + 重新校准，
以及 WebUI /api/config/drift 只读展示与 /repin 受控重校准。
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest import mock

import pytest

from anima.core.config_drift import ConfigDriftMonitor
from anima.core.config import AnimaConfig, PermissionConfig, SecurityConfig, WebUIConfig


@pytest.fixture(autouse=True)
def _isolate_drift_baseline(tmp_path, monkeypatch):
    """把漂移基线隔离到 tmp_path。

    自 2026-09-18（债 D-08）起基线会**落盘**到 `persistence.data_dir`。默认配置
    指向仓库的 `data/` ⇒ 不隔离会有两个后果：测试相互污染（前一个测试落的基线被
    后一个读到，产生假漂移）且往仓库写文件。
    """
    monkeypatch.setattr(
        "anima.core.config_drift._default_baseline_path",
        lambda cfg: tmp_path / "config_drift_baseline.json",
    )


def _cfg(**overrides):
    """用默认值构造一份配置，并按 overrides 点路径修改（键用 '.' 分隔层级）。"""
    cfg = AnimaConfig(
        permissions=PermissionConfig(owners=[10001], admins=[10002]),
        security=SecurityConfig(require_approval_for_danger=True),
        webui=WebUIConfig(enabled=False, host="127.0.0.1", port=8088, token="t"),
    )
    for dotted, val in overrides.items():
        node = cfg
        parts = dotted.split(".")
        for p in parts[:-1]:
            node = getattr(node, p)
        setattr(node, parts[-1], val)
    return cfg


def _fake_core(cfg):
    return SimpleNamespace(
        config=cfg,
        audit=SimpleNamespace(emit=lambda *a, **k: None),
        permission_store=SimpleNamespace(admins=lambda: list(cfg.permissions.admins)),
    )


def _server(core):
    from anima.webui.server import WebUIServer
    with mock.patch("anima.webui.server.ConfigService"):
        return WebUIServer(core)


def _mk_request(query=None):
    return SimpleNamespace(query=dict(query or {}))


# --------------------------------------------------------------------------
# 监控器单元
# --------------------------------------------------------------------------

def test_no_drift_on_identical_config():
    cfg = _cfg()
    m = ConfigDriftMonitor(cfg, actor="bootstrap")
    report = m.check(cfg)
    assert report["drift_detected"] is False
    assert report["watched_fields"] == len(m._baseline)
    assert report["dangerous"] == []


def test_owner_added_flags_warning_drift():
    cfg = _cfg()
    m = ConfigDriftMonitor(cfg, actor="bootstrap")
    changed = _cfg(**{"permissions.owners": [10001, 99999]})  # 新增一位 owner
    report = m.check(changed)
    assert report["drift_detected"] is True
    owners = [d for d in report["items"] if d["field"] == "owners"][0]
    assert owners["severity"] == "warning"
    assert sorted(owners["after"]) == ["10001", "99999"]


def test_guard_relaxed_flags_danger():
    cfg = _cfg(**{"security.require_approval_for_danger": True})
    m = ConfigDriftMonitor(cfg, actor="bootstrap")
    relaxed = _cfg(**{"security.require_approval_for_danger": False})
    report = m.check(relaxed)
    item = [d for d in report["items"] if d["field"] == "require_approval_for_danger"][0]
    assert item["severity"] == "danger"
    assert report["dangerous"]


def test_webui_exposure_outside_loopback_is_danger():
    cfg = _cfg(**{"webui.enabled": True, "webui.host": "127.0.0.1"})
    m = ConfigDriftMonitor(cfg, actor="bootstrap")
    exposed = _cfg(**{"webui.enabled": True, "webui.host": "0.0.0.0"})
    item = [d for d in m.check(exposed)["items"] if d["field"] == "webui_host"][0]
    assert item["severity"] == "danger"


def test_owners_cleared_is_danger():
    cfg = _cfg(**{"permissions.owners": [10001]})
    m = ConfigDriftMonitor(cfg, actor="bootstrap")
    cleared = _cfg(**{"permissions.owners": []})
    item = [d for d in m.check(cleared)["items"] if d["field"] == "owners"][0]
    assert item["severity"] == "danger"


def test_maybe_emit_writes_audit_once_then_dedupes():
    cfg = _cfg()
    m = ConfigDriftMonitor(cfg, actor="bootstrap")
    drifts = []
    fake_audit = SimpleNamespace(emit=lambda *a, **k: drifts.append(k))
    # 第一次漂移：写 audit
    first = m.maybe_emit(_cfg(**{"security.require_approval_for_danger": False}), fake_audit)
    assert first is True
    assert len(drifts) == 1
    # 同一漂移重复调用：去重，不再写
    second = m.maybe_emit(_cfg(**{"security.require_approval_for_danger": False}), fake_audit)
    assert second is False
    assert len(drifts) == 1
    # 漂移消失后再次出现不同漂移：再次写
    third = m.maybe_emit(_cfg(**{"permissions.owners": [10001, 88888]}), fake_audit)
    assert third is True
    assert len(drifts) == 2


def test_repin_resets_baseline_and_clears_drift():
    cfg = _cfg()
    m = ConfigDriftMonitor(cfg, actor="bootstrap")
    changed = _cfg(**{"permissions.owners": [10001, 88888]})
    assert m.check(changed)["drift_detected"] is True
    m.repin(changed, actor="owner:10001")
    assert m.check(changed)["drift_detected"] is False
    assert m._baseline_actor == "owner:10001"
    # 更进一步的偏离现在被视为新漂移
    assert m.check(_cfg(**{"permissions.owners": [10001, 88888, 77777]}))["drift_detected"] is True


# --------------------------------------------------------------------------
# WebUI 处理器
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_config_drift_handler_returns_report():
    cfg = _cfg(**{"permissions.owners": [10001]})
    core = _fake_core(cfg)
    from anima.core.config_drift import ConfigDriftMonitor
    core.config_drift = ConfigDriftMonitor(cfg, actor="bootstrap")
    server = _server(core)
    resp = await server.config_drift(_mk_request())
    body = json.loads(resp.body)
    assert body["drift_detected"] is False
    assert body["watched_fields"] > 0
    assert resp.status == 200


@pytest.mark.asyncio
async def test_config_drift_repin_handler_rebaselines():
    cfg = _cfg(**{"permissions.owners": [10001]})
    core = _fake_core(cfg)
    from anima.core.config_drift import ConfigDriftMonitor
    m = ConfigDriftMonitor(cfg, actor="bootstrap")
    core.config_drift = m
    # 先制造漂移
    core.config.permissions.owners = [10001, 55555]
    server = _server(core)
    before = await server.config_drift(_mk_request())
    assert json.loads(before.body)["drift_detected"] is True
    # owner 受控重校准
    rep = await server.config_drift_repin(_mk_request())
    assert json.loads(rep.body)["drift_detected"] is False
    assert m._baseline_actor == "owner:10001"
