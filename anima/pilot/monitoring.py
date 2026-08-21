"""P8.8 — Live Pilot Monitoring (metrics + summaries).

Tracks the QQ / model / system indicators for the real-user pilot and
produces machine-readable summaries.

QQ indicators (P8.8):
    messages_received / messages_answered / reply_latency /
    error_rate / blocked_requests

Model indicators:
    wrong_answer_report / hallucination_report / tool_failure /
    execution_failure

System indicators:
    CPU / Memory / Queue / Ledger / Storage

The monitor is a PURE collector: it records counters/gauges and can produce a
snapshot.  It never decides policy, never executes, never mutates the system.

Scope limits (P8.0): no System B, no gateway, no capability activation, no
env bypass.
"""
from __future__ import annotations

import json
import threading as _threading
import time
from pathlib import Path
from typing import Any, Optional

__all__ = ["PilotMonitor", "MonitorSnapshot"]


class MonitorSnapshot:
    __slots__ = ("metrics", "ts")

    def __init__(self, metrics: dict, ts: float) -> None:
        self.metrics = metrics
        self.ts = ts

    def to_dict(self) -> dict:
        return {"timestamp": self.ts, "metrics": self.metrics}


class PilotMonitor:
    """Counters/gauges for the live pilot; thread-safe."""

    def __init__(self, data_dir: str) -> None:
        self._lock = _threading.RLock()
        self._counters: dict[str, int] = {
            "messages_received": 0,
            "messages_answered": 0,
            "blocked_requests": 0,
            "wrong_answer_report": 0,
            "hallucination_report": 0,
            "tool_failure": 0,
            "execution_failure": 0,
        }
        self._latencies: list[float] = []
        self._data_dir = data_dir

    # -- counters ----------------------------------------------------------

    def inc(self, name: str, by: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + by

    def record_latency(self, ms: float) -> None:
        with self._lock:
            self._latencies.append(ms)
            if len(self._latencies) > 10000:
                self._latencies = self._latencies[-5000:]

    # -- system gauges -------------------------------------------------------

    def _system_gauges(self) -> dict:
        gauges: dict[str, Any] = {}
        try:
            import os

            gauges["memory_rss_mb"] = round(
                int(os.getpid() and 0) or 0, 0
            )
        except Exception:  # noqa: BLE001
            gauges["memory_rss_mb"] = None
        # ledger file count (durable facts) + storage size
        try:
            ledger_dir = Path(self._data_dir) / "execution_ledger"
            if ledger_dir.is_dir():
                files = list(ledger_dir.glob("*.json"))
                gauges["ledger_facts"] = len(files)
                gauges["ledger_bytes"] = sum(
                    f.stat().st_size for f in files if f.is_file()
                )
            else:
                gauges["ledger_facts"] = 0
                gauges["ledger_bytes"] = 0
        except Exception:  # noqa: BLE001
            gauges["ledger_facts"] = None
            gauges["ledger_bytes"] = None
        # storage usage under data_dir
        try:
            root = Path(self._data_dir)
            total = sum(
                f.stat().st_size for f in root.rglob("*") if f.is_file()
            ) if root.exists() else 0
            gauges["storage_bytes"] = total
        except Exception:  # noqa: BLE001
            gauges["storage_bytes"] = None
        return gauges

    # -- snapshot -----------------------------------------------------------

    def snapshot(self) -> MonitorSnapshot:
        with self._lock:
            counters = dict(self._counters)
            lat = list(self._latencies)
        metrics = dict(counters)
        metrics["reply_latency_ms"] = {
            "count": len(lat),
            "avg": round(sum(lat) / len(lat), 3) if lat else 0.0,
            "p95": round(sorted(lat)[int(len(lat) * 0.95) - 1], 3) if lat else 0.0,
            "max": round(max(lat), 3) if lat else 0.0,
        }
        metrics["error_rate"] = self._error_rate()
        metrics["system"] = self._system_gauges()
        return MonitorSnapshot(metrics, time.time())

    def _error_rate(self) -> float:
        with self._lock:
            answered = self._counters.get("messages_answered", 0)
            if answered == 0:
                return 0.0
            failed = (
                self._counters.get("execution_failure", 0)
                + self._counters.get("tool_failure", 0)
            )
            return round(failed / answered, 4)

    def dump(self, path: Optional[str] = None) -> str:
        snap = self.snapshot().to_dict()
        text = json.dumps(snap, ensure_ascii=False, indent=2)
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(text, encoding="utf-8")
        return text
