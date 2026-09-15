"""P5.2 Runtime Consumption Bridge / Read-only Adapter - authoritative contract.

This test module is the RED->GREEN contract for P5.2.  It proves that an
*observed* runtime state (produced read-only by P5.1) can be consumed by a
second, independent read-only layer without ever:

  * mutating the observed state or its governance evidence,
  * activating, executing, applying, or dispatching anything,
  * reaching production or any VPS,
  * widening the synthetic-only / offline-only scope.

The consumption layer is the symmetric counterpart of P5.1's observation
layer.  P5.1 turns governance evidence into an immutable, offline
``ObservedRuntimeState``; P5.2 consumes that state into an immutable,
digest-bound ``ConsumedRuntimeState`` and verifies the consumption stayed
inside the read-only boundary.

All numbers in the P5.2 Final Report come from *running* this file - never
from memory.  Run::

    pytest tests/test_capability_runtime_consumption.py -q
"""

from __future__ import annotations

import hashlib
import json
import inspect
import os
from types import MappingProxyType

import pytest

from anima.capability_runtime_observation import (
    NOT_ACTIVATED,
    NOT_APPLICABLE,
    NOT_EXECUTED,
    OFFLINE_SOURCE_PREFIXES,
    OBSERVATION_SCOPE,
    ObservedRuntimeState,
)
from anima.capability_runtime_consumption import (
    ConsumedRuntimeState,
    RuntimeConsumptionAdapter,
    RuntimeConsumptionError,
    RuntimeConsumptionReport,
    RuntimeConsumptionVerification,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _hex64(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _digest(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _make_state(
    subject_id: str = "subject-x",
    runtime_activation: str = NOT_ACTIVATED,
    execution_status: str = NOT_EXECUTED,
    observed_at: str = "2026-08-10T00:00:00Z",
    source: str = "synthetic://p5.2/subject-x",
):
    return ObservedRuntimeState(
        subject_id=subject_id,
        runtime_activation=runtime_activation,
        execution_status=execution_status,
        observed_at=observed_at,
        source=source,
    )


def _consumed_payload(c: ConsumedRuntimeState) -> dict:
    return {
        "consumption_id": c.consumption_id,
        "subject_id": c.subject_id,
        "runtime_activation": c.runtime_activation,
        "execution_status": c.execution_status,
        "observed_at": c.observed_at,
        "source": c.source,
        "consumed_at": c.consumed_at,
        "observation_scope": c.observation_scope,
    }


def _craft(adapter: RuntimeConsumptionAdapter, state: ObservedRuntimeState, **overrides):
    """Bypass __post_init__ guards to build a tampered ConsumedRuntimeState.

    The frozen dataclass __post_init__ already enforces digest / scope / source
    on the happy path.  To prove verify() fails *closed* on a bad instance we
    construct one without running the post-init validators.
    """
    base = adapter.consume(state, consumption_id="c1", consumed_at="t1")
    inst = object.__new__(ConsumedRuntimeState)
    for field in ConsumedRuntimeState.__slots__:
        object.__setattr__(inst, field, getattr(base, field))
    for key, value in overrides.items():
        object.__setattr__(inst, key, value)
    return inst


# ---------------------------------------------------------------------------
# consume
# ---------------------------------------------------------------------------
def test_consume_requires_observed_runtime_state():
    adapter = RuntimeConsumptionAdapter()
    with pytest.raises(RuntimeConsumptionError):
        adapter.consume(object(), consumption_id="c1", consumed_at="t1")


def test_consume_builds_frozen_digest_bound_state():
    adapter = RuntimeConsumptionAdapter()
    state = _make_state()
    consumed = adapter.consume(state, consumption_id="c1", consumed_at="t1")

    assert isinstance(consumed, ConsumedRuntimeState)
    assert consumed.consumption_id == "c1"
    assert consumed.subject_id == state.subject_id
    assert consumed.runtime_activation == state.runtime_activation
    assert consumed.execution_status == state.execution_status
    assert consumed.observed_at == state.observed_at
    assert consumed.source == state.source
    assert consumed.consumed_at == "t1"
    assert consumed.observation_scope == OBSERVATION_SCOPE

    # digest is a SHA-256 hex value bound to its own fields
    assert len(consumed.consumption_digest) == 64
    assert consumed.consumption_digest == _digest(_consumed_payload(consumed))

    # frozen: cannot be mutated after construction
    with pytest.raises((AttributeError, TypeError)):
        consumed.subject_id = "mutated"


def test_consume_preserves_offline_source():
    adapter = RuntimeConsumptionAdapter()
    state = _make_state(source="fixture://p5.2/subject-y")
    consumed = adapter.consume(state, consumption_id="c1", consumed_at="t1")
    assert consumed.source.startswith(OFFLINE_SOURCE_PREFIXES)


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------
def test_inspect_returns_immutable_mapping():
    adapter = RuntimeConsumptionAdapter()
    consumed = adapter.consume(_make_state(), consumption_id="c1", consumed_at="t1")
    view = adapter.inspect(consumed)

    assert isinstance(view, MappingProxyType)
    assert view["subject_id"] == consumed.subject_id
    assert view["consumption_digest"] == consumed.consumption_digest
    # the view is a read-only projection
    with pytest.raises(TypeError):
        view["subject_id"] = "x"


def test_inspect_rejects_wrong_type():
    adapter = RuntimeConsumptionAdapter()
    with pytest.raises(RuntimeConsumptionError):
        adapter.inspect(object())


# ---------------------------------------------------------------------------
# verify (fails closed)
# ---------------------------------------------------------------------------
def test_verify_valid_consumption():
    adapter = RuntimeConsumptionAdapter()
    state = _make_state()
    consumed = adapter.consume(state, consumption_id="c1", consumed_at="t1")
    ver = adapter.verify(consumed, state)

    assert isinstance(ver, RuntimeConsumptionVerification)
    assert ver.valid is True
    assert ver.digest_intact is True
    assert ver.scope_intact is True
    assert ver.no_mutation is True
    assert ver.no_execution is True
    assert ver.source_offline is True
    assert ver.errors == ()


def test_verify_fails_closed_on_digest_tamper():
    adapter = RuntimeConsumptionAdapter()
    state = _make_state()
    consumed = adapter.consume(state, consumption_id="c1", consumed_at="t1")
    tampered = _craft(adapter, state, consumption_digest="0" * 64)

    ver = adapter.verify(tampered, state)
    assert ver.digest_intact is False
    assert ver.valid is False
    assert any("digest" in e for e in ver.errors)


def test_verify_fails_closed_on_scope_widened():
    adapter = RuntimeConsumptionAdapter()
    state = _make_state()
    tampered = _craft(adapter, state, observation_scope="production")

    ver = adapter.verify(tampered, state)
    assert ver.scope_intact is False
    assert ver.valid is False


def test_verify_detects_mutation_against_original():
    adapter = RuntimeConsumptionAdapter()
    state_a = _make_state(subject_id="A", execution_status=NOT_EXECUTED)
    # the "original" observed state was actually different at consume time
    state_b = _make_state(subject_id="A", execution_status="running")
    consumed = adapter.consume(state_a, consumption_id="c1", consumed_at="t1")

    ver = adapter.verify(consumed, state_b)
    assert ver.no_mutation is False
    assert ver.valid is False


def test_verify_detects_execution_claim():
    adapter = RuntimeConsumptionAdapter()
    # an observed state that claims it ran is still consumed faithfully, but
    # verify must flag that consumption is not allowed to imply execution
    state = _make_state(
        subject_id="A", runtime_activation="active", execution_status="executed"
    )
    consumed = adapter.consume(state, consumption_id="c1", consumed_at="t1")

    ver = adapter.verify(consumed, state)
    assert ver.no_execution is False
    assert ver.valid is False


def test_verify_rejects_wrong_type():
    adapter = RuntimeConsumptionAdapter()
    with pytest.raises(RuntimeConsumptionError):
        adapter.verify(object(), _make_state())


# ---------------------------------------------------------------------------
# reconcile (report only, never fix)
# ---------------------------------------------------------------------------
def test_reconcile_reports_no_contradiction():
    adapter = RuntimeConsumptionAdapter()
    state = _make_state()
    consumed = adapter.consume(state, consumption_id="c1", consumed_at="t1")

    report = adapter.reconcile(consumed, state)
    assert isinstance(report, RuntimeConsumptionReport)
    assert report.reconciled is True
    assert report.contradictions == ()
    assert len(report.report_digest) == 64
    # original state is untouched (it is frozen, but assert explicitly)
    assert state.runtime_activation == NOT_ACTIVATED


def test_reconcile_reports_contradiction_without_fixing():
    adapter = RuntimeConsumptionAdapter()
    state_a = _make_state(subject_id="A", runtime_activation=NOT_ACTIVATED)
    state_b = _make_state(subject_id="A", runtime_activation="active")
    consumed = adapter.consume(state_a, consumption_id="c1", consumed_at="t1")

    report = adapter.reconcile(consumed, state_b)
    assert report.reconciled is False
    assert len(report.contradictions) >= 1
    # the contradiction is reported, not repaired: state_b still says "active"
    assert state_b.runtime_activation == "active"
    assert consumed.runtime_activation == NOT_ACTIVATED


# ---------------------------------------------------------------------------
# static boundary scan (P4.4 forbidden-token + frozen-subsystem import set)
# ---------------------------------------------------------------------------
_FORBIDDEN_TOKENS = (
    "subprocess",
    "ssh",
    "systemctl",
    "docker",
    "requests",
    "httpx",
    "paramiko",
    "fabric",
    "socket",
    ".activate(",
    ".execute(",
    ".apply(",
    ".mutate(",
    "active=True",
)

_FORBIDDEN_SUBSYSTEMS = (
    "anima.lifecycle",
    "anima.constitution",
    "anima.foundation",
    "anima.memory",
    "anima.identity",
    "anima.runtime",
    "anima.capability_contract",
)


def _module_source() -> str:
    import anima.capability_runtime_consumption as mod

    path = inspect.getsourcefile(mod)
    assert path is not None
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def test_static_boundary_scan_no_forbidden_tokens():
    source = _module_source()
    hits = [tok for tok in _FORBIDDEN_TOKENS if tok in source]
    assert hits == [], f"forbidden tokens present: {hits}"


def test_static_boundary_scan_no_frozen_subsystem_imports():
    source = _module_source()
    hits = [sub for sub in _FORBIDDEN_SUBSYSTEMS if sub in source]
    assert hits == [], f"forbidden subsystem imports present: {hits}"


def test_capability_runtime_consumption_module_path_exists():
    # sanity: the module under test is importable from the expected path
    import anima.capability_runtime_consumption as mod

    path = inspect.getsourcefile(mod)
    assert path is not None
    assert os.path.isfile(path)
    assert path.endswith("capability_runtime_consumption.py")
