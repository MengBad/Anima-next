"""持久化的运行健康趋势，只保存聚合指标，不保存聊天正文。"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from anima.core.persistence import PersistenceProvider

if TYPE_CHECKING:
    from anima.core.anima_core import AnimaCore


class HealthHistory:
    """保留最近约 48 小时的健康样本，供诊断和 WebUI 趋势使用。"""

    def __init__(self, core: "AnimaCore", data_dir: str,
                 *, max_samples: int = 2880, min_interval_sec: float = 60.0):
        self.core = core
        self.persistence = PersistenceProvider(data_dir)
        self.path = "health_history.json"
        self.max_samples = max(60, int(max_samples))
        self.min_interval_sec = max(1.0, float(min_interval_sec))
        self._samples: list[dict[str, Any]] = []
        self._last_record_at = 0.0
        self._restore()

    def _restore(self) -> None:
        raw = self.persistence.read_json(self.path, {}) or {}
        samples = raw.get("samples", []) if isinstance(raw, dict) else []
        if not isinstance(samples, list):
            return
        self._samples = [item for item in samples if isinstance(item, dict)][-self.max_samples:]
        if self._samples:
            self._last_record_at = float(self._samples[-1].get("ts", 0.0) or 0.0)

    def _snapshot(self, now: float) -> dict[str, Any]:
        from anima.watchdog import watchdog_health

        channel = getattr(getattr(self.core, "gateway", None), "channel", None)
        try:
            channel_health = channel.health() if channel and hasattr(channel, "health") else {}
        except Exception:
            channel_health = {}
        try:
            watchdog = watchdog_health()
        except Exception:
            watchdog = {}
        try:
            llm = self.core.llm.health() if self.core.llm and hasattr(self.core.llm, "health") else {}
        except Exception:
            llm = {}
        try:
            sylann = self.core.sylann.health() if self.core.sylann else {}
        except Exception:
            sylann = {}
        try:
            audio = self.core.llm_handler.audio_health() if self.core.llm_handler and hasattr(self.core.llm_handler, "audio_health") else {}
        except Exception:
            audio = {}
        agenda = getattr(self.core, "agenda", None)
        approvals = getattr(self.core, "approvals", None)
        monitors = getattr(self.core, "monitors", None)
        subscriptions = getattr(getattr(monitors, "store", None), "items", {}) or {}
        drafts = getattr(self.core, "skill_drafts", None)
        heartbeat = getattr(self.core, "heartbeat", None)
        try:
            skill_drafts_pending = len(drafts.pending()) if drafts and hasattr(drafts, "pending") else 0
        except Exception:
            skill_drafts_pending = 0
        return {
            "ts": round(now, 3),
            "running": bool(getattr(self.core, "_running", False)),
            "channel_connected": bool(channel_health.get("connected")),
            "heartbeat_running": bool(heartbeat and getattr(heartbeat, "_running", False)),
            "watchdog_running": bool(watchdog.get("running")),
            "watchdog_lag_sec": float(watchdog.get("last_lag_sec", 0.0) or 0.0),
            "watchdog_max_lag_sec": float(watchdog.get("max_lag_sec", 0.0) or 0.0),
            "watchdog_late_ticks": int(watchdog.get("late_ticks", 0) or 0),
            "llm_requests": int(llm.get("request_count", 0) or 0),
            "llm_failures": int(llm.get("failure_count", 0) or 0),
            "sylann_observations": int(sylann.get("observations", 0) or 0),
            "sylann_ticks": int(sylann.get("ticks", 0) or 0),
            "sylann_errors": int(sylann.get("errors", 0) or 0),
            "sylann_responses": int(sylann.get("responses", 0) or 0),
            "sylann_expression_hints": int(sylann.get("expression_hints", 0) or 0),
            "audio_requests": int(audio.get("requests", 0) or 0),
            "audio_successes": int(audio.get("successes", 0) or 0),
            "audio_failures": int(audio.get("failures", 0) or 0),
            "audio_throttled": int(audio.get("throttled", 0) or 0),
            "agenda_pending": len(agenda.pending()) if agenda and hasattr(agenda, "pending") else 0,
            "approvals_pending": len(approvals.pending()) if approvals and hasattr(approvals, "pending") else 0,
            "subscriptions_total": len(subscriptions),
            "subscriptions_enabled": sum(1 for item in subscriptions.values() if bool(getattr(item, "enabled", False))),
            "subscriptions_live": sum(1 for item in subscriptions.values() if getattr(item, "last_state", "") == "live"),
            "skill_drafts_pending": skill_drafts_pending,
            "intents_open": int((getattr(self.core, "intent_ledger", None).summary() or {}).get("open", 0))
            if getattr(self.core, "intent_ledger", None) else 0,
            "workflows_active": int((getattr(self.core, "workflows", None).summary() or {}).get("active", 0))
            if getattr(self.core, "workflows", None) else 0,
            "episodes_total": int((getattr(self.core, "episodes", None).summary() or {}).get("episodes", 0))
            if getattr(self.core, "episodes", None) else 0,
            "commitments_open": int((getattr(self.core, "episodes", None).summary() or {}).get("commitments_open", 0))
            if getattr(self.core, "episodes", None) else 0,
            "events_backpressure_dropped": int(channel_health.get("events_backpressure_dropped", 0) or 0),
            "outbound_failures": int(channel_health.get("outbound_failures", 0) or 0),
            "llm_circuit_open": bool(float(llm.get("circuit_open_until") or 0) > now),
            "llm_failover_count": int(llm.get("failover_count", 0) or 0),
            "llm_degradation_count": int(llm.get("degradation_count", 0) or 0),
        }

    def record(self, *, force: bool = False) -> dict[str, Any] | None:
        now = time.time()
        if not force and now - self._last_record_at < self.min_interval_sec:
            return None
        sample = self._snapshot(now)
        self._samples.append(sample)
        self._samples = self._samples[-self.max_samples:]
        self._last_record_at = now
        self.persistence.write_json(self.path, {"version": 1, "samples": self._samples})
        return dict(sample)

    def query(self, *, start: float | None = None, end: float | None = None,
              limit: int = 2880) -> list[dict[str, Any]]:
        start = float(start if start is not None else 0.0)
        end = float(end if end is not None else time.time() + 1.0)
        limit = max(1, min(int(limit), self.max_samples))
        result = [item for item in self._samples
                  if start <= float(item.get("ts", 0.0) or 0.0) <= end]
        return result[-limit:]

    def health(self) -> dict[str, Any]:
        latest = self._samples[-1] if self._samples else None
        return {"samples": len(self._samples),
                "latest_at": latest.get("ts") if latest else None,
                "retention_samples": self.max_samples,
                "sample_interval_sec": self.min_interval_sec}
