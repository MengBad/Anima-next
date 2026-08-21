# -*- coding: utf-8 -*-
"""R16: heartbeat anomaly self-check."""
from __future__ import annotations

import time

import pytest

from anima.core.autonomy import Heartbeat
from anima.core.health_history import HealthHistory


def _sample(ts, **kw):
    base = {
        "ts": ts, "running": True, "channel_connected": False,
        "heartbeat_running": True, "watchdog_running": False,
        "watchdog_lag_sec": 9.0, "llm_requests": 0, "llm_failures": 0,
        "llm_circuit_open": False, "events_backpressure_dropped": 0,
        "outbound_failures": 0,
    }
    base.update(kw)
    return base


class _Audit:
    def __init__(self):
        self.events = []

    def emit(self, name, **kw):
        self.events.append((name, kw))


class _FakeCore:
    def __init__(self, health, audit):
        self.health_history = health
        self.audit = audit


def _heartbeat(health, audit):
    hb = Heartbeat(_FakeCore(health, audit))
    hb._anomaly_check_interval = 300.0
    hb._last_anomaly_check = 0.0
    hb._last_anomaly_signature = None
    return hb


def test_selfcheck_emits_on_severe_anomaly():
    now = time.time()
    hh = HealthHistory(_FakeCore(None, None), "data")
    hh._samples = [_sample(now - 200, running=True), _sample(now - 100, running=False), _sample(now, running=True)]
    hh._last_record_at = now
    audit = _Audit()
    hb = _heartbeat(hh, audit)
    hb._anomaly_selfcheck()
    assert len(audit.events) == 1
    name, kw = audit.events[0]
    assert name == "runtime_anomaly_detected"
    types = {t for t, _ in kw["anomalies"]}
    assert {"process_restart", "channel_disconnect", "watchdog_lag"} <= types


def test_selfcheck_no_emit_when_healthy():
    now = time.time()
    hh = HealthHistory(_FakeCore(None, None), "data")
    hh._samples = [_sample(now - 10, channel_connected=True, watchdog_running=True, watchdog_lag_sec=0.0)]
    hh._last_record_at = now
    audit = _Audit()
    hb = _heartbeat(hh, audit)
    hb._anomaly_selfcheck()
    assert audit.events == []


def test_selfcheck_dedupes_same_signature():
    now = time.time()
    hh = HealthHistory(_FakeCore(None, None), "data")
    hh._samples = [_sample(now - 10), _sample(now)]
    hh._last_record_at = now
    audit = _Audit()
    hb = _heartbeat(hh, audit)
    hb._anomaly_selfcheck()
    assert len(audit.events) == 1
    hb._anomaly_selfcheck()
    assert len(audit.events) == 1


def test_selfcheck_throttle_respected():
    now = time.time()
    hh = HealthHistory(_FakeCore(None, None), "data")
    hh._samples = [_sample(now - 5)]
    hh._last_record_at = now
    audit = _Audit()
    hb = _heartbeat(hh, audit)
    hb._last_anomaly_check = now - 1  # within 300s interval
    hb._anomaly_selfcheck()
    assert audit.events == []


def test_selfcheck_missing_health_graceful():
    audit = _Audit()
    hb = _heartbeat(None, audit)
    hb._anomaly_selfcheck()
    assert audit.events == []
