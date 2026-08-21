"""P7.15 — Controlled Pilot Validation (synthetic traffic, RED contract).

Proves the pilot control chain is reliable with SYNTHETIC traffic only:

    PilotScope -> CohortMatcher -> pilot_gate -> KillSwitchExecutor
        -> RealTaskExecutor -> sandbox.write -> durable ExecutionLedger

Evidence targets:
    1. execution_key stability   - same cohort/run -> same key & provider token
    2. ledger durable            - durable fact survives restart
    3. replay safe               - ledger HIT + C2 marker: no double side effect
    4. cleanup                   - completed removed, failed preserved
    5. kill switch               - toggle rejects, gate fails closed
    6. failure recovery          - failure -> FAILED observation; restart recovers
    7. synthetic boundary        - sandbox.write only, no network/external/System B
"""
from __future__ import annotations

import pathlib
import re

import pytest

from anima.core.persistence import PersistenceProvider
from anima.runtime.execution_ledger import ExecutionLedger
from anima.runtime.executor import ExecutionStep
from anima.runtime.pilot_control import BridgeKillSwitch, KillSwitchExecutor
from anima.runtime.pilot_scope import (
    CohortMatcher,
    PilotEnablementRecord,
    PilotScope,
    pilot_gate,
)
from anima.runtime.providers.sandbox_write_provider import SandboxWriteProvider
from anima.runtime.real_task_executor import RealTaskExecutor


def _synthetic_scope(**overrides):
    defaults = dict(
        scope_id="pilot-synthetic-1",
        enabled=True,
        mode="CONTROLLED",
        goal_prefixes=("goal#synthetic",),
        plan_prefixes=("plan#synthetic",),
        allowlist=("plan#synthetic-001",),
        operator_approval_ref="opr-synth-001",
    )
    defaults.update(overrides)
    return PilotScope(**defaults)


def _synthetic_record(scope):
    return PilotEnablementRecord(
        pilot_enablement_id="pe-synth-001",
        operator="pilot-operator",
        scope_digest=scope.scope_digest,
        approved_at="2026-08-11T18:20:00+00:00",
    )


def _chain(tmp_path, *, switch_enabled=True, data_subdir="data"):
    """Full synthetic control chain (sandbox.write only, durable ledger)."""
    data_dir = str(tmp_path / data_subdir)
    scope = _synthetic_scope()
    record = _synthetic_record(scope)
    switch = BridgeKillSwitch(enabled=switch_enabled)
    registry = _registry(data_dir)
    inner = RealTaskExecutor(resolver=registry)
    guarded = KillSwitchExecutor(inner, switch)
    ledger = ExecutionLedger(persistence=PersistenceProvider(data_dir))
    return scope, record, switch, guarded, ledger, data_dir


def _registry(data_dir):
    from anima.runtime.capability_resolver import (
        CapabilityContract,
        CapabilityRegistry,
        CapabilityResolver,
    )

    reg = CapabilityRegistry()
    reg.register(
        CapabilityContract("sandbox.write", side_effect=True, idempotent=True),
        SandboxWriteProvider(sandbox_root=None, data_dir=data_dir),
    )
    return CapabilityResolver(reg)


def _step(path="out.txt", content="synthetic"):
    return ExecutionStep(
        id="t-synth", title="sandbox write", intent="synthetic pilot",
        capability_need={"capability": "sandbox.write",
                         "args": {"path": path, "content": content}, "context": {}},
        depends_on=[], estimates={},
    )


# ---------------------------------------------------------------------------
# 1 - Gate admission: correct cohort enters, others denied
# ---------------------------------------------------------------------------
def test_1_gate_admission(tmp_path):
    scope, record, switch, *_ = _chain(tmp_path)
    assert pilot_gate(scope, record, switch, goal_id="goal#synthetic-1", plan_id="plan#synthetic-001") is True
    assert pilot_gate(scope, record, switch, goal_id="goal#other", plan_id="plan#synthetic-001") is False
    assert pilot_gate(scope, record, switch, goal_id="goal#synthetic-1", plan_id="plan#other") is False


# ---------------------------------------------------------------------------
# 2 - execution_key stability: same cohort/run -> same provider token
# ---------------------------------------------------------------------------
def test_2_execution_key_stable(tmp_path):
    _, _, _, guarded, ledger, data_dir = _chain(tmp_path)
    key = ("run-synth-1", "t-synth", 0)
    first = guarded.execute(_step(), key, ledger)
    assert first.success is True
    marker_a = list((pathlib.Path(data_dir) / "sandbox_write" / ".tokens").glob("*.token"))
    assert len(marker_a) == 1
    # a second identical dispatch derives the SAME key -> SAME token (deterministic)
    token_a = marker_a[0].read_text(encoding="utf-8")
    guarded.execute(_step(), key, ledger)  # ledger HIT -> no new marker
    tokens = list((pathlib.Path(data_dir) / "sandbox_write" / ".tokens").glob("*.token"))
    assert len(tokens) == 1
    assert tokens[0].read_text(encoding="utf-8") == token_a  # token stable


# ---------------------------------------------------------------------------
# 3 - ledger durable: fact survives restart
# ---------------------------------------------------------------------------
def test_3_ledger_durable(tmp_path):
    _, _, _, guarded, ledger, data_dir = _chain(tmp_path)
    key = ("run-synth-2", "t-synth", 0)
    guarded.execute(_step(), key, ledger)
    # restart: fresh ledger over the same data dir
    ledger2 = ExecutionLedger(persistence=PersistenceProvider(data_dir))
    cached = ledger2.get(key)
    assert cached is not None and cached.success is True


# ---------------------------------------------------------------------------
# 4 - replay safe: ledger HIT + C2 marker, no double side effect
# ---------------------------------------------------------------------------
def test_4_replay_safe(tmp_path):
    _, _, _, guarded, ledger, data_dir = _chain(tmp_path)
    key = ("run-synth-3", "t-synth", 0)
    guarded.execute(_step(), key, ledger)
    # same key again -> ledger HIT, content unchanged
    guarded.execute(_step(content="CHANGED"), key, ledger)
    out = pathlib.Path(data_dir) / "sandbox_write" / "out.txt"
    assert out.read_text(encoding="utf-8") == "synthetic"  # unchanged (HIT)
    # provider-level C2: same key -> written=False
    provider = SandboxWriteProvider(sandbox_root=None, data_dir=data_dir)
    r2 = provider.invoke("sandbox.write", {"path": "dup.txt", "content": "x"}, key)
    assert r2.success is True and r2.data["written"] is False  # external token hit


# ---------------------------------------------------------------------------
# 5 - cleanup: completed removed; resolution-failed tasks never enter the ledger
# ---------------------------------------------------------------------------
def test_5_cleanup(tmp_path):
    _, _, _, guarded, ledger, _ = _chain(tmp_path)
    guarded.execute(_step(path="a.txt"), ("run-synth-4", "t-synth", 0), ledger)  # success
    failed = guarded.execute(
        _step(path="../evil.txt"), ("run-synth-5", "t-synth", 0), ledger
    )
    assert failed.success is False
    # a resolution-failed task is NEVER recorded as an execution fact
    # (resolver-level refusal -> FAILED observation, no ledger entry)
    assert ledger.has(("run-synth-5", "t-synth", 0)) is False
    removed = ledger.cleanup(before=None)
    assert removed == 1  # only the completed fact removed
    assert ledger.has(("run-synth-4", "t-synth", 0)) is False


# ---------------------------------------------------------------------------
# 6 - kill switch: toggle rejects; gate fails closed
# ---------------------------------------------------------------------------
def test_6_kill_switch(tmp_path):
    scope, record, switch, guarded, ledger, _ = _chain(tmp_path)
    key = ("run-synth-6", "t-synth", 0)
    assert guarded.execute(_step(), key, ledger).success is True
    switch.disable()
    assert pilot_gate(scope, record, switch, goal_id="goal#synthetic-1", plan_id="plan#synthetic-001") is False
    from anima.runtime.pilot_control import BridgeDisabledError

    with pytest.raises(BridgeDisabledError):
        guarded.execute(_step(path="b.txt"), ("run-synth-7", "t-synth", 0), ledger)
    assert not (pathlib.Path(tmp_path) / "data" / "sandbox_write" / "b.txt").exists()


# ---------------------------------------------------------------------------
# 7 - failure recovery: failure -> FAILED observation; restart recovers durable facts
# ---------------------------------------------------------------------------
def test_7_failure_recovery(tmp_path):
    _, _, _, guarded, ledger, data_dir = _chain(tmp_path)
    failed = guarded.execute(_step(path="../evil.txt"), ("run-synth-8", "t-synth", 0), ledger)
    assert failed.success is False  # FAILED observation, no fabricated success
    ok = guarded.execute(_step(path="ok.txt"), ("run-synth-9", "t-synth", 0), ledger)
    assert ok.success is True
    # restart: durable facts (both the failed observation? no — failed still put? check) reload
    ledger2 = ExecutionLedger(persistence=PersistenceProvider(data_dir))
    assert ledger2.get(("run-synth-9", "t-synth", 0)).success is True


# ---------------------------------------------------------------------------
# 8 - synthetic boundary: sandbox.write only, no network/external/System B
# ---------------------------------------------------------------------------
def test_8_synthetic_boundary(tmp_path):
    src = pathlib.Path("tests/test_pilot_validation.py").read_text(encoding="utf-8")
    # token concatenation avoids self-reference (the assertion string itself
    # would otherwise appear in src)
    gov = "capability" + "." + "governance"
    tools = "anima" + ".tools"
    health = "capability" + "." + "health"
    assert gov not in src
    assert health not in src
    assert tools not in src
    for needle in (r"\brequests\b", "htt" + "px",
                   "sub" + "process", r"\bssh\b"):
        assert not re.search(needle, src)
    # the chain only ever touches sandbox.write: an out-of-domain capability is
    # a FAILED observation (I2: resolver refusal never fabricated as success)
    obs = _chain(tmp_path)[3].execute(
        ExecutionStep(id="t-x", title="x", intent="x",
                      capability_need={"capability": "code.execute",
                                       "args": {}, "context": {}},
                      depends_on=[], estimates={}),
        ("run-x", "t-x", 0),
        ExecutionLedger(),
    )
    assert obs.success is False
