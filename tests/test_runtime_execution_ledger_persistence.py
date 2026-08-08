"""F5.2 — ExecutionLedger persistence (durability, serialization, atomicity).

Validates the F5.2 ruling:

- The ledger persists side-effect facts via the shared ``PersistenceProvider``
  (atomic tempfile + fsync + os.replace), deliberately SEPARATE from the
  ``WorkflowRunStore`` storage domain (``workflow_runs/`` vs ``execution_ledger/``).
- The idempotency key is serialized as a STRUCTURED dict
  ``{"run_id", "task_id", "attempt"}`` — never a Python tuple repr
  (so a future cross-language / cross-format migration can parse it).
- Observations are PURE DATA (result/error/metadata survive JSON round-trip;
  callables / runtime instances must be rejected at the serialization boundary).
- A failed ``put`` (write interrupted) leaves NO half-written entry: the fact is
  simply absent, so the executor is free to retry — exactly the F5.2 failure
  semantics (no external transaction yet, atomic file boundary only).

Discipline: F5.1 memory-only ``ExecutionLedger()`` must keep working (backward
compat with F5.1 tests).
"""
from __future__ import annotations

import json
import pathlib
import tempfile

import pytest

from anima.core.persistence import PersistenceProvider
from anima.runtime.executor import ExecutionObservation, FakeTaskExecutor
from anima.runtime.execution_ledger import ExecutionLedger


def _key(run="r1", task="t1", attempt=1):
    return (run, task, attempt)


def _obs(task="t1", success=True, result=None, error="", metadata=None):
    return ExecutionObservation(
        task_id=task,
        success=success,
        result=result if result is not None else {"v": 1},
        error=error,
        metadata=metadata if metadata is not None else {},
    )


# --------------------------------------------------------------------------- #
# 1. Backward compat: F5.1 memory-only mode still works
# --------------------------------------------------------------------------- #
def test_memory_only_mode_still_works():
    """No persistence -> in-memory ledger (F5.1 contract unchanged)."""
    ledger = ExecutionLedger()
    ledger.put(_key(), _obs())
    assert ledger.get(_key()) is not None
    assert ledger.has(_key()) is True
    assert ledger.get((_key()[0], "other", 1)) is None


# --------------------------------------------------------------------------- #
# 2. Restart durability
# --------------------------------------------------------------------------- #
def test_restart_durability():
    """put -> new process (new ledger over same storage) -> get returns cached fact."""
    with tempfile.TemporaryDirectory() as td:
        pp = PersistenceProvider(td)
        ledger1 = ExecutionLedger(persistence=pp)
        key = _key(run="run-A", task="task-9", attempt=2)
        obs = _obs(task="task-9", success=False, error="boom", result={"code": 7})
        ledger1.put(key, obs)

        # simulate process restart: brand-new ledger over the SAME storage
        ledger2 = ExecutionLedger(persistence=PersistenceProvider(td))
        got = ledger2.get(key)
        assert got is not None
        assert got.success is False
        assert got.error == "boom"
        assert got.result == {"code": 7}
        assert got.task_id == "task-9"


def test_get_after_restart_does_not_false_hit_missing_key():
    with tempfile.TemporaryDirectory() as td:
        pp = PersistenceProvider(td)
        ExecutionLedger(persistence=pp).put(_key(task="present"), _obs())
        ledger2 = ExecutionLedger(persistence=PersistenceProvider(td))
        assert ledger2.get(_key(task="absent")) is None


# --------------------------------------------------------------------------- #
# 3. Key serialization: structured dict, NOT tuple repr
# --------------------------------------------------------------------------- #
def test_key_serialization_is_structured_not_tuple_repr():
    with tempfile.TemporaryDirectory() as td:
        pp = PersistenceProvider(td)
        key = _key(run="run-1", task="task-1", attempt=1)
        ExecutionLedger(persistence=pp).put(key, _obs())

        # locate the on-disk file and inspect its raw content
        files = list(pathlib.Path(td).glob("execution_ledger/*.json"))
        assert len(files) == 1
        raw = files[0].read_text(encoding="utf-8")

        # the key must be a structured object, not a python repr
        assert '"(\'' not in raw and "('run-1'" not in raw
        data = json.loads(raw)
        assert data["key"] == {"run_id": "run-1", "task_id": "task-1", "attempt": 1}


def test_multiple_keys_do_not_collide_on_disk():
    with tempfile.TemporaryDirectory() as td:
        pp = PersistenceProvider(td)
        ledger = ExecutionLedger(persistence=pp)
        keys = [
            _key(run="r", task="a", attempt=1),
            _key(run="r", task="a", attempt=2),  # same run/task, diff attempt
            _key(run="r", task="b", attempt=1),  # diff task
            _key(run="s", task="a", attempt=1),  # diff run
        ]
        for k in keys:
            ledger.put(k, _obs(task=k[1]))
        files = list(pathlib.Path(td).glob("execution_ledger/*.json"))
        assert len(files) == 4  # each key -> its own durable file
        for k in keys:
            assert ledger.get(k) is not None


# --------------------------------------------------------------------------- #
# 4. Observation serialization: pure data round-trip
# --------------------------------------------------------------------------- #
def test_observation_serialization_pure_data():
    with tempfile.TemporaryDirectory() as td:
        pp = PersistenceProvider(td)
        ledger = ExecutionLedger(persistence=pp)
        key = _key(run="r2", task="t2", attempt=3)
        obs = _obs(
            task="t2",
            success=True,
            result={"nested": {"list": [1, 2, 3], "flag": True}, "s": "hi"},
            metadata={"skipped": False, "timings": {"ms": 42.5}},
        )
        ledger.put(key, obs)

        got = ExecutionLedger(persistence=PersistenceProvider(td)).get(key)
        assert got.task_id == "t2"
        assert got.result == {"nested": {"list": [1, 2, 3], "flag": True}, "s": "hi"}
        assert got.metadata == {"skipped": False, "timings": {"ms": 42.5}}


def test_observation_rejects_non_serializable_payload():
    """Pure-data boundary: a callable / instance in the payload must NOT persist."""
    with tempfile.TemporaryDirectory() as td:
        pp = PersistenceProvider(td)
        ledger = ExecutionLedger(persistence=pp)
        bad = _obs(result={"cb": lambda: 1})  # non-serializable
        with pytest.raises((TypeError, ValueError)):
            ledger.put(_key(), bad)
        # nothing durable was written for that key
        assert ledger.get(_key()) is None


# --------------------------------------------------------------------------- #
# 5. Failure semantics: atomic boundary, no half-written entry
# --------------------------------------------------------------------------- #
def test_put_atomic_no_half_write_on_failure():
    """If the durable write fails, the fact is absent -> executor may retry."""
    with tempfile.TemporaryDirectory() as td:
        pp = PersistenceProvider(td)
        ledger = ExecutionLedger(persistence=pp)

        # force the underlying atomic write to fail (simulates crash mid-write)
        def _boom(rel_path, obj):
            raise OSError("simulated disk failure")

        pp.write_json = _boom
        with pytest.raises(OSError):
            ledger.put(_key(task="x"), _obs())

        # the fact must not be recoverable (no half-written file)
        assert ledger.get(_key(task="x")) is None


def test_put_succeeds_when_disk_ok():
    with tempfile.TemporaryDirectory() as td:
        pp = PersistenceProvider(td)
        ledger = ExecutionLedger(persistence=pp)
        ledger.put(_key(task="ok"), _obs())
        # file exists + readable
        files = list(pathlib.Path(td).glob("execution_ledger/*.json"))
        assert len(files) == 1
        assert files[0].stat().st_size > 0


# --------------------------------------------------------------------------- #
# 6. Storage-domain isolation + layering guards (AST)
# --------------------------------------------------------------------------- #
def test_ledger_storage_isolated_from_workflow_runs():
    with tempfile.TemporaryDirectory() as td:
        pp = PersistenceProvider(td)
        ledger = ExecutionLedger(persistence=pp)
        ledger.put(_key(task="iso"), _obs())
        # the ledger writes under execution_ledger/, NOT workflow_runs/
        ledger_files = list(pathlib.Path(td).glob("execution_ledger/*.json"))
        workflow_files = list(pathlib.Path(td).glob("workflow_runs/*.json"))
        assert len(ledger_files) == 1
        assert len(workflow_files) == 0  # ledger never touches the workflow domain


def test_ledger_does_not_reference_workflow_authority():
    """Layering guard: ledger must not COUPLE to the Workflow Authority.

    A docstring *mention* of WorkflowRunStore (to explain the storage separation) is
    legitimate documentation, not a coupling. We therefore detect only actual code
    coupling: imports of the workflow modules / types and their use.
    """
    src = pathlib.Path("anima/runtime/execution_ledger.py").read_text(encoding="utf-8")
    coupling_patterns = [
        "from anima.runtime.workflow_run_store",
        "from anima.constitution.workflow import",
        "import recover_run",
        "recover_run(",
        "WorkflowRunStore(",
        "WorkflowRunStore.",
        "WorkflowInterface(",
    ]
    for pat in coupling_patterns:
        assert pat not in src, f"ledger must not couple to {pat}"


def test_fake_executor_round_trips_through_persistent_ledger():
    """Integration with FakeTaskExecutor: fact recorded once, idempotency hit on restart."""
    with tempfile.TemporaryDirectory() as td:
        pp = PersistenceProvider(td)
        key = _key(run="run-X", task="tX", attempt=1)
        script = {key: _obs(task="tX", success=True, result={"read": "X"})}
        calls = []

        # first session: execute + record to ledger
        ex1 = FakeTaskExecutor(script=script, on_execute=lambda: calls.append(1))
        ledger1 = ExecutionLedger(persistence=pp)
        ex1.execute(_step(), key, ledger=ledger1)
        assert len(calls) == 1

        # restart: new ledger over same storage; a fresh executor with empty script
        # must STILL serve the cached observation (no KeyError, no re-execution)
        ledger2 = ExecutionLedger(persistence=PersistenceProvider(td))
        ex2 = FakeTaskExecutor(script={}, on_execute=lambda: calls.append(2))
        obs = ex2.execute(_step(), key, ledger=ledger2)
        assert obs.success is True
        assert obs.result == {"read": "X"}
        assert len(calls) == 1  # the second execute hit the ledger, no execution


def _step():
    """Minimal ExecutionStep stand-in (imports kept light)."""
    from anima.constitution.execution import ExecutionStep

    return ExecutionStep(
        id="t1",
        title="noop",
        intent="do nothing",
        depends_on=[],
        capability_need={},
        estimates={},
    )
