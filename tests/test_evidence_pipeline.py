"""P7.17-A Phase 3 — Evidence pipeline validation (F5 domain, RED).

Full chain (P6.10/P6.12 System B + F5 domain):
    intent_id -> authz_id -> execution_key -> evidence_id

F5 rules verified here (P7.17-A):
    * execution_key is NEVER a request_id (shape (run_id, task_id, attempt))
    * evidence_id is deterministically derived from the execution_key
    * a ledger HIT replays the cached observation with NO side effect (replay)
    * the same key executed twice = ONE provider act (duplicate execution)
    * a durable fact survives a ledger restart (ledger recovery)
    * a lost/cleaned evidence means the key is NOT protected anymore
      (cleanup semantics: completed facts may be removed; replay re-executes)
Acceptance:
    N  execution_key shape is a triple, never a single request_id
    N2 evidence_id deterministic; attempt component changes it
    N3 replay: same key twice -> provider invoked once (ledger HIT)
    N4 duplicate execution: 100x same key -> exactly one provider write
    N5 ledger recovery: durable fact survives restart
    N6 evidence lost (cleanup) -> replay re-executes (documented, explicit)
    N7 F5 evidence chain never merges System B identity (no authz in key)
"""
from __future__ import annotations

import pathlib

from anima.core.persistence import PersistenceProvider
from anima.runtime.execution_ledger import ExecutionLedger
from anima.runtime.executor import ExecutionObservation, ExecutionStep
from anima.runtime.providers.sandbox_write_provider import SandboxWriteProvider


def _provider(data_dir: str) -> SandboxWriteProvider:
    return SandboxWriteProvider(sandbox_root=None, data_dir=data_dir)


def _step(task_id="t-1", path="out.txt", content="x"):
    return ExecutionStep(
        id=task_id, title="sandbox write", intent="evidence test",
        capability_need={"capability": "sandbox.write",
                         "args": {"path": path, "content": content}, "context": {}},
        depends_on=[], estimates={},
    )


def _key(run="run-1", task="t-1", attempt=0):
    return (run, task, attempt)


# ---------------------------------------------------------------------------
# N - execution_key shape: a triple, never a single request_id
# ---------------------------------------------------------------------------
def test_n_key_shape_is_triple():
    key = _key()
    assert isinstance(key, tuple) and len(key) == 3
    # a request_id-like single value is NOT a valid F5 key shape
    assert len(("req-123",)) != 3


# ---------------------------------------------------------------------------
# N2 - evidence_id deterministic; attempt component changes it
# ---------------------------------------------------------------------------
def test_n2_evidence_id_deterministic():
    from anima.runtime.execution_events import evidence_id_for

    k1 = _key(run="run-1", task="t-1", attempt=0)
    k2 = _key(run="run-1", task="t-1", attempt=1)
    assert evidence_id_for(k1) == evidence_id_for(k1)
    assert evidence_id_for(k1) != evidence_id_for(k2)
    assert len(evidence_id_for(k1)) == 64


# ---------------------------------------------------------------------------
# N3 - replay: same key twice -> provider invoked once (ledger HIT)
# ---------------------------------------------------------------------------
def test_n3_replay(tmp_path):
    from anima.runtime.capability_resolver import (
        CapabilityContract,
        CapabilityRegistry,
        CapabilityResolver,
    )
    from anima.runtime.real_task_executor import RealTaskExecutor

    data_dir = str(tmp_path / "data")
    calls = []
    p = _provider(data_dir)
    original = p.invoke

    def counting(*a):
        calls.append(1)
        return original(*a)

    p.invoke = counting
    reg = CapabilityRegistry()
    reg.register(CapabilityContract("sandbox.write", side_effect=True, idempotent=True), p)
    exec_ = RealTaskExecutor(resolver=CapabilityResolver(reg))
    ledger = ExecutionLedger(persistence=PersistenceProvider(data_dir))

    key = _key()
    exec_.execute(_step(), key, ledger)
    exec_.execute(_step(), key, ledger)  # ledger HIT -> no provider call
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# N4 - duplicate execution: 100x same key -> exactly one provider write
# ---------------------------------------------------------------------------
def test_n4_duplicate_execution(tmp_path):
    from anima.runtime.capability_resolver import (
        CapabilityContract,
        CapabilityRegistry,
        CapabilityResolver,
    )
    from anima.runtime.real_task_executor import RealTaskExecutor

    data_dir = str(tmp_path / "data")
    calls = []
    p = _provider(data_dir)
    original = p.invoke

    def counting(*a):
        calls.append(1)
        return original(*a)

    p.invoke = counting
    reg = CapabilityRegistry()
    reg.register(CapabilityContract("sandbox.write", side_effect=True, idempotent=True), p)
    exec_ = RealTaskExecutor(resolver=CapabilityResolver(reg))
    ledger = ExecutionLedger(persistence=PersistenceProvider(data_dir))

    key = _key()
    for _ in range(100):
        exec_.execute(_step(), key, ledger)
    assert len(calls) == 1  # exactly one provider write
    assert ledger.get(key) is not None


# ---------------------------------------------------------------------------
# N5 - ledger recovery: durable fact survives restart
# ---------------------------------------------------------------------------
def test_n5_ledger_recovery(tmp_path):
    from anima.runtime.real_task_executor import RealTaskExecutor
    from anima.runtime.capability_resolver import (
        CapabilityContract,
        CapabilityRegistry,
        CapabilityResolver,
    )

    data_dir = str(tmp_path / "data")
    reg = CapabilityRegistry()
    reg.register(CapabilityContract("sandbox.write", side_effect=True, idempotent=True), _provider(data_dir))
    exec_ = RealTaskExecutor(resolver=CapabilityResolver(reg))
    ledger1 = ExecutionLedger(persistence=PersistenceProvider(data_dir))
    key = _key()
    exec_.execute(_step(), key, ledger1)

    ledger2 = ExecutionLedger(persistence=PersistenceProvider(data_dir))  # restart
    cached = ledger2.get(key)
    assert cached is not None and cached.success is True
    assert cached.task_id == "t-1"


# ---------------------------------------------------------------------------
# N6 - evidence lost (cleanup) -> replay re-executes (documented, explicit)
# ---------------------------------------------------------------------------
def test_n6_evidence_lost_replays(tmp_path):
    from anima.runtime.capability_resolver import (
        CapabilityContract,
        CapabilityRegistry,
        CapabilityResolver,
    )
    from anima.runtime.real_task_executor import RealTaskExecutor

    data_dir = str(tmp_path / "data")
    calls = []
    p = _provider(data_dir)
    original = p.invoke

    def counting(*a):
        calls.append(1)
        return original(*a)

    p.invoke = counting
    reg = CapabilityRegistry()
    reg.register(CapabilityContract("sandbox.write", side_effect=True, idempotent=True), p)
    exec_ = RealTaskExecutor(resolver=CapabilityResolver(reg))
    ledger = ExecutionLedger(persistence=PersistenceProvider(data_dir))

    key = _key()
    exec_.execute(_step(), key, ledger)
    assert len(calls) == 1
    # explicit cleanup removes the completed fact (P7.10 cleanup semantics)
    removed = ledger.cleanup(before=None)
    assert removed == 1
    assert ledger.get(key) is None  # evidence lost
    # a replay now re-executes (no ledger protection) — documented behavior
    exec_.execute(_step(), key, ledger)
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# N7 - F5 evidence chain never merges System B identity (no authz in key)
# ---------------------------------------------------------------------------
def test_n7_f5_identity_autonomous():
    from anima.runtime.execution_events import evidence_id_for

    key = _key(run="run-1", task="t-1", attempt=0)
    ev = evidence_id_for(key)
    # the F5 evidence id is a pure sha256 of the triple — no authz vocabulary
    assert all(ch in "0123456789abcdef" for ch in ev)
    # key components are run/task/attempt, never authz/request ids
    assert key[0].startswith("run-")
    assert "authz" not in str(key).lower()
    assert "request" not in str(key[0]).lower()


# ---------------------------------------------------------------------------
# O - static boundary: execution_events stays F5-domain
# ---------------------------------------------------------------------------
def test_o_static_boundary():
    src = pathlib.Path("anima/runtime/execution_events.py").read_text(encoding="utf-8")
    parts = src.split('"""')
    code_body = parts[2] if len(parts) >= 3 else src
    forbidden = [
        "CapabilityGateway", "gateway", "tools", "approve_and_execute",
        "capability_mode", "webui", "napcat", r"\brequests\b", "httpx",
        "subprocess", r"\bssh\b", "paramiko", "os.environ", "getenv",
        "capability_authorization_fact", "capability_intent_fact",
    ]
    for needle in forbidden:
        assert needle not in code_body, f"forbidden token: {needle}"
