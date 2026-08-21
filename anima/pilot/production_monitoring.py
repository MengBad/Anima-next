"""P8.11 — Production Monitoring (metrics + structured logs + retention + alerts).

Extends the P8.8 monitor with production-grade concerns:

    * metrics            - QQ / model / system indicators (P8.8 base)
    * structured logs    - the monitor ALSO aggregates the conversation
                           evidence JSONL into analyzable summaries
    * retention          - evidence/log retention: old records are pruned by
                           an explicit retention policy (age / max count)
    * alert hooks        - threshold-based alert hooks: when error_rate or
                           a counter crosses a threshold, registered hooks are
                           called (e.g. a webhook / log / owner notification)

Alert hooks NEVER execute anything on the system: they are notification-only.
The monitor remains a pure collector + notifier.

Scope limits (P8.11): no System B, no gateway mutation, no capability
activation, no env bypass.
"""
from __future__ import annotations

import json
import threading as _threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from anima.pilot.monitoring import PilotMonitor

__all__ = [
    "ProductionMonitor",
    "RetentionPolicy",
    "AlertRule",
    "AlertHook",
    "STANDARD_METRICS",
    "retention_72h",
    "retention_7d",
    "retention_30d",
]

#: hook signature: (rule_name, value, threshold, snapshot) -> None
AlertHook = Callable[[str, float, float, dict], None]

#: P8.11 standard metrics the monitor tracks (all counters start at 0).
STANDARD_METRICS = (
    "messages_received",
    "messages_answered",
    "reject_count",          # entry-gate / policy rejections
    "safety_block",          # safety-filter interceptions
    "execution_count",       # F5 executions admitted
    "error_rate",
    "blocked_requests",
    "wrong_answer_report",
    "hallucination_report",
    "tool_failure",
    "execution_failure",
)

#: P8.11 retention presets.
RETENTION_72H = 72 * 3600.0
RETENTION_7D = 7 * 24 * 3600.0
RETENTION_30D = 30 * 24 * 3600.0


class RetentionPolicy:
    """Explicit retention for evidence/log files (P8.4: records deletable)."""

    def __init__(self, *, max_age_sec: float = 0.0, max_count: int = 0) -> None:
        self.max_age_sec = max_age_sec   # <=0 = no age pruning
        self.max_count = max_count       # <=0 = no count pruning

    def active(self) -> bool:
        return self.max_age_sec > 0 or self.max_count > 0


class AlertRule:
    """One threshold rule: metric value >= threshold triggers hooks."""

    def __init__(self, metric: str, threshold: float, name: str = "") -> None:
        self.metric = metric
        self.threshold = threshold
        self.name = name or f"{metric}>={threshold}"

    def evaluate(self, metrics: dict) -> Optional[float]:
        """Return the metric value if it crosses the threshold, else None."""
        if self.metric not in metrics:
            return None
        value = metrics[self.metric]
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        return value if value >= self.threshold else None


def alert_rules_from_dict(rows: Optional[list]) -> list[AlertRule]:
    """P9.3: build AlertRule list from a config dict / JSON array.

    Each row: {"metric": str, "threshold": float, "name": str?}.  Malformed
    rows are skipped (never crash the monitor on bad config); None/[] -> [].
    """
    if not rows:
        return []
    rules = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        metric = row.get("metric")
        if not metric:
            continue
        try:
            threshold = float(row.get("threshold", 0))
        except (TypeError, ValueError):
            continue
        rules.append(AlertRule(metric, threshold, str(row.get("name") or "")))
    return rules


class ProductionMonitor:
    """Production monitor: metrics + structured aggregation + retention + alerts."""

    def __init__(
        self,
        data_dir: str,
        *,
        retention: Optional[RetentionPolicy] = None,
        alert_rules: Optional[list[AlertRule]] = None,
    ) -> None:
        self._base = PilotMonitor(data_dir)
        self._retention = retention or RetentionPolicy()
        self._rules = alert_rules or []
        self._hooks: list[AlertHook] = []
        self._lock = _threading.RLock()
        self._data_dir = data_dir
        self._alerts_fired: dict[str, float] = {}
        # P9.3: QQ (NapCat) connection state — default unknown = disconnected
        # (fail closed: monitoring must never assume the gateway is up).
        self._qq_connected = False
        self._qq_connected_since = 0.0
        # P8.11: pre-seed the standard metric counters (incl. the three that
        # the base monitor does not track) so snapshots always carry them.
        for name in STANDARD_METRICS:
            if name not in self._base._counters:  # noqa: SLF001 - internal seed
                self._base._counters[name] = 0  # noqa: SLF001
        for name in ("kill_switch_trigger", "provider_failure",
                     "qq_connected", "qq_disconnects", "permission_deny",
                     "qq_connection_uptime_sec"):
            if name not in self._base._counters:  # noqa: SLF001
                self._base._counters[name] = 0  # noqa: SLF001

    # -- counters / latency (delegate to the base monitor) ------------------

    def inc(self, name: str, by: int = 1) -> None:
        self._base.inc(name, by)

    def record_latency(self, ms: float) -> None:
        self._base.record_latency(ms)

    # -- P8.11 event hooks: kill switch trigger / provider failure -----------

    def on_kill_switch_trigger(self, group_id: str = "") -> None:
        """Record a kill-switch trigger event and fire a dedicated hook."""
        self.inc("kill_switch_trigger", 1)
        self._fire_named("kill_switch_trigger", 1.0,
                         {"group_id": group_id})

    def on_provider_failure(self, *, capability: str, error: str = "") -> None:
        """Record a provider failure and fire a dedicated hook."""
        self.inc("provider_failure", 1)
        self._fire_named("provider_failure", 1.0,
                         {"capability": capability, "error": error[:200]})

    # -- P9.3 QQ connection state -------------------------------------------

    def set_qq_connected(self, connected: bool, *, ws_uptime_sec: float = 0.0) -> None:
        """Record the QQ (NapCat) gateway connection state.

        A transition connected->disconnected increments ``qq_disconnects``;
        reconnects update ``qq_connected_since``.
        """
        with self._lock:
            was = self._qq_connected
            self._qq_connected = bool(connected)
            if connected:
                self._qq_connected_since = (
                    time.time() if ws_uptime_sec <= 0 else ws_uptime_sec
                )
                self._base._counters["qq_connected"] = 1  # noqa: SLF001
                self._base._counters["qq_connection_uptime_sec"] = (  # noqa: SLF001
                    self._qq_connected_since
                )
            else:
                self._base._counters["qq_connected"] = 0  # noqa: SLF001
                if was:
                    self.inc("qq_disconnects", 1)

    def qq_connected(self) -> bool:
        """Whether the QQ gateway is currently connected (default False)."""
        return self._qq_connected

    def _fire_named(self, name: str, value: float, payload: dict) -> None:
        """Fire all registered hooks for a named event (notification-only)."""
        with self._lock:
            hooks = list(self._hooks)
        event_snapshot = {"event": name, **payload,
                          "metrics": self._base.snapshot().to_dict()["metrics"]}
        for hook in hooks:
            try:
                hook(name, value, 0.0, event_snapshot)
            except Exception:  # noqa: BLE001 - hooks are notification-only
                import logging

                logging.getLogger("anima.pilot.monitoring").exception(
                    "alert hook failed (event=%s)", name
                )


    # -- alert hooks ---------------------------------------------------------

    def register_hook(self, hook: AlertHook) -> None:
        with self._lock:
            self._hooks.append(hook)

    def _fire(self, rule: AlertRule, value: float, snapshot: dict) -> None:
        with self._lock:
            # fire once per rule per threshold crossing (until reset)
            if self._alerts_fired.get(rule.name) == value:
                return
            self._alerts_fired[rule.name] = value
            hooks = list(self._hooks)
        for hook in hooks:
            try:
                hook(rule.name, value, rule.threshold, snapshot)
            except Exception:  # noqa: BLE001 - hooks are notification-only
                import logging

                logging.getLogger("anima.pilot.monitoring").exception(
                    "alert hook failed (rule=%s)", rule.name
                )

    def reset_alerts(self) -> None:
        with self._lock:
            self._alerts_fired.clear()

    # -- retention -----------------------------------------------------------

    def apply_retention(self) -> int:
        """Prune evidence/log files per the retention policy; return removed."""
        if not self._retention.active():
            return 0
        ev_path = Path(self._data_dir) / "logs" / "conversation_evidence.jsonl"
        if not ev_path.exists():
            return 0
        lines = ev_path.read_text(encoding="utf-8").splitlines()
        if self._retention.max_count > 0 and len(lines) > self._retention.max_count:
            keep = lines[-self._retention.max_count:]
            ev_path.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
            return len(lines) - len(keep)
        return 0

    # -- snapshot with alerts --------------------------------------------------

    def snapshot(self, *, apply_retention: bool = True) -> dict:
        if apply_retention:
            self.apply_retention()
        snap = self._base.snapshot().to_dict()
        metrics = snap["metrics"]
        # structured aggregation from the evidence log
        metrics["conversation_summary"] = self._summarize_conversations()
        # P9.5: aggregate the pilot runtime's cross-process metrics file
        metrics.update(self._summarize_pilot_metrics())
        # evaluate alert rules
        fired = []
        for rule in self._rules:
            value = rule.evaluate(metrics)
            if value is not None:
                self._fire(rule, value, snap)
                fired.append({"rule": rule.name, "value": value,
                              "threshold": rule.threshold})
        snap["alerts"] = fired
        return snap

    def _summarize_pilot_metrics(self) -> dict:
        """P9.5: aggregate logs/pilot_metrics.jsonl (cross-process source of
        truth written by the pilot runtime) into the dashboard metrics.
        """
        path = Path(self._data_dir) / "logs" / "pilot_metrics.jsonl"
        if not path.exists():
            return {}
        lat = []
        counts = {"messages_received": 0, "messages_answered": 0,
                  "reject_count": 0, "safety_block": 0,
                  "execution_count": 0, "blocked_requests": 0}
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            counts["messages_received"] += 1
            result = rec.get("result", "")
            if rec.get("latency_ms") is not None:
                lat.append(float(rec["latency_ms"]))
            if result == "ANSWERED":
                counts["messages_answered"] += 1
            elif result == "DENIED":
                counts["reject_count"] += 1
                counts["blocked_requests"] += 1
            elif result == "BLOCKED":
                counts["safety_block"] += 1
                counts["blocked_requests"] += 1
            elif result == "EXECUTED":
                counts["execution_count"] += 1
        out = dict(counts)
        if lat:
            lat_sorted = sorted(lat)
            out["reply_latency_ms"] = {
                "count": len(lat_sorted),
                "avg": round(sum(lat_sorted) / len(lat_sorted), 3),
                "p50": round(lat_sorted[int(len(lat_sorted) * 0.50) - 1], 3),
                "p95": round(lat_sorted[int(len(lat_sorted) * 0.95) - 1], 3),
                "max": round(lat_sorted[-1], 3),
            }
        return out

    def _summarize_conversations(self) -> dict:
        ev_path = Path(self._data_dir) / "logs" / "conversation_evidence.jsonl"
        if not ev_path.exists():
            return {"count": 0}
        count = 0
        results: dict[str, int] = {}
        for line in ev_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            count += 1
            r = obj.get("result", "?")
            results[r] = results.get(r, 0) + 1
        return {"count": count, "by_result": results}

    def dump(self, path: Optional[str] = None) -> str:
        snap = self.snapshot()
        text = json.dumps(snap, ensure_ascii=False, indent=2)
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(text, encoding="utf-8")
        return text

    def dashboard(self, path: Optional[str] = None) -> str:
        """P9.3: full operational dashboard JSON (metrics + QQ + latency +
        alerts + retention + rules).  Returns the JSON text; writes it to
        ``path`` when given.
        """
        snap = self.snapshot(apply_retention=True)
        metrics = snap["metrics"]
        lat = metrics.get("reply_latency_ms", {})
        if not isinstance(lat, dict):
            lat = {}
        dash = {
            "generated_at": time.time(),
            "qq_connection": {
                "connected": self._qq_connected,
                "uptime_sec": metrics.get("qq_connection_uptime_sec", 0),
                "disconnects": metrics.get("qq_disconnects", 0),
            },
            "metrics": metrics,
            "latency": {
                "count": lat.get("count", 0),
                "avg_ms": lat.get("avg", 0),
                "p95_ms": lat.get("p95", 0),
                "max_ms": lat.get("max", 0),
            },
            "alerts": snap.get("alerts", []),
            "alert_rules": [
                {"name": r.name, "metric": r.metric, "threshold": r.threshold}
                for r in self._rules
            ],
            "retention": {
                "active": self._retention.active(),
                "max_age_sec": self._retention.max_age_sec,
                "max_count": self._retention.max_count,
            },
        }
        text = json.dumps(dash, ensure_ascii=False, indent=2)
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(text, encoding="utf-8")
        return text
