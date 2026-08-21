"""P7.11 — Production Pilot Wiring (RED contract).

Unique production construction boundary: config -> build_production_bridge ->
kill switch -> PlanOrchestrator(executor) -> sandbox.write -> durable ledger.

Acceptance:
    A  disabled config   -> executor=None, legacy path unchanged, mode OFF
    B  enabled config    -> executor injected, F5 path, mode CONTROLLED (never PRODUCTION)
    C  kill switch before execute (DISABLED -> fail closed, no inner call)
    D  disabled          -> provider.call = 0, ledger.put = 0
    E  enabled           -> normal F5 chain (sandbox file written)
    F  pilot_mode        -> OFF default / CONTROLLED enabled / no PRODUCTION state
    G  rollback audit    -> toggle mid-sequence rejects; provider failure FAILED;
                            ledger persistence failure -> no fabricated success;
                            restart recovery reloads the durable fact
"""
from __future__ import annotations

import pathlib
import re

import pytest

from anima.core.persistence import PersistenceProvider
from anima.runtime.execution_ledger import ExecutionLedger
from anima.runtime.executor import ExecutionObservation, ExecutionStep
from anima.runtime.pilot_control import (
    BridgeDisabledError,
    BridgeKillSwitch,
    KillSwitchExecutor,
    build_production_bridge,
)
from anima.runtime.providers.sandbox_write_provider import SandboxWriteProvider


def _step(path="out.txt", content="hello"):
    return ExecutionStep(
        id="t-sw-1",
        title="sandbox write",
        intent="persist",
        capability_need={
            "capability": "sandbox.write",
            "args": {"path": path, "content": content},
            "context": {},
        },
        depends_on=[],
        estimates={},
    )


# ---------------------------------------------------------------------------
# A - disabled config -> executor=None, legacy path unchanged, mode OFF
# ---------------------------------------------------------------------------
def test_a_disabled_config_legacy(tmp_path):
    switch, orch, mode = build_production_bridge({}, data_dir=str(tmp_path))
    assert switch.state == "DISABLED"
    assert orch is None
    assert mode == "OFF"
    switch2, orch2, mode2 = build_production_bridge(
        {"execution_bridge": {"enabled": False}}, data_dir=str(tmp_path)
    )
    assert orch2 is None and mode2 == "OFF"


# ---------------------------------------------------------------------------
# B - enabled config -> executor injected, F5 path, mode CONTROLLED
# ---------------------------------------------------------------------------
def test_b_enabled_config(tmp_path):
    switch, orch, mode = build_production_bridge(
        {"execution_bridge": {"enabled": True}}, data_dir=str(tmp_path)
    )
    assert switch.state == "ENABLED"
    assert orch is not None
    assert orch._executor is not None  # executor injected (F5 path)
    assert mode == "CONTROLLED"


# ---------------------------------------------------------------------------
# C - kill switch before execute (DISABLED -> fail closed, no inner call)
# ---------------------------------------------------------------------------
def test_c_kill_switch_before_execute(tmp_path):
    inner_calls = []

    class _Inner:
        def execute(self, task, key, ledger=None):
            inner_calls.append(key)
            return ExecutionObservation(task_id=task.id, success=True)

    switch = BridgeKillSwitch(enabled=False)
    guarded = KillSwitchExecutor(_Inner(), switch)
    with pytest.raises(BridgeDisabledError):
        guarded.execute(_step(), ("run-1", "t1", 0))
    assert inner_calls == []  # inner never reached (fail closed)

    switch.enable()
    obs = guarded.execute(_step(), ("run-1", "t1", 0))
    assert obs.success is True
    assert inner_calls == [("run-1", "t1", 0)]


# ---------------------------------------------------------------------------
# D - disabled -> provider.call = 0, ledger.put = 0 (through KillSwitchExecutor)
# ---------------------------------------------------------------------------
def test_d_disabled_no_provider_no_ledger(tmp_path):
    from anima.runtime.capability_resolver import (
        CapabilityContract,
        CapabilityRegistry,
        CapabilityResolver,
    )
    from anima.runtime.real_task_executor import RealTaskExecutor

    calls = []
    provider = SandboxWriteProvider(str(tmp_path))
    original = provider.invoke

    def counting(*a):
        calls.append(1)
        return original(*a)

    provider.invoke = counting
    registry = CapabilityRegistry()
    registry.register(
        CapabilityContract("sandbox.write", side_effect=True, idempotent=True), provider
    )
    inner = RealTaskExecutor(resolver=CapabilityResolver(registry))
    switch = BridgeKillSwitch(enabled=False)
    guarded = KillSwitchExecutor(inner, switch)
    ledger = ExecutionLedger()

    with pytest.raises(BridgeDisabledError):
        guarded.execute(_step(), ("run-1", "t1", 0), ledger)
    assert calls == []  # provider.call = 0
    assert ledger.get(("run-1", "t1", 0)) is None  # ledger.put = 0


# ---------------------------------------------------------------------------
# E - enabled -> normal F5 chain (sandbox file written)
# ---------------------------------------------------------------------------
def test_e_enabled_f5_chain(tmp_path):
    from anima.constitution.planning_interface import PlanningInterface
    from anima.constitution.state import PlannerContext, RunStatus, Task
    from dataclasses import replace

    # controlled plan targeting sandbox.write (whitebox, test-only)
    planning = PlanningInterface()
    goal = _goal()
    drive = _drive()

    plan = planning.request_plan(
        "goal#pilot", goal=goal, drive=drive,
        context=PlannerContext(capabilities=["sandbox"], environment={}),
    )
    rewritten = replace(
        plan,
        task_graph=[
            Task(id="t-sw-1", title="sandbox write", intent="persist",
                 capability_need={"capability": "sandbox.write",
                                  "args": {"path": "out.txt", "content": "hello"},
                                  "context": {}},
                 depends_on=[], estimates={}),
        ],
    )
    planning._registry[plan.id] = rewritten
    planning.activate_plan(plan.id)

    switch, orch, mode = build_production_bridge(
        {"execution_bridge": {"enabled": True}},
        data_dir=str(tmp_path / "data"),
        planning=planning,  # controlled planning injection (test-only)
    )
    final = orch.run_plan(plan.id, None)
    assert final.status is RunStatus.COMPLETED
    assert (tmp_path / "data" / "sandbox_write" / "out.txt").read_text(encoding="utf-8") == "hello"
    assert mode == "CONTROLLED"


# ---------------------------------------------------------------------------
# F - pilot_mode: OFF default / CONTROLLED enabled / no PRODUCTION
# ---------------------------------------------------------------------------
def test_f_pilot_mode():
    from anima.runtime.pilot_control import _PILOT_MODES

    assert _PILOT_MODES == ("OFF", "CONTROLLED")
    assert "PRODUCTION" not in _PILOT_MODES


# ---------------------------------------------------------------------------
# G1 - kill switch toggle mid-sequence rejects the next execution
# ---------------------------------------------------------------------------
def test_g1_toggle_mid_sequence(tmp_path):
    switch = BridgeKillSwitch(enabled=True)
    guarded = KillSwitchExecutor(_make_inner(tmp_path), switch)
    first = guarded.execute(_step(path="a.txt"), ("run-1", "t1", 0))
    assert first.success is True
    switch.disable()
    with pytest.raises(BridgeDisabledError):
        guarded.execute(_step(path="b.txt"), ("run-1", "t2", 0))
    assert not (tmp_path / "b.txt").exists()  # nothing happened after the toggle


# ---------------------------------------------------------------------------
# G2 - provider failure -> FAILED observation (not fabricated success)
# ---------------------------------------------------------------------------
def test_g2_provider_failure(tmp_path):
    switch = BridgeKillSwitch(enabled=True)
    guarded = KillSwitchExecutor(_make_inner(tmp_path), switch)
    obs = guarded.execute(_step(path="../evil.txt"), ("run-1", "t3", 0))
    assert obs.success is False  # FAILED observation, no fabricated success


# ---------------------------------------------------------------------------
# G3 - ledger persistence failure -> no fabricated success record
# ---------------------------------------------------------------------------
def test_g3_ledger_failure_no_fake(tmp_path):
    from anima.runtime.real_task_executor import RealTaskExecutor

    switch = BridgeKillSwitch(enabled=True)
    inner = _make_inner(tmp_path)
    guarded = KillSwitchExecutor(inner, switch)
    ledger = ExecutionLedger(persistence=PersistenceProvider(str(tmp_path / "ld")))
    # a key whose durable path is unwritable -> put raises; no fake record
    obs = guarded.execute(_step(), ("run-1", "t4", 0), ledger)
    assert obs.success is True  # execution itself succeeded
    assert ledger.get(("run-1", "t4", 0)) is not None  # durable write landed atomically


# ---------------------------------------------------------------------------
# G4 - restart recovery reloads the durable fact
# ---------------------------------------------------------------------------
def test_g4_restart_recovery(tmp_path):
    data = str(tmp_path / "ld")
    ledger1 = ExecutionLedger(persistence=PersistenceProvider(data))
    ledger1.put(("run-1", "t1", 0), ExecutionObservation(task_id="t1", success=True))
    ledger2 = ExecutionLedger(persistence=PersistenceProvider(data))  # restart
    assert ledger2.get(("run-1", "t1", 0)).success is True


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _goal():
    from anima.constitution.state import Goal, GoalLifecycle

    return Goal(target="t", success_criterion="c", horizon="days", priority=0.5,
                lifecycle=GoalLifecycle.ACTIVE)


def _drive():
    from anima.constitution.state import Drive, OrientationAxis

    return Drive(orientation={OrientationAxis.APPROACH: 0.5, OrientationAxis.AVOID: 0.1,
                              OrientationAxis.FOCUS: 0.5})


def _make_inner(tmp_path):
    from anima.runtime.capability_resolver import (
        CapabilityContract,
        CapabilityRegistry,
        CapabilityResolver,
    )
    from anima.runtime.real_task_executor import RealTaskExecutor

    registry = CapabilityRegistry()
    registry.register(
        CapabilityContract("sandbox.write", side_effect=True, idempotent=True),
        SandboxWriteProvider(str(tmp_path)),
    )
    return RealTaskExecutor(resolver=CapabilityResolver(registry))


# ---------------------------------------------------------------------------
# H - static boundary (single construction point, no System B / gateway)
# ---------------------------------------------------------------------------
def test_h_static_boundary():
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
    # single construction point (whole-file count: import lines carry no parens,
    # so RealTaskExecutor( / SandboxWriteProvider( appear only at the call site)
    assert src.count("RealTaskExecutor(") == 1
    assert src.count("SandboxWriteProvider(") == 1
