"""P7.2 — RealTaskExecutor Minimal Implementation Slice (RED contract).

Validates the F5-domain chain:

    TaskExecutor contract -> RealTaskExecutor -> Resolver -> MemoryReadProvider
        -> ExecutionObservation -> ExecutionLedger idempotency gate

Acceptance (all GREEN):
    A  Contract compatibility   - RealTaskExecutor satisfies TaskExecutor
    B  Ledger idempotency       - 2nd execute with same key = HIT, no provider call
    C  Provider isolation       - only MemoryReadProvider is registered/served
    D  Failure semantics        - provider/resolution failure -> FAILED observation
    E  Non-escalation           - execution fact never becomes approved/authorized/
                                  deployed/production
    F  Boundary scan            - no CapabilityGateway / tools / governance mutation
                                  / production wiring imports
"""
from __future__ import annotations

import pathlib
import re

import pytest

from anima.runtime.executor import ExecutionStep, ExecutionObservation, TaskExecutor


def _step(**overrides):
    defaults = dict(
        id="t1",
        title="read memory",
        intent="answer from memory",
        depends_on=[],
        capability_need={
            "capability": "memory.read",
            "args": {"key": "facts"},
            "context": {},
        },
        estimates={},
    )
    defaults.update(overrides)
    return ExecutionStep(**defaults)


def _store():
    return {"facts": {"v": 1}, "empty": None}


def _make_executor(store=None, provider=None):
    """Construct the executor under test (module is RED until implemented)."""
    from anima.runtime.real_task_executor import RealTaskExecutor

    return RealTaskExecutor(store=store, provider=provider)


# ---------------------------------------------------------------------------
# A - Contract compatibility
# ---------------------------------------------------------------------------
def test_a_contract_compatibility():
    executor = _make_executor(_store())
    assert isinstance(executor, TaskExecutor)
    # exactly the contract surface: execute() present, no approve/activate/dispatch
    assert callable(executor.execute)
    for banned in ("approve", "activate", "dispatch", "invoke", "apply", "repair"):
        assert not hasattr(executor, banned), f"RealTaskExecutor leaks {banned!r}"


# ---------------------------------------------------------------------------
# B - Ledger idempotency (2nd execute = HIT, no provider call)
# ---------------------------------------------------------------------------
def test_b_ledger_idempotency():
    from anima.runtime.execution_ledger import ExecutionLedger

    store = _store()
    calls = []
    provider = _provider_with_call_counter(store, calls)
    executor = _make_executor(store, provider=provider)
    ledger = ExecutionLedger()  # F5.1 memory-only mode

    key = ("run-1", "t1", 0)
    first = executor.execute(_step(), key, ledger)
    second = executor.execute(_step(), key, ledger)

    assert first.success is True
    assert second.success is True
    assert len(calls) == 1, "ledger HIT must skip the second provider call"
    assert second == first  # cached observation returned


def _provider_with_call_counter(store, calls):
    from anima.runtime.providers.memory_read_provider import MemoryReadProvider

    provider = MemoryReadProvider(store=store)
    original = provider.invoke

    def counting_invoke(capability, arguments, execution_key):
        calls.append(arguments.get("key"))
        return original(capability, arguments, execution_key)

    provider.invoke = counting_invoke
    return provider


# ---------------------------------------------------------------------------
# C - Provider isolation (only MemoryReadProvider is served)
# ---------------------------------------------------------------------------
def test_c_provider_isolation():
    executor = _make_executor(_store())
    # memory.read resolves
    observation = executor.execute(_step(), ("run-1", "t1", 0))
    assert observation.success is True
    assert observation.metadata.get("provider_id") == "memory.read.local"
    # a capability outside the single provider domain cannot be served:
    # at the resolver layer it raises CapabilityNotFound...
    from anima.runtime.capability_resolver import CapabilityNeed, CapabilityNotFound

    with pytest.raises(CapabilityNotFound):
        executor.resolver.resolve(
            CapabilityNeed(capability_id="code.execute", args={}, context={}),
            ("run-1", "t2", 0),
        )
    # ...and through execute() it is a FAILED observation, never a success and
    # never a crash (I2).
    obs = executor.execute(
        _step(capability_need={"capability": "code.execute", "args": {}, "context": {}}),
        ("run-1", "t2", 0),
    )
    assert obs.success is False
    assert "not registered" in obs.error


# ---------------------------------------------------------------------------
# D - Failure semantics (provider/resolution failure -> FAILED observation)
# ---------------------------------------------------------------------------
def test_d_failure_semantics():
    executor = _make_executor(_store())
    # missing key -> provider failure -> FAILED observation (no exception)
    obs = executor.execute(
        _step(capability_need={"capability": "memory.read", "args": {"key": "missing"}, "context": {}}),
        ("run-1", "t3", 0),
    )
    assert obs.success is False
    assert "not found" in obs.error
    assert obs.metadata.get("side_effect_recorded") is False


# ---------------------------------------------------------------------------
# E - Non-escalation (execution fact never becomes authorization)
# ---------------------------------------------------------------------------
def test_e_non_escalation():
    from anima.runtime.execution_ledger import ExecutionLedger

    executor = _make_executor(_store())
    ledger = ExecutionLedger()  # F5.1 memory-only mode
    key = ("run-1", "t4", 0)
    obs = executor.execute(_step(), key, ledger)

    # the observation vocabulary is fact-only
    payload = " ".join((obs.task_id, str(obs.result), obs.error, str(obs.metadata))).lower()
    for token in ("approved", "authorized", "deployed", "production"):
        assert token not in payload
    # the ledger fact is the same observation (fact, not authorization)
    cached = ledger.get(key)
    assert cached is not None and cached.success is True


# ---------------------------------------------------------------------------
# F - Boundary scan (no gateway / tools / governance / production wiring)
# ---------------------------------------------------------------------------
def test_f_boundary_scan():
    src = pathlib.Path("anima/runtime/real_task_executor.py").read_text(encoding="utf-8")
    # docstring is documentation (negative declarations are allowed); the CODE
    # body must carry no forbidden executable surface.
    parts = src.split('"""')
    code_body = parts[2] if len(parts) >= 3 else src
    forbidden = [
        "CapabilityGateway",
        "gateway",
        "approve_and_execute",
        "capability_mode",
        "webui",
        "napcat",
        r"\brequests\b",
        "httpx",
        "subprocess",
        r"\bssh\b",
        "paramiko",
    ]
    for needle in forbidden:
        assert not re.search(needle, code_body), f"forbidden token in code: {needle}"
    # imports stay inside the F5 runtime domain
    allowed_prefixes = ("anima.runtime.", "anima.constitution.")
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith(("from ", "import ")):
            target = stripped.split(" import ")[0].replace("from ", "").strip()
            if target.startswith("anima."):
                assert target.startswith(allowed_prefixes), (
                    f"import escapes F5 domain: {target!r}"
                )
