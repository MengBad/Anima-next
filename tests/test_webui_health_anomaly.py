# -*- coding: utf-8 -*-
"""R16: /api/health/history 端点返回 anomaly 摘要。

验证 WebUI 健康历史端点把 HealthHistory.anomaly_report 派生结果透出,
使「过去24小时有没有异常」可经 HTTP 查询。
"""
from __future__ import annotations

import json
import time

import pytest
from aiohttp import web

from anima.core.health_history import HealthHistory


def _sample(ts, **kw):
    base = {
        "ts": ts, "running": True, "channel_connected": True,
        "heartbeat_running": True, "watchdog_running": True,
        "watchdog_lag_sec": 0.0, "llm_requests": 0, "llm_failures": 0,
        "llm_circuit_open": False, "events_backpressure_dropped": 0,
        "outbound_failures": 0,
    }
    base.update(kw)
    return base


class _FakeHealth:
    """A HealthHistory-like object exposing query/health/anomaly_report."""

    def __init__(self, samples):
        self._samples = samples

    def query(self, *, start=None, end=None, limit=2880):
        return [s for s in self._samples if float(s.get("ts", 0) or 0) >= (start or 0)][-limit:]

    def health(self):
        return {"samples": len(self._samples)}

    def anomaly_report(self, *, hours=24.0):
        return {"hours": hours, "samples": len(self._samples),
                "summary": "ok", "anomalies": []}


class _FakeCore:
    def __init__(self, health):
        self.health_history = health
        from types import SimpleNamespace
        self.config = SimpleNamespace(
            webui=SimpleNamespace(enabled=True, host="127.0.0.1", port=0))
        self.audit = SimpleNamespace(emit=lambda *a, **k: None)
        self._owner = None


def _server(core):
    from unittest import mock
    from anima.webui.server import WebUIServer
    with mock.patch("anima.webui.server.ConfigService"):
        return WebUIServer(core)


def _mk_request(server, query):
    from types import SimpleNamespace
    return SimpleNamespace(query=dict(query))


@pytest.mark.asyncio
async def test_health_history_endpoint_includes_anomaly():
    now = time.time()
    health = _FakeHealth([_sample(now - 3600), _sample(now - 300), _sample(now - 60)])
    server = _server(_FakeCore(health))
    req = _mk_request(server, {"range": "24h"})
    resp = await server.health_history(req)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["range"] == "24h"
    assert "anomaly" in body
    assert body["anomaly"]["summary"] == "ok"
    assert body["anomaly"]["anomalies"] == []


@pytest.mark.asyncio
async def test_health_history_anomaly_passthrough_real_history():
    # Use the real HealthHistory to confirm the endpoint surfaces its report.
    now = time.time()
    real = HealthHistory(_FakeCore(None), "data")
    real._samples = [_sample(now - 3000, channel_connected=False)]
    real._last_record_at = now - 3000
    server = _server(_FakeCore(real))
    req = _mk_request(server, {"range": "24h"})
    resp = await server.health_history(req)
    body = json.loads(resp.body)
    types = [a["type"] for a in body["anomaly"]["anomalies"]]
    assert "channel_disconnect" in types
