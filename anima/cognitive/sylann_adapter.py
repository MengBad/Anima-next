"""Sylanne-Core v2.5 与 Anima 认知运行时的受控桥接。

引擎拥有自己的会话状态、情绪 Surface、关系动力学、伤痕热池与心跳 tick；
Anima 只读取经过摘要化的公开 Surface 来调整表达。QQ 出站、工具、权限、
聊天记忆和原始消息仍由 Anima 独占，不能被上游引擎绕过。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from datetime import datetime, timezone
from collections import deque
from dataclasses import dataclass
from typing import Any

from anima.core.persistence import PersistenceProvider
from anima.llm.usage import llm_scope

logger = logging.getLogger("anima.cognitive.sylann")

_OBSERVE_TIMEOUT_SEC = 15.0  # 原 25s，缩短以减少 pipeline 阻塞（超时后有 60s 熔断降级）
_TICK_TIMEOUT_SEC = 8.0
_START_TIMEOUT_SEC = 12.0
_TIMEOUT_COOLDOWN_SEC = 60.0


@dataclass(frozen=True)
class SylannObservation:
    session_id: str
    action: str = "hold"
    valence: float = 0.0
    arousal: float = 0.0
    guard: str = "none"
    label: str = "neutral"
    urgency: float = 0.0
    surface: dict[str, Any] | None = None


class SylannAdapter:
    """无上游依赖时的零行为适配器，保留统一观测和健康契约。"""

    def __init__(self, persistence: PersistenceProvider, mode: str = "disabled", weight: float = 0.15):
        self.persistence = persistence
        self.mode = mode if mode in {"disabled", "shadow", "active"} else "disabled"
        self.weight = max(0.0, min(float(weight), 1.0))
        self._count = 0
        self._ticks = 0
        self._errors = 0
        self._latency_total_ms = 0.0
        self._latency_max_ms = 0.0
        self._last_error_at = 0.0
        self._last: SylannObservation | None = None
        self._recent: deque[dict[str, Any]] = deque(maxlen=12)
        # 仅记录脱敏运行指标，不保存原文、QQ 号或完整会话状态。
        self._expression_hints = 0
        self._responses = 0
        self._response_failures = 0
        self._response_chars = 0
        self._last_feedback_at = 0.0
        self._restore_metrics()

    @staticmethod
    def session_id(group_id: Any, user_id: Any) -> str:
        source = f"{group_id if group_id is not None else 'private'}:{user_id}"
        return hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]

    def _restore_metrics(self) -> None:
        """只恢复聚合计数；旧的 Surface、会话标识和原文永不回灌运行时。"""
        raw = self.persistence.read_json("sylann/shadow_status.json", {})
        if not isinstance(raw, dict):
            return
        raw_names = {
            "_count": "observations", "_ticks": "ticks", "_errors": "errors",
            "_expression_hints": "expression_hints", "_responses": "responses",
            "_response_failures": "response_failures",
        }
        for name, raw_name in raw_names.items():
            try:
                setattr(self, name, max(0, min(int(raw.get(raw_name, 0) or 0), 10_000_000)))
            except (TypeError, ValueError):
                continue
        try:
            self._response_chars = max(0, min(int(raw.get("response_chars", 0) or 0), 2_000_000_000))
        except (TypeError, ValueError):
            self._response_chars = 0
        try:
            attempts = self._count + self._errors
            average = max(0.0, min(float(raw.get("average_latency_ms", 0.0) or 0.0), 120_000.0))
            self._latency_total_ms = average * attempts
            self._latency_max_ms = max(0.0, min(float(raw.get("max_latency_ms", 0.0) or 0.0), 120_000.0))
        except (TypeError, ValueError):
            self._latency_total_ms = self._latency_max_ms = 0.0

    async def observe(self, group_id: Any, user_id: Any, text: str, *, hurt: bool = False,
                      message_id: str | None = None, values: dict[str, float] | None = None) -> SylannObservation:
        started = time.perf_counter()
        observation = SylannObservation(
            session_id=self.session_id(group_id, user_id),
            guard="hurt" if hurt else "none",
            surface={"source": "fallback", "message_signal": "hurt" if hurt else "neutral"},
        )
        self._record_outcome(started, success=True)
        self._remember(observation)
        self._write_shadow_status()
        return observation

    async def tick(self, max_sessions: int | None = None) -> list[SylannObservation]:
        return []

    def health(self) -> dict[str, Any]:
        attempts = self._count + self._errors
        return {
            "enabled": self.mode != "disabled",
            "mode": self.mode,
            "weight": self.weight,
            "observations": self._count,
            "ticks": self._ticks,
            "errors": self._errors,
            "attempts": attempts,
            "error_rate": self._errors / attempts if attempts else 0.0,
            "average_latency_ms": round(self._latency_total_ms / attempts, 2) if attempts else 0.0,
            "max_latency_ms": round(self._latency_max_ms, 2),
            "evaluation_sample_target": 100,
            "ready_for_evaluation": self._count >= 100 and self._errors == 0,
            "expression_hints": self._expression_hints,
            "expression_hint_rate": self._expression_hints / self._count if self._count else 0.0,
            "responses": self._responses,
            "response_failures": self._response_failures,
            "response_success_rate": ((self._responses - self._response_failures) / self._responses
                                       if self._responses else 0.0),
            "response_chars": self._response_chars,
            "active_expression": self.mode == "active" and self._expression_hints > 0,
            "last_surface": self._last.surface if self._last else None,
            "recent_surfaces": list(self._recent),
            "engine": "not-installed",
        }

    def expression_hint(self) -> str:
        return ""

    def record_response(self, *, success: bool, response_chars: int = 0) -> None:
        """记录脱敏的出站反馈，不保存消息内容或身份信息。"""
        self._responses += 1
        if not success:
            self._response_failures += 1
        self._response_chars += max(0, min(int(response_chars), 20_000))
        self._last_feedback_at = time.time()
        self._write_shadow_status()

    def record_expression_hint(self, applied: bool) -> None:
        """记录本轮是否真的把表达倾向交给了生成阶段。"""
        if applied:
            self._expression_hints += 1
            self._write_shadow_status()

    async def close(self) -> None:
        return None

    def _record_outcome(self, started: float, *, success: bool) -> None:
        latency_ms = (time.perf_counter() - started) * 1000
        self._latency_total_ms += latency_ms
        self._latency_max_ms = max(self._latency_max_ms, latency_ms)
        if success:
            self._count += 1
        else:
            self._errors += 1
            self._last_error_at = time.time()

    def _remember(self, observation: SylannObservation) -> None:
        self._last = observation
        if observation.surface:
            self._recent.append({
                "action": observation.action,
                "label": observation.label,
                "guard": observation.guard,
                "urgency": observation.urgency,
                "surface": observation.surface,
            })

    def _write_shadow_status(self) -> None:
        if self.mode not in {"shadow", "active"}:
            return
        health = self.health()
        last = self._last
        # 仅写匿名会话摘要、聚合指标和公开 Surface；不写原文、QQ 号或完整上游内部状态。
        self.persistence.write_json("sylann/shadow_status.json", {
            # ISO timestamp keeps the privacy status file human-readable and
            # avoids confusing a timestamp with a persisted QQ/user id.
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "mode": self.mode,
            "weight": self.weight,
            "observations": self._count,
            "ticks": self._ticks,
            "errors": self._errors,
            "error_rate": health["error_rate"],
            "average_latency_ms": health["average_latency_ms"],
            "max_latency_ms": health["max_latency_ms"],
            "last_error_at": self._last_error_at,
            "evaluation_sample_target": 100,
            "ready_for_evaluation": health["ready_for_evaluation"],
            "expression_hints": self._expression_hints,
            "expression_hint_rate": health["expression_hint_rate"],
            "responses": self._responses,
            "response_failures": self._response_failures,
            "response_success_rate": health["response_success_rate"],
            "response_chars": self._response_chars,
            "last_feedback_at": self._last_feedback_at,
            "last": ({
                "session_id": last.session_id,
                "action": last.action,
                "label": last.label,
                "guard": last.guard,
                "urgency": last.urgency,
                "surface": last.surface,
            } if last else None),
        })


def _build_nonblocking_engine_class(base: type) -> type:
    """为 v2.5 引擎把同步计算/落盘移出 Anima 主事件循环。

    Sylanne 的公开方法是 async，但 v2.5 的 ``host.on_request``、快照序列化和
    ``fsync`` 仍在协程内部同步执行。这里保留其会话锁、去重和 LLM 回调语义，
    只把锁内的纯同步部分交给工作线程，防止 QQ/WebUI/systemd 心跳一起假死。
    """

    class NonBlockingSylanneEngine(base):
        async def start(self) -> None:
            def initialise() -> None:
                self._data_dir.mkdir(parents=True, exist_ok=True)
                if self._config_from_file:
                    from sylanne_core._config_store import write_default_config
                    write_default_config(self._data_dir)
                self._status = "running"

            await asyncio.to_thread(initialise)

        async def process(
            self, session_id: str, text: str, *, confidence=None, flags=None,
            now=None, values=None,
        ):
            await self._ensure_started()
            assessment = await self._assess(text) if self._config.assessor_enabled else None
            async with self._session_lock(session_id):
                def compute():
                    host = self._get_or_create_host(session_id)
                    event = {
                        "text": text,
                        "confidence": confidence if confidence is not None else (assessment or {}).get("confidence", 0.0),
                        "flags": flags if flags is not None else (assessment or {}).get("flags", []),
                        "now": now if now is not None else time.time(),
                        "values": values or {},
                    }
                    result = host.on_request(event, assessment=assessment)
                    return self._to_surface(session_id, host, result)

                surface = await asyncio.to_thread(compute)
            await self._notify(session_id, surface)
            return surface

        async def tick(self, session_id: str, flags=None, *, force: bool = False):
            await self._ensure_started()
            now_ts = time.time()
            if not force:
                cached = self._last_tick.get(session_id)
                if cached is not None and now_ts - cached[0] < self._config.tick_min_interval_seconds:
                    return cached[1]
            async with self._session_lock(session_id):
                def compute():
                    host = self._get_or_create_host(session_id)
                    event = {
                        "text": "", "confidence": 0.0,
                        "flags": flags or ["idle"], "now": now_ts, "values": {},
                    }
                    result = host.on_request(event)
                    return self._to_surface(session_id, host, result)

                surface = await asyncio.to_thread(compute)
            self._last_tick[session_id] = (now_ts, surface)
            return surface

        async def inject(
            self, session_id: str, source: str, influence_type: str, intensity: float,
            target_dimension: str = "", payload=None,
        ) -> None:
            from sylanne_core.compute.hot_pool import _VALID_INFLUENCE_TYPES
            if influence_type not in _VALID_INFLUENCE_TYPES:
                raise ValueError(f"Invalid influence_type {influence_type!r}")
            await self._ensure_started()
            async with self._session_lock(session_id):
                def apply_influence() -> None:
                    from sylanne_core.compute.hot_pool import Influence
                    host = self._get_or_create_host(session_id)
                    host.kernel.hot_pool.receive_influence(Influence(
                        source=source,
                        type=influence_type,
                        intensity=max(0.0, min(1.0, intensity)),
                        target_dimension=target_dimension,
                        payload=payload or {},
                    ))

                await asyncio.to_thread(apply_influence)

        async def shutdown(self) -> None:
            def flush_hosts() -> bool:
                had_error = False
                for session_id, host in self._hosts.items():
                    try:
                        host.flush()
                    except Exception:
                        had_error = True
                        logger.warning("Sylann 关闭时会话 %r 落盘失败", session_id, exc_info=True)
                if self._telemetry_sink is not None:
                    self._telemetry_sink.close()
                return had_error

            had_error = await asyncio.to_thread(flush_hosts)
            self._hosts.clear()
            self._locks.clear()
            self._cancel_and_clear_submissions()
            self._last_tick.clear()
            self._status = "degraded" if had_error else "closed"

    return NonBlockingSylanneEngine


class EngineSylannAdapter(SylannAdapter):
    """Sylanne-Core v2.5 完整 Surface/submit/tick/inject 生命周期桥接。"""

    def __init__(self, persistence: PersistenceProvider, core: Any, mode: str = "shadow"):
        config = getattr(core, "config", None)
        sylann_cfg = getattr(config, "sylann", None)
        super().__init__(persistence, mode, getattr(sylann_cfg, "weight", 0.15))
        self.core = core
        self.engine: Any | None = None
        self._engine_mode = str(getattr(sylann_cfg, "engine_mode", "lite"))
        self._assessor_enabled = bool(getattr(sylann_cfg, "assessor_enabled", True))
        self._pel_core_enabled = bool(getattr(sylann_cfg, "pel_core_enabled", True))
        self._tick_sessions_limit = max(1, min(int(getattr(sylann_cfg, "tick_sessions_limit", 32)), 128))
        self._sessions: dict[str, float] = {}
        self._timeouts = 0
        self._circuit_open_until = 0.0

    async def start(self) -> None:
        if self.engine is not None:
            return
        from sylanne_core import SylanneConfig as EngineConfig, SylanneEngine

        async def llm(system: str, user: str) -> str:
            client = getattr(self.core, "llm", None)
            if client is None:
                return ""
            with llm_scope("sylann", "assessor"):
                return await client.chat(
                    [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    temperature=0.2, max_tokens=256,
                )

        embedder = getattr(getattr(self.core, "vector_memory", None), "embedding", None)

        async def embedding(text: str) -> list[float]:
            if embedder is None:
                return []
            vector = await embedder.embed(text)
            return vector or []

        engine_mode = self._engine_mode if self._engine_mode in {"lite", "pro", "max"} else "lite"
        engine_class = _build_nonblocking_engine_class(SylanneEngine)
        engine_config = EngineConfig(
                mode=engine_mode,
                assessor_enabled=self._assessor_enabled,
                pel_core_enabled=self._pel_core_enabled,
                locale="zh",
        )
        self.engine = await asyncio.to_thread(
            engine_class,
            self.persistence.data_dir / "sylann" / "engine",
            llm=llm,
            embedding=embedding if embedder is not None else None,
            config=engine_config,
        )
        await asyncio.wait_for(self.engine.start(), timeout=_START_TIMEOUT_SEC)

    async def observe(self, group_id: Any, user_id: Any, text: str, *, hurt: bool = False,
                      message_id: str | None = None, values: dict[str, float] | None = None) -> SylannObservation:
        started = time.perf_counter()
        sid = self.session_id(group_id, user_id)
        if time.time() < self._circuit_open_until:
            return SylannObservation(
                sid,
                guard="hurt" if hurt else "none",
                surface={"source": "timeout_circuit", "message_signal": "hurt" if hurt else "neutral"},
            )
        try:
            await self.start()
            assert self.engine is not None
            flags = ["hurt", "boundary"] if hurt else ["safe"]
            if hurt:
                # 只给上游传入类别和强度，不传原始侮辱内容或 QQ 身份。
                await asyncio.wait_for(
                    self.engine.inject(
                        sid, "anima-next", "betrayal", 0.22,
                        target_dimension="boundary", payload={"category": "hurtful_language"},
                    ),
                    timeout=_TICK_TIMEOUT_SEC,
                )
            surface = await asyncio.wait_for(
                self.engine.submit(
                    sid, text, msg_id=message_id, flags=flags,
                    values=values or {}, plugin="anima-next",
                ),
                timeout=_OBSERVE_TIMEOUT_SEC,
            )
            observation = self._observation_from_surface(sid, surface)
            self._sessions[sid] = time.time()
            self._record_outcome(started, success=True)
            self._remember(observation)
            self._write_shadow_status()
            return observation
        except asyncio.TimeoutError:
            self._timeouts += 1
            self._circuit_open_until = time.time() + _TIMEOUT_COOLDOWN_SEC
            self._record_outcome(started, success=False)
            self._write_shadow_status()
            logger.warning(
                "Sylann 观测超时，已降级并熔断 %.0fs session=%s",
                _TIMEOUT_COOLDOWN_SEC, sid,
            )
            return SylannObservation(
                sid,
                guard="hurt" if hurt else "none",
                surface={"source": "timeout_fallback", "message_signal": "hurt" if hurt else "neutral"},
            )
        except Exception:
            self._record_outcome(started, success=False)
            self._write_shadow_status()
            raise

    async def tick(self, max_sessions: int | None = None) -> list[SylannObservation]:
        if self.engine is None or not self._sessions:
            return []
        limit = max_sessions or self._tick_sessions_limit
        sessions = sorted(self._sessions, key=self._sessions.get, reverse=True)[:limit]
        observations: list[SylannObservation] = []
        for sid in sessions:
            started = time.perf_counter()
            try:
                surface = await asyncio.wait_for(
                    self.engine.tick(sid, flags=["idle"]),
                    timeout=_TICK_TIMEOUT_SEC,
                )
                observation = self._observation_from_surface(sid, surface)
                self._ticks += 1
                self._record_outcome(started, success=True)
                self._remember(observation)
                observations.append(observation)
            except asyncio.TimeoutError:
                self._timeouts += 1
                self._circuit_open_until = time.time() + _TIMEOUT_COOLDOWN_SEC
                self._record_outcome(started, success=False)
                logger.warning("Sylann 心跳超时，终止本轮剩余会话 session=%s", sid)
                break
            except Exception:
                self._record_outcome(started, success=False)
        self._write_shadow_status()
        return observations

    def _observation_from_surface(self, session_id: str, surface: dict[str, Any]) -> SylannObservation:
        summary = _surface_summary(surface)
        pad = summary["pad"]
        decision = summary["decision"]
        guard = summary["guard"]
        guard_state = "active" if not guard["allowed"] or guard["constraints"] else "observed"
        return SylannObservation(
            session_id=session_id,
            action=decision["action"],
            valence=pad["valence"],
            arousal=pad["arousal"],
            guard=guard_state,
            label=pad["label"],
            urgency=decision["urgency"],
            surface=summary,
        )

    def health(self) -> dict[str, Any]:
        base = super().health()
        package_version: str | None
        try:
            from importlib.metadata import version
            package_version = version("sylanne-core")
        except Exception:
            package_version = None
        engine_health = self.engine.health() if self.engine is not None and hasattr(self.engine, "health") else None
        submit_stats = self.engine.submit_stats() if self.engine is not None and hasattr(self.engine, "submit_stats") else None
        return {
            **base,
            "engine": "running" if self.engine else "lazy",
            "package_version": package_version,
            "engine_mode": self._engine_mode,
            "assessor_enabled": self._assessor_enabled,
            "pel_core_enabled": self._pel_core_enabled,
            "tracked_sessions": len(self._sessions),
            "timeouts": self._timeouts,
            "circuit_open": time.time() < self._circuit_open_until,
            "engine_health": engine_health,
            "submit_stats": submit_stats,
        }

    def expression_hint(self) -> str:
        """把完整 Surface 压缩为一条不泄露内部状态的表达建议。"""
        if self.mode != "active" or self._last is None:
            return ""
        surface = self._last.surface or {}
        guard = surface.get("guard", {})
        needs = surface.get("needs", {})
        decision = surface.get("decision", {})
        if not guard.get("allowed", True) or float(guard.get("risk_score", 0.0)) >= 0.7:
            return "语气收敛，避免挑衅、施压或延展敏感话题；必要时简短说明边界。"
        if self._last.guard == "active":
            return "放轻语气，先接住对方的话，不要用玩笑刺激或逼问。"
        if float(needs.get("recovery", 0.0)) >= 0.65 or float(needs.get("quiet", 0.0)) >= 0.65:
            return "语句简短、节奏放慢，不要堆叠玩笑、表情或追问。"
        if decision.get("action") in {"listen", "hold"}:
            return "先认真回应眼前这句话，少打断、少自顾自展开新话题。"
        if self.weight < 0.1:
            return ""
        if self._last.valence < -0.35:
            return "保持克制和关照，不要冷嘲或过度兴奋。"
        if self._last.valence > 0.45 and self._last.arousal < 0.75:
            return "可以自然亲近一点，但不要夸张撒娇或强行煽情。"
        return ""

    async def close(self) -> None:
        if self.engine is not None:
            await self.engine.shutdown()
        self.engine = None


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return max(-1.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _unit(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _surface_summary(surface: dict[str, Any]) -> dict[str, Any]:
    """将 v2.5 全量 Surface 压缩为可审计且不含原文/身份的摘要。"""
    pad = surface.get("pad") or {}
    decision = surface.get("decision") or {}
    guard = surface.get("guard") or {}
    state = surface.get("state") or {}
    needs = state.get("needs") or {}
    personality = (surface.get("personality") or {}).get("surface") or {}
    constraints = guard.get("constraints") or []
    return {
        "turns": max(0, int(surface.get("turns", 0) or 0)),
        "pad": {
            "label": str(pad.get("label") or "neutral")[:32],
            "valence": _number(pad.get("valence")),
            "arousal": _unit(pad.get("arousal")),
            "dominance": _unit(pad.get("dominance")),
            "confidence": _unit(pad.get("confidence")),
        },
        "decision": {
            "action": str(decision.get("action") or "hold")[:32],
            "reason_code": str(decision.get("reason_code") or "")[:80],
            "confidence": _unit(decision.get("confidence")),
            "urgency": _unit(decision.get("urgency")),
        },
        "guard": {
            "allowed": bool(guard.get("allowed", True)),
            "risk_score": _unit(guard.get("risk_score")),
            "constraints": [str(item)[:80] for item in constraints[:4]],
        },
        "needs": {key: _unit(needs.get(key)) for key in ("expression", "quiet", "recovery", "contact")},
        "personality": {key: _unit(personality.get(key)) for key in (
            "warmth_bias", "directness", "curiosity", "patience", "intimacy_pull", "autonomy_guard",
        )},
    }
