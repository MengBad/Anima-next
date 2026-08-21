"""P9.1 canary wiring tests (RED -> GREEN).

Validates the canary-specific wiring semantics:
  * canary config load (pilot section -> CanaryWireSpec, default OFF)
  * fail-closed data-dir validation (only independent canary dirs)
  * allowed_groups whitelist propagation into LivePilotConfig
  * kill-switch semantics (DISABLED -> pilot fully inert)
  * NapCat OneBot11 event shape -> coordinator first, LLM fallback
  * static boundary: no System B / gateway / capability activation
"""

import asyncio
import pathlib

import pytest

# ---------------------------------------------------------------------------
# A - no pilot section -> default OFF (coordinator absent, LLM-only)
# ---------------------------------------------------------------------------


def test_a_no_pilot_section_off():
    from anima.pilot.canary import load_canary_pilot_config

    spec = load_canary_pilot_config({})
    assert spec is None  # fail closed: no config -> pilot absent
    spec2 = load_canary_pilot_config({"qq": {"enabled": True}})
    assert spec2 is None


def test_b_pilot_disabled_off():
    from anima.pilot.canary import load_canary_pilot_config

    spec = load_canary_pilot_config({"pilot": {"enabled": False}})
    assert spec is None


# ---------------------------------------------------------------------------
# C - pilot enabled + gate whitelist propagates
# ---------------------------------------------------------------------------


def test_c_enabled_with_whitelist():
    from anima.pilot.canary import load_canary_pilot_config

    spec = load_canary_pilot_config(
        {
            "pilot": {
                "enabled": True,
                "gate": {"allowed_groups": ["123456", "789012"], "max_groups": 2},
            }
        }
    )
    assert spec is not None
    assert spec.enabled is True
    assert spec.config.allowed_groups == ("123456", "789012")
    assert spec.config.max_groups == 2
    assert spec.config.mode == "REAL_USER_CONTROLLED"


def test_d_enabled_empty_whitelist_fail_closed():
    from anima.pilot.canary import load_canary_pilot_config

    spec = load_canary_pilot_config({"pilot": {"enabled": True}})
    assert spec is not None
    # empty whitelist -> LivePilotConfig fails closed: every group denied
    assert spec.config.allowed_groups == ()
    from anima.pilot.pilot_runtime import PilotRuntime

    rt = PilotRuntime(config=spec.config, data_dir=str(pathlib.Path("_tmp_canary") / "d"))
    # handle() expects the RAW OneBot11 event (it normalizes internally)
    ev = {"topic": "event.message.group", "user_id": 10001, "group_id": "123456",
          "message_id": "m-1", "text": "hi", "time": 1786450000,
          "mentions_self": True}
    r = rt.handle(ev)
    # empty whitelist -> gate denies -> pilot does NOT handle (handled=False,
    # LLM fallback); the deny reason proves fail-closed behaviour.
    assert r.handled is False
    assert r.denied_reason == "group_not_allowed"


# ---------------------------------------------------------------------------
# E - fail-closed data-dir validation (only independent canary dirs)
# ---------------------------------------------------------------------------


def test_e_data_dir_validation():
    from anima.pilot.canary import validate_canary_data_dir

    # system / production roots refused
    for bad in ("/", "/opt/anima", "/opt/anima-test", "/var/log", "C:\\"):
        with pytest.raises(ValueError):
            validate_canary_data_dir(bad)
    # an independent canary-style dir is allowed (may not exist yet)
    assert validate_canary_data_dir("/opt/production-canary/data") is None
    assert validate_canary_data_dir("/opt/anima-canary/data") is None


# ---------------------------------------------------------------------------
# F - kill switch: DISABLED -> coordinator fully inert
# ---------------------------------------------------------------------------


def test_f_kill_switch_disabled_inert(tmp_path):
    from anima.pilot.canary import build_canary_coordinator

    class _Core:
        gateway = None

        def __init__(self):
            self.pilot_coordinator = None
            self.bus = _Bus()

    class _Bus:
        def __init__(self):
            self.subs = []

        def subscribe(self, topic, fn):
            self.subs.append((topic, fn))

    core = _Core()

    class _OffSwitch:
        def check(self):
            raise RuntimeError("kill switch disabled")

    coord = build_canary_coordinator(
        spec=None,  # no pilot -> OFF -> no coordinator
        data_dir=str(tmp_path / "data"),
        core=core,
        kill_switch=_OffSwitch(),
    )
    assert coord is None  # absent = LLM-only, bot unchanged
    assert core.pilot_coordinator is None
    assert core.bus.subs == []  # no subscription registered


def test_f2_kill_switch_enabled_coordinator_registered(tmp_path):
    from anima.pilot.canary import load_canary_pilot_config, build_canary_coordinator

    class _Core:
        gateway = None

        def __init__(self):
            self.pilot_coordinator = None
            self.bus = _Bus()

    class _Bus:
        def __init__(self):
            self.subs = []

        def subscribe(self, topic, fn):
            self.subs.append((topic, fn))

    core = _Core()
    spec = load_canary_pilot_config(
        {"pilot": {"enabled": True, "gate": {"allowed_groups": ["123456"]}}}
    )
    coord = build_canary_coordinator(
        spec=spec, data_dir=str(tmp_path / "data"), core=core, kill_switch=None
    )
    assert coord is not None
    assert core.pilot_coordinator is coord
    topics = [t for t, _ in core.bus.subs]
    assert "event.message.group" in topics


# ---------------------------------------------------------------------------
# H - NapCat event shape flows through coordinator (handled vs fallback)
# ---------------------------------------------------------------------------


def test_h_napcat_event_shape(tmp_path):
    from anima.pilot.canary import load_canary_pilot_config, build_canary_coordinator

    calls = {"llm": 0, "reply": 0}

    class _Core:
        gateway = None

        def __init__(self):
            self.pilot_coordinator = None
            self.bus = _Bus()

    class _Bus:
        def __init__(self):
            self.subs = []

        def subscribe(self, topic, fn):
            self.subs.append((topic, fn))

    core = _Core()
    spec = load_canary_pilot_config(
        {"pilot": {"enabled": True, "gate": {"allowed_groups": ["123456"]}}}
    )

    async def _reply(dt, tid, msg):
        calls["reply"] += 1

    async def _fallback(ev):
        calls["llm"] += 1

    coord = build_canary_coordinator(
        spec=spec, data_dir=str(tmp_path / "data"), core=core, kill_switch=None,
        reply_fn=_reply, fallback_fn=_fallback,
    )
    assert coord is not None

    # a real OneBot11 group event shape (NapCat) for a whitelisted group
    ev = {
        "topic": "event.message.group",
        "user_id": 10001,
        "group_id": "123456",
        "message_id": "m-42",
        "text": "什么是量子计算",
        "time": 1786450000,
        "mentions_self": True,
        "raw": {"message_type": "group", "sender": {"nickname": "u1"}},
    }
    asyncio.run(coord.on_group_event_safe(ev))
    # whitelisted + @ + question -> gate passed, LLM answers (fallback)
    assert calls["llm"] == 1
    assert calls["reply"] == 0  # pilot sends no 受理确认 itself

    # a non-whitelisted group -> fail-closed SILENT (no pilot reply, and
    # NO LLM fallback either — the whitelist must not be bypassed)
    ev2 = dict(ev, group_id="999999", message_id="m-43")
    asyncio.run(coord.on_group_event_safe(ev2))
    assert calls["llm"] == 1  # unchanged: whitelist fail-closed
    assert calls["reply"] == 0


# ---------------------------------------------------------------------------
# K - static boundary: canary layer must not touch System B
# ---------------------------------------------------------------------------


def test_k_static_boundary():
    src = pathlib.Path("anima/pilot/canary.py").read_text(encoding="utf-8")
    parts = src.split('"""')
    body = parts[2] if len(parts) >= 3 else src
    forbidden = (
        "CapabilityGateway", "approve_and_execute", "capability_mode",
        "gateway_mode", "webui", "napcat", "subprocess", "os.environ",
        "getenv", "requests", "httpx", "ssh", "paramiko",
        "capability.governance", "capability.health", "anima.tools",
    )
    for needle in forbidden:
        assert needle not in body, f"forbidden token: {needle}"
    allowed_prefixes = ("anima.pilot.", "anima.runtime.", "anima.constitution.")
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith(("from ", "import ")) and "anima." in stripped:
            target = stripped.split(" import ")[0].replace("from ", "").strip()
            if target.startswith("anima."):
                assert target.startswith(allowed_prefixes), f"import escapes: {target!r}"
