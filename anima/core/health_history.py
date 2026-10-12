"""持久化的运行健康趋势，只保存聚合指标，不保存聊天正文。"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from anima.core.persistence import PersistenceProvider

if TYPE_CHECKING:
    from anima.core.anima_core import AnimaCore


#: 固定在每份快照里的阶段键（2026-10-05）——`[阶段耗时]` 会打的全部阶段。
#: 显式列出而非从数据推导：这样**没跑过回合时字段也在**（count=0），
#: 读的人才分得清"没数据"与"字段缺失"。
_STAGE_KEYS = (
    "total",
    "entry_to_pipeline",
    "pre_react",
    "prefilter_stage",
    "attachment_text_stage",
    "bullying_stage",
    "permission_stage",
    "memory_stage",
    "cognition_stage",
    "generation_stage",
    "guardrail_stage",
)


def _empty_latency() -> dict[str, Any]:
    """无样本时的零值占位（字段形状与真实分位一致）。"""
    return {"count": 0, "p50": 0.0, "p99": 0.0, "avg": 0.0, "max": 0.0}


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
        cap = self._cap_usage()
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
            # ── 阶段耗时分位（2026-10-05 新增）─────────────────────────
            # 数据源：`[阶段耗时]` 已有的日志 → `MetricsCollector.histogram`
            # → `observability.latency_snapshot()`。**只加字段，不改任何既有字段**
            # （WebUI 在读它们），也不新增周期任务（复用 `record()` 既有调用点）。
            "stage_latency_ms": self._stage_latency(),
            # ── 上游（CAP）自述的用量与失败归因（2026-10-11 新增）──────
            # 🔴 这条是**断链修复**：此前 Anima 只能靠自己被打回来才知道上游出事
            # （10-11 实测：CPA 13:25 已判死某模型并冷却 9.27h，Anima 侧累积 89 次
            # 失败才切换）。CAP 插件其实一直记着 failed/failure_status/effort，
            # 只是没人读。**只加字段，不新增周期任务**。
            # fail-closed：CAP 不可达 ⇒ `upstream_reachable=False`，**不谎报健康**。
            "upstream_cap": cap,
        }

    def _cap_usage(self) -> dict[str, Any]:
        """拉一次 CAP 用量快照（只读 HTTP）。

        🔴 **必须是同步实现，不能用 `asyncio.run(probe.fetch())`。**
        `record()` 的调用方（`Heartbeat.start()` / `Heartbeat._loop()`）**本身就在
        运行中的事件循环里** ⇒ `asyncio.run` 会抛
        `RuntimeError: asyncio.run() cannot be called from a running event loop`，
        被这里的 `except` 吞掉 ⇒ **每次都静默返回"不可达"**（10-11 实测：
        `upstream_cap.reachable=None`、groups=0，同时 stderr 打
        `coroutine 'fetch' was never awaited`）。所以走同步路径。

        失败即 `upstream_reachable=False` —— 读不到就是未知，
        绝不能因为"拉不动"就当成"上游正常"。同理**不缓存**：
        缓存会让一次成功掩盖随后的长时间失联。
        """
        probe = getattr(self.core, "cap_probe", None)
        if probe is None:
            return {"configured": False}
        try:
            snapshot = probe.fetch_sync()
            # 🔴 `to_dict()` 也必须在 try 内 —— 否则探针返回非预期类型时
            # `AttributeError` 会掀翻整次健康采样，**让监控自己变成故障源**。
            data = snapshot.to_dict()
        except Exception as exc:  # noqa: BLE001 — 探针绝不影响健康采样
            return {"configured": True, "upstream_reachable": False,
                    "error": f"{type(exc).__name__}: {exc}"}
        if not isinstance(data, dict):  # 防御：形状不对即视为不可达
            return {"configured": True, "upstream_reachable": False,
                    "error": "探针返回非 dict"}
        data["configured"] = True
        return data

    def _stage_latency(self) -> dict[str, Any]:
        """各阶段耗时分位；无样本的阶段补零值占位。"""
        try:
            from anima.pipeline.stages.observability import (
                STAGE_LATENCY_METRIC, latency_snapshot,
            )
        except Exception:  # noqa: BLE001 — 观测不可用时不得影响健康采样
            return {}
        try:
            data = latency_snapshot()
        except Exception:  # noqa: BLE001
            return {}
        if not isinstance(data, dict):
            return {}
        # 🔴 无样本的阶段**仍给零值占位**（"有字段但 count=0" ≠ "字段缺失"）——
        # 后者会让排查误判成"功能没实现"。
        for stage in _STAGE_KEYS:
            data.setdefault(stage, _empty_latency())
        data["_metric"] = STAGE_LATENCY_METRIC
        return data

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

    def anomaly_report(self, *, hours: float = 24.0) -> dict[str, Any]:
        now = time.time()
        start = now - float(hours) * 3600.0
        window = [x for x in self._samples if float(x.get('ts', 0.0) or 0.0) >= start]
        if not window:
            return {"hours": hours, "samples": 0, "summary": "no samples in window",
                    "anomalies": [], "latest": None}
        latest = window[-1]
        anomalies = []
        disc = [x for x in window if not bool(x.get('channel_connected'))]
        if disc:
            anomalies.append({"type": "channel_disconnect", "count": len(disc),
                              "detail": "QQ channel disconnected on %d samples" % len(disc)})
        nobeat = [x for x in window if not bool(x.get('heartbeat_running', True))]
        if nobeat:
            anomalies.append({"type": "heartbeat_loss", "count": len(nobeat),
                              "detail": "heartbeat lost on %d samples" % len(nobeat)})
        badwd = [x for x in window if not bool(x.get('watchdog_running', True))
                 or float(x.get('watchdog_lag_sec', 0.0) or 0.0) > 5.0]
        if badwd:
            anomalies.append({"type": "watchdog_lag", "count": len(badwd),
                              "detail": "watchdog lagged/stopped on %d samples" % len(badwd)})
        circuit = [x for x in window if bool(x.get('llm_circuit_open'))]
        if circuit:
            anomalies.append({"type": "llm_circuit_open", "count": len(circuit),
                              "detail": "LLM circuit breaker open on %d samples" % len(circuit)})
        lreq = sum(int(x.get('llm_requests', 0) or 0) for x in window)
        lfail = sum(int(x.get('llm_failures', 0) or 0) for x in window)
        if lreq > 0 and (lfail / lreq) > 0.10:
            anomalies.append({"type": "llm_high_failure", "count": lfail, "total": lreq,
                              "detail": "LLM failure rate %.0f%% (%d/%d)" % ((lfail / lreq) * 100, lfail, lreq)})
        bp = sum(int(x.get('events_backpressure_dropped', 0) or 0) for x in window)
        obf = sum(int(x.get('outbound_failures', 0) or 0) for x in window)
        if bp:
            anomalies.append({"type": "backpressure_drop", "count": bp,
                              "detail": "%d events dropped by backpressure" % bp})
        if obf:
            anomalies.append({"type": "outbound_failure", "count": obf,
                              "detail": "%d outbound failures" % obf})
        restarts = 0
        prev = None
        for x in window:
            cur = bool(x.get('running'))
            if prev is False and cur is True:
                restarts += 1
            prev = cur
        if restarts:
            anomalies.append({"type": "process_restart", "count": restarts,
                              "detail": "detected %d process restarts" % restarts})
        return {"hours": hours, "samples": len(window), "window_start": start,
                "latest": {"ts": latest.get('ts'),
                           "running": bool(latest.get('running')),
                           "channel_connected": bool(latest.get('channel_connected')),
                           "heartbeat_running": bool(latest.get('heartbeat_running'))},
                "summary": "ok" if not anomalies else "%d anomaly types" % len(anomalies),
                "anomalies": anomalies}

