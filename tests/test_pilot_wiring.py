"""P8.9 — Real Wiring (RED contract).

OneBot11Adapter -> PilotRuntime front + LLMHandler fallback.
Requirements:
    * PilotRuntime is tried FIRST
    * LLMHandler fallback preserved (fallback replies when pilot did NOT)
    * kill switch effective (pilot DISABLED -> everything falls to LLM)
    * no double reply

Acceptance:
    A  pilot handles -> coordinator replies via gateway, LLM NOT called
    B  pilot not handled (non-pilot group) -> LLM fallback called
    C  pilot not handled (untriggered) -> LLM fallback called
    D  kill switch disabled -> LLM fallback called (pilot inert)
    E  coordinator failure -> safe wrapper falls back to LLM
    F  stats: pilot_handled / llm_fallback counts
    G  static boundary: no System B / gateway mutation / capability_mode
"""
from __future__ import annotations

import pathlib

import pytest

from anima.pilot.pilot_runtime import LivePilotConfig, PilotRuntime
from anima.pilot.wiring import (
    PilotCoordinator,
    build_pilot_coordinator,
    should_llm_handle,
)


def _event(**overrides):
    ev = {
        "topic": "event.message.group",
        "user_id": 10001,
        "group_id": "123456",
        "message_id": "m-1",
        "text": "什么是量子计算？",
        "time": 1786450000,
        "mentions_self": True,
        "raw": {"message_type": "group", "sender": {"nickname": "u1"}},
    }
    ev.update(overrides)
    return ev


class _FakeCore:
    def __init__(self):
        self.sent = []

    async def gateway_send(self, detail_type, target_id, message):
        self.sent.append((detail_type, target_id, message))
        return "mid-1"


def _make(tmp_path, *, runtime=None, fallback_impl=None):
    if runtime is None:
        runtime = PilotRuntime(
            config=LivePilotConfig(allowed_groups=("123456",), min_interval_sec=0.0),
            data_dir=str(tmp_path / "data"),
        )
    core = _FakeCore()
    replies = []

    async def reply(dt, tid, msg):
        replies.append((dt, tid, msg))
        return "mid"

    calls = {"fallback": 0}

    async def fallback(ev):
        calls["fallback"] += 1

    coord = PilotCoordinator(runtime, reply_fn=reply, fallback_fn=fallback)
    return coord, core, replies, calls


# ---------------------------------------------------------------------------
# A - pilot gate passed + question -> LLM answers (one real reply, no 受理)
# ---------------------------------------------------------------------------
def test_a_pilot_handles_no_llm(tmp_path):
    coord, _, replies, calls = _make(tmp_path)
    import asyncio

    asyncio.run(coord.on_group_event(_event()))
    # P9.5: ANSWER -> pilot hands the question to the LLM (fallback) for the
    # real answer; the pilot sends no "受理确认" itself.
    assert len(replies) == 0
    assert calls["fallback"] == 1  # LLM answers for real


# ---------------------------------------------------------------------------
# B - non-whitelisted group -> SILENT (fail closed, never LLM)
# ---------------------------------------------------------------------------
def test_b_non_pilot_fallback(tmp_path):
    coord, _, replies, calls = _make(tmp_path)
    import asyncio

    asyncio.run(coord.on_group_event(_event(group_id="999999")))
    assert replies == []
    assert calls["fallback"] == 0  # whitelist fail-closed: no LLM bypass


# ---------------------------------------------------------------------------
# C - untriggered -> SILENT (PASSIVE, never LLM)
# ---------------------------------------------------------------------------
def test_c_untriggered_fallback(tmp_path):
    coord, _, replies, calls = _make(tmp_path)
    import asyncio

    asyncio.run(coord.on_group_event(_event(mentions_self=False)))
    assert replies == []
    assert calls["fallback"] == 0  # PASSIVE: no auto-reply / no LLM bypass


# ---------------------------------------------------------------------------
# D - kill switch disabled -> LLM fallback (pilot inert)
# ---------------------------------------------------------------------------
def test_d_kill_switch_fallback(tmp_path):
    class _Off:
        def check(self):
            raise RuntimeError("disabled")

    rt = PilotRuntime(
        config=LivePilotConfig(allowed_groups=("123456",), min_interval_sec=0.0),
        data_dir=str(tmp_path / "data"),
    )
    rt._kill_switch = _Off()
    coord, _, replies, calls = _make(tmp_path, runtime=rt)
    import asyncio

    asyncio.run(coord.on_group_event(_event()))
    assert replies == []  # pilot inert
    assert calls["fallback"] == 1  # LLM fallback preserved


# ---------------------------------------------------------------------------
# E - coordinator failure -> safe wrapper falls back to LLM
# ---------------------------------------------------------------------------
def test_e_safe_wrapper(tmp_path):
    import asyncio

    calls = {"fallback": 0}

    async def fallback(ev):
        calls["fallback"] += 1

    class _Broken:
        def handle(self, event):
            raise RuntimeError("boom")

    coord = PilotCoordinator(_Broken(), reply_fn=lambda *a: asyncio.sleep(0),
                             fallback_fn=fallback)
    asyncio.run(coord.on_group_event_safe(_event()))
    assert calls["fallback"] == 1


# ---------------------------------------------------------------------------
# F - stats
# ---------------------------------------------------------------------------
def test_f_stats(tmp_path):
    import asyncio

    coord, _, _, _ = _make(tmp_path)

    async def _drive():
        await coord.on_group_event(_event())  # gate passed -> LLM answer
        await coord.on_group_event(_event(group_id="999999"))  # silent denied
        return await coord.stats()

    stats = asyncio.run(_drive())
    assert stats["pilot_handled"] == 0
    assert stats["llm_fallback"] == 1
    assert stats["silent_denied"] == 1


# ---------------------------------------------------------------------------
# G - static boundary
# ---------------------------------------------------------------------------
def test_g_static_boundary():
    src = pathlib.Path("anima/pilot/wiring.py").read_text(encoding="utf-8")
    parts = src.split('"""')
    code_body = parts[2] if len(parts) >= 3 else src
    forbidden = [
        "CapabilityGateway", "approve_and_execute", "capability_mode",
        "gateway_mode", "webui", "napcat", "subprocess", "os.environ",
        "getenv", "requests", "httpx", "ssh", "paramiko",
    ]
    for needle in forbidden:
        assert needle not in code_body, f"forbidden token: {needle}"
    allowed_prefixes = ("anima.pilot.", "anima.runtime.", "anima.constitution.",
                        "anima.core.gateway")
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith(("from ", "import ")) and "anima." in stripped:
            target = stripped.split(" import ")[0].replace("from ", "").strip()
            if target.startswith("anima."):
                assert target.startswith(allowed_prefixes), f"import escapes: {target!r}"


# ---------------------------------------------------------------------------
# H - double-subscription gate: with a wired coordinator, the standalone LLM
#     handler must not process group events (double reply / whitelist bypass)
# ---------------------------------------------------------------------------
class _CoreWithCoord:
    pilot_coordinator = "wired"


class _CoreNoCoord:
    pilot_coordinator = None


def test_h_double_subscription_gate():
    group = {"topic": "event.message.group", "group_id": "123456"}
    private = {"topic": "event.message.private", "user_id": 1}
    # coordinator wired -> group traffic belongs to the coordinator
    assert should_llm_handle(group, _CoreWithCoord()) is False
    assert should_llm_handle(private, _CoreWithCoord()) is True
    # no coordinator -> LLM-only behaviour unchanged
    assert should_llm_handle(group, _CoreNoCoord()) is True
    assert should_llm_handle(private, _CoreNoCoord()) is True
    assert should_llm_handle(None, _CoreWithCoord()) is True
