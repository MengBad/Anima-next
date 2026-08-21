"""P7.10 — L3 Production Construction Boundary + L4 Kill Switch (RED contract).

L3 (build_pilot_bridge): config-driven controlled factory; default DISABLED;
explicit config required to enable; legacy path unchanged; no env bypass.

L4 (BridgeKillSwitch): ENABLED/DISABLED, default DISABLED, fail-closed (check
raises); toggle never mutates identity/execution_key.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from anima.core.persistence import PersistenceProvider
from anima.runtime.pilot_control import BridgeDisabledError, BridgeKillSwitch, build_pilot_bridge


# ---------------------------------------------------------------------------
# L3-H default disabled (no orchestrator, no bridge)
# ---------------------------------------------------------------------------
def test_l3h_default_disabled(tmp_path):
    switch, orch = build_pilot_bridge({}, data_dir=str(tmp_path))
    assert switch.state == "DISABLED"
    assert orch is None
    # explicit empty config section is also disabled
    switch2, orch2 = build_pilot_bridge({"execution_bridge": {}}, data_dir=str(tmp_path))
    assert switch2.state == "DISABLED"
    assert orch2 is None


# ---------------------------------------------------------------------------
# L3-I enabled requires explicit config (execution_bridge.enabled=true)
# ---------------------------------------------------------------------------
def test_l3i_explicit_config(tmp_path):
    switch, orch = build_pilot_bridge(
        {"execution_bridge": {"enabled": True}}, data_dir=str(tmp_path)
    )
    assert switch.state == "ENABLED"
    assert orch is not None
    # env vars never bypass the config
    import os

    os.environ["ANIMA_EXECUTION_BRIDGE_ENABLED"] = "true"
    try:
        switch2, orch2 = build_pilot_bridge({}, data_dir=str(tmp_path))
        assert switch2.state == "DISABLED"
        assert orch2 is None
    finally:
        del os.environ["ANIMA_EXECUTION_BRIDGE_ENABLED"]


# ---------------------------------------------------------------------------
# L3-J legacy path unchanged (bridge absent does not alter run_plan behavior)
# ---------------------------------------------------------------------------
def test_l3j_legacy_unchanged(tmp_path):
    from anima.constitution.execution import ExecutionRuntime
    from anima.constitution.planning_interface import PlanningInterface
    from anima.runtime.orchestrator import PlanOrchestrator

    switch, orch = build_pilot_bridge({}, data_dir=str(tmp_path))
    assert orch is None
    # a default PlanOrchestrator (no executor) still uses the legacy path
    legacy = PlanOrchestrator(execution=ExecutionRuntime())
    assert legacy._executor is None  # legacy fallback intact


# ---------------------------------------------------------------------------
# L4-K disabled rejects execution (fail closed, no provider call)
# ---------------------------------------------------------------------------
def test_l4k_disabled_rejects():
    switch = BridgeKillSwitch(enabled=False)
    assert switch.state == "DISABLED"
    with pytest.raises(BridgeDisabledError):
        switch.check()
    with pytest.raises(BridgeDisabledError):
        switch.check()  # fail closed repeatedly


# ---------------------------------------------------------------------------
# L4-L enabled permits controlled test execution
# ---------------------------------------------------------------------------
def test_l4l_enabled_permits(tmp_path):
    switch, orch = build_pilot_bridge(
        {"execution_bridge": {"enabled": True}}, data_dir=str(tmp_path)
    )
    assert switch.state == "ENABLED"
    switch.check()  # no raise
    assert orch is not None


# ---------------------------------------------------------------------------
# L4-M toggle does not mutate identity / execution_key
# ---------------------------------------------------------------------------
def test_l4m_toggle_no_identity_mutation(tmp_path):
    switch = BridgeKillSwitch(enabled=False)
    switch.enable()
    assert switch.state == "ENABLED"
    switch.disable()
    assert switch.state == "DISABLED"
    # identity derivation stays stable across toggles: the same key still maps
    # to the same provider token after enable/disable cycles.
    from anima.runtime.providers.sandbox_write_provider import SandboxWriteProvider

    provider = SandboxWriteProvider(str(tmp_path))
    key = ("run-1", "t1", 0)
    marker_a = provider._token_path(key)
    switch.enable()
    switch.disable()
    provider2 = SandboxWriteProvider(str(tmp_path))
    marker_b = provider2._token_path(key)
    assert marker_a == marker_b  # deterministic identity, toggle-immune


# ---------------------------------------------------------------------------
# Static boundary (pilot_control stays in F5 domain, no System B / gateway)
# ---------------------------------------------------------------------------
def test_static_boundary():
    src = pathlib.Path("anima/runtime/pilot_control.py").read_text(encoding="utf-8")
    parts = src.split('"""')
    code_body = parts[2] if len(parts) >= 3 else src
    forbidden = [
        "CapabilityGateway", "gateway", "tools", "approve_and_execute",
        "capability_mode", "webui", "napcat", r"\brequests\b", "httpx",
        "subprocess", r"\bssh\b", "paramiko", "os.environ",
    ]
    for needle in forbidden:
        assert not re.search(needle, code_body), f"forbidden token: {needle}"
    allowed_prefixes = ("anima.runtime.", "anima.constitution.", "anima.core.persistence")
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith(("from ", "import ")):
            target = stripped.split(" import ")[0].replace("from ", "").strip()
            if target.startswith("anima."):
                assert target.startswith(allowed_prefixes), f"import escapes: {target!r}"
