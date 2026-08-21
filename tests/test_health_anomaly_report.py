# -*- coding: utf-8 -*-
"""R16 Production Reliability: HealthHistory.anomaly_report.

验证从健康样本派生「过去 N 小时运行摘要 + 异常清单」：断连 / 心跳丢失 /
看门狗滞后 / LLM 熔断 / 资源异常 / 进程重启检测。纯只读分析。
"""
from __future__ import annotations

import time

import pytest

from anima.core.health_history import HealthHistory


def _mk(samples):
    class FakeCore:
        pass
    h = HealthHistory(FakeCore(), "data")
    h._samples = samples
    if samples:
        h._last_record_at = samples[-1]["ts"]
    return h


def _sample(ts, **kw):
    base = {
        "ts": ts,
        "running": True,
        "channel_connected": True,
        "heartbeat_running": True,
        "watchdog_running": True,
        "watchdog_lag_sec": 0.0,
        "llm_requests": 0,
        "llm_failures": 0,
        "llm_circuit_open": False,
        "events_backpressure_dropped": 0,
        "outbound_failures": 0,
    }
    base.update(kw)
    return base


NOW = time.time()


@pytest.mark.parametrize("kind,field,value", [
    ("channel_disconnect", "channel_connected", False),
    ("heartbeat_loss", "heartbeat_running", False),
    ("watchdog_lag", "watchdog_running", False),
    ("llm_circuit_open", "llm_circuit_open", True),
])
def test_detects_single_anomaly(kind, field, value):
    samples = [
        _sample(NOW - 3600),
        _sample(NOW - 3000),
        _sample(NOW - 2400, **{field: value}),
        _sample(NOW - 1800),
    ]
    h = _mk(samples)
    report = h.anomaly_report(hours=24)
    types = [a["type"] for a in report["anomalies"]]
    assert kind in types


def test_llm_high_failure_detected():
    samples = [
        _sample(NOW - 3000, llm_requests=100, llm_failures=20),
        _sample(NOW - 2000, llm_requests=100, llm_failures=15),
    ]
    h = _mk(samples)
    report = h.anomaly_report(hours=24)
    types = [a["type"] for a in report["anomalies"]]
    assert "llm_high_failure" in types


def test_backpressure_and_outbound_detected():
    samples = [
        _sample(NOW - 3000, events_backpressure_dropped=5, outbound_failures=3),
    ]
    h = _mk(samples)
    report = h.anomaly_report(hours=24)
    types = [a["type"] for a in report["anomalies"]]
    assert "backpressure_drop" in types
    assert "outbound_failure" in types


def test_process_restart_detected():
    # running False -> True 翻转 = 重启
    samples = [
        _sample(NOW - 3000, running=True),
        _sample(NOW - 2400, running=False),
        _sample(NOW - 1800, running=True),  # 重启
        _sample(NOW - 1200, running=False),
        _sample(NOW - 600, running=True),   # 再重启
    ]
    h = _mk(samples)
    report = h.anomaly_report(hours=24)
    restarts = [a for a in report["anomalies"] if a["type"] == "process_restart"]
    assert restarts and restarts[0]["count"] == 2


def test_healthy_window_no_anomaly():
    samples = [_sample(NOW - 1000), _sample(NOW - 500), _sample(NOW - 100)]
    h = _mk(samples)
    report = h.anomaly_report(hours=24)
    assert report["anomalies"] == []
    assert report["summary"] == "ok"


def test_empty_window_graceful():
    h = _mk([])
    report = h.anomaly_report(hours=1)
    assert report["samples"] == 0
    assert report["anomalies"] == []


def test_hours_boundary_only_recent():
    # 只有窗口外的样本 -> 无异常
    old = _sample(NOW - 48 * 3600)
    h = _mk([old])
    report = h.anomaly_report(hours=24)
    assert report["samples"] == 0
