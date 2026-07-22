import pytest
import asyncio
import time

from anima.cognitive.sylann_adapter import SylannAdapter
from anima.core.persistence import PersistenceProvider


@pytest.mark.asyncio
async def test_shadow_adapter_isolates_identity_and_never_stores_text(tmp_path):
    adapter = SylannAdapter(PersistenceProvider(str(tmp_path)), mode="shadow")
    obs = await adapter.observe(100, 200, "这是不应落盘的原文", hurt=True)
    assert obs.guard == "hurt"
    data = (tmp_path / "sylann" / "shadow_status.json").read_text(encoding="utf-8")
    assert "这是不应落盘的原文" not in data
    assert "200" not in data
    assert '"errors": 0' in data
    assert '"evaluation_sample_target": 100' in data


@pytest.mark.asyncio
async def test_disabled_adapter_has_no_disk_side_effect(tmp_path):
    adapter = SylannAdapter(PersistenceProvider(str(tmp_path)), mode="disabled")
    await adapter.observe(None, 200, "hi")
    assert adapter.health()["enabled"] is False
    assert not (tmp_path / "sylann" / "shadow_status.json").exists()


def test_active_adapter_health_exposes_enabled_flag(tmp_path):
    adapter = SylannAdapter(PersistenceProvider(str(tmp_path)), mode="active")
    assert adapter.health()["enabled"] is True


def test_sylann_telemetry_tracks_expression_and_outbound_without_identity(tmp_path):
    adapter = SylannAdapter(PersistenceProvider(str(tmp_path)), mode="active")
    adapter.record_expression_hint(True)
    adapter.record_expression_hint(False)
    adapter.record_response(success=True, response_chars=24)
    adapter.record_response(success=False, response_chars=7)
    health = adapter.health()
    assert health["expression_hints"] == 1
    assert health["responses"] == 2
    assert health["response_failures"] == 1
    assert health["response_chars"] == 31
    data = (tmp_path / "sylann" / "shadow_status.json").read_text(encoding="utf-8")
    assert "expression_hints" in data
    assert "response_failures" in data
    assert '"response_chars": 31' in data
    assert "user_id" not in data


def test_sylann_telemetry_restores_aggregate_counters_after_restart(tmp_path):
    persistence = PersistenceProvider(str(tmp_path))
    first = SylannAdapter(persistence, mode="active")
    first._count = 7
    first._errors = 1
    first._ticks = 3
    first.record_expression_hint(True)
    first.record_response(success=True, response_chars=55)
    second = SylannAdapter(persistence, mode="active")
    health = second.health()
    assert health["observations"] == 7
    assert health["errors"] == 1
    assert health["ticks"] == 3
    assert health["expression_hints"] == 1
    assert health["responses"] == 1
    assert health["response_chars"] == 55
    assert second.health()["last_surface"] is None


@pytest.mark.asyncio
async def test_engine_shadow_adapter_processes_without_identity_leak(tmp_path):
    import types
    from anima.cognitive.sylann_adapter import EngineSylannAdapter
    core = types.SimpleNamespace(llm=None)
    adapter = EngineSylannAdapter(PersistenceProvider(str(tmp_path)), core, mode="shadow")
    obs = await adapter.observe(100, 200, "测试引擎状态")
    assert obs.session_id
    assert (tmp_path / "sylann" / "shadow_status.json").exists()
    await adapter.close()


@pytest.mark.asyncio
async def test_engine_adapter_deduplicates_same_message_id(tmp_path):
    import types
    from anima.cognitive.sylann_adapter import EngineSylannAdapter

    adapter = EngineSylannAdapter(PersistenceProvider(str(tmp_path)), types.SimpleNamespace(llm=None), mode="shadow")
    first = await adapter.observe(100, 200, "同一条消息", message_id="onebot-1", values={"message_length": 0.1})
    second = await adapter.observe(100, 200, "同一条消息", message_id="onebot-1", values={"message_length": 0.1})
    assert first.session_id == second.session_id
    assert adapter.health()["observations"] == 2
    await adapter.close()


@pytest.mark.asyncio
async def test_engine_shadow_failure_is_counted_without_persisting_input(tmp_path):
    import types
    from anima.cognitive.sylann_adapter import EngineSylannAdapter

    class BrokenEngine:
        async def submit(self, *args, **kwargs):
            raise RuntimeError("engine unavailable")

    adapter = EngineSylannAdapter(PersistenceProvider(str(tmp_path)), types.SimpleNamespace(llm=None), mode="shadow")
    adapter.engine = BrokenEngine()
    with pytest.raises(RuntimeError, match="engine unavailable"):
        await adapter.observe(100, 200, "不得写入的测试原文")
    health = adapter.health()
    assert health["observations"] == 0
    assert health["errors"] == 1
    assert health["ready_for_evaluation"] is False
    data = (tmp_path / "sylann" / "shadow_status.json").read_text(encoding="utf-8")
    assert "不得写入的测试原文" not in data


def test_active_engine_exposes_only_coarse_expression_hint(tmp_path):
    import types
    from anima.cognitive.sylann_adapter import EngineSylannAdapter, SylannObservation

    adapter = EngineSylannAdapter(PersistenceProvider(str(tmp_path)), types.SimpleNamespace(llm=None), mode="active")
    adapter._last = SylannObservation("session", valence=-0.6, arousal=0.1)
    assert adapter.mode == "active"
    assert "克制" in adapter.expression_hint()
    assert "session" not in adapter.expression_hint()


def test_surface_summary_removes_raw_text_and_identity():
    from anima.cognitive.sylann_adapter import _surface_summary

    summary = _surface_summary({
        "turns": 3,
        "pad": {"label": "joy", "valence": 0.6, "arousal": 0.4, "dominance": 0.5, "confidence": 0.9},
        "decision": {"action": "express", "reason_code": "contact", "confidence": 0.8, "urgency": 0.2},
        "guard": {"allowed": True, "risk_score": 0.1, "constraints": []},
        "state": {"needs": {"expression": 0.6, "quiet": 0.1, "recovery": 0.2, "contact": 0.7}},
        "personality": {"surface": {"warmth_bias": 0.7, "directness": 0.4}},
        "raw_text": "不得进入摘要的消息原文 123456",
        "user_id": "123456",
    })
    assert summary["pad"]["label"] == "joy"
    assert "raw_text" not in summary
    assert "user_id" not in summary


@pytest.mark.asyncio
async def test_engine_adapter_ticks_tracked_sessions(tmp_path):
    import types
    from anima.cognitive.sylann_adapter import EngineSylannAdapter

    surface = {
        "turns": 1,
        "pad": {"label": "neutral", "valence": 0.0, "arousal": 0.2, "dominance": 0.5, "confidence": 0.8},
        "decision": {"action": "listen", "reason_code": "steady", "confidence": 0.7, "urgency": 0.1},
        "guard": {"allowed": True, "risk_score": 0.0, "constraints": []},
        "state": {"needs": {}}, "personality": {"surface": {}},
    }

    class Engine:
        async def submit(self, *args, **kwargs):
            return surface

        async def tick(self, *args, **kwargs):
            return surface

    adapter = EngineSylannAdapter(PersistenceProvider(str(tmp_path)), types.SimpleNamespace(llm=None), mode="active")
    adapter.engine = Engine()
    await adapter.observe(100, 200, "hello", message_id="m1")
    observations = await adapter.tick()
    assert len(observations) == 1
    assert adapter.health()["ticks"] == 1
    assert "认真回应" in adapter.expression_hint()


@pytest.mark.asyncio
async def test_nonblocking_engine_keeps_main_loop_responsive(tmp_path):
    from sylanne_core import SylanneConfig, SylanneEngine
    from anima.cognitive.sylann_adapter import _build_nonblocking_engine_class

    engine_type = _build_nonblocking_engine_class(SylanneEngine)
    engine = engine_type(
        tmp_path / "engine",
        llm=lambda *_: asyncio.sleep(0, result=""),
        config=SylanneConfig(mode="lite", assessor_enabled=False),
    )
    await engine.start()

    class SlowHost:
        def on_request(self, event, assessment=None):
            time.sleep(0.20)
            return {"ok": True}

        def flush(self):
            return None

    host = SlowHost()
    engine._get_or_create_host = lambda session_id: host
    engine._to_surface = lambda session_id, current_host, result: result
    marker = asyncio.Event()

    async def mark_while_computing():
        await asyncio.sleep(0.03)
        marker.set()

    compute = asyncio.create_task(engine.process("session", "hello"))
    marker_task = asyncio.create_task(mark_while_computing())
    await asyncio.wait_for(marker.wait(), timeout=0.10)
    assert await compute == {"ok": True}
    await marker_task
    await engine.shutdown()


@pytest.mark.asyncio
async def test_engine_adapter_timeout_degrades_without_blocking_reply(tmp_path, monkeypatch):
    import types
    import anima.cognitive.sylann_adapter as module
    from anima.cognitive.sylann_adapter import EngineSylannAdapter

    class SlowEngine:
        async def submit(self, *args, **kwargs):
            await asyncio.sleep(1.0)

    monkeypatch.setattr(module, "_OBSERVE_TIMEOUT_SEC", 0.02)
    adapter = EngineSylannAdapter(
        PersistenceProvider(str(tmp_path)), types.SimpleNamespace(llm=None), mode="active"
    )
    adapter.engine = SlowEngine()
    started = time.perf_counter()
    observation = await adapter.observe(100, 200, "不会拖住回复", message_id="m-timeout")
    assert time.perf_counter() - started < 0.2
    assert observation.surface["source"] == "timeout_fallback"
    assert adapter.health()["timeouts"] == 1
    assert adapter.health()["circuit_open"] is True
