"""Phase F5.1 — Executor Contract implementation tests.

Validates the frozen F5 Executor Contract WITHOUT real side effects:

    Executor.execute(task, execution_key) -> ExecutionObservation

with

    execution_key = (run_id, task_id, attempt)
    ExecutionObservation = per-task {task_id, success, result, error, metadata}

Coverage per F5.1 acceptance:
- DONE observation
- FAILED observation
- SKIPPED observation
- exception propagation (executor raises -> caller sees it)
- idempotency hit (same key twice -> cached observation, no re-execution)
- authority boundary: executor never writes WorkflowRun / task_state / judges run outcome
- contract alignment: ExecutionObservation is convertible to TaskState for report_task_outcome
"""
from __future__ import annotations

import ast
import pathlib

from anima.constitution.execution import ExecutionStep
from anima.constitution.state import TaskRunStatus, TaskState
from anima.runtime.execution_ledger import ExecutionLedger
from anima.runtime.executor import (
    ExecutionObservation,
    TaskExecutor,
    FakeTaskExecutor,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _step(task_id: str = "t1") -> ExecutionStep:
    return ExecutionStep(
        id=task_id,
        title="Read",
        intent="understand X",
        depends_on=[],
        capability_need={},
        estimates={},
    )


def _key(run_id: str = "run#1", task_id: str = "t1", attempt: int = 1):
    return (run_id, task_id, attempt)


def test_observation_is_per_task_not_aggregate():
    """ExecutionObservation is a per-task record — never a run-level aggregate."""
    obs = ExecutionObservation(task_id="t1", success=True, result={"read": "X"},
                               error="", metadata={})
    assert obs.task_id == "t1"
    assert obs.success is True
    assert obs.result == {"read": "X"}
    # no run-level fields on the observation
    assert not hasattr(obs, "run_id")
    assert not hasattr(obs, "status")


def test_executor_interface_has_execute_only():
    """The Executor contract surface is exactly execute(task, execution_key[, ledger])."""
    import inspect

    params = set(inspect.signature(TaskExecutor.execute).parameters)
    assert params == {"self", "task", "execution_key", "ledger"}
    # TaskExecutor is abstract: cannot instantiate directly
    try:
        TaskExecutor()  # type: ignore[abstract]
    except TypeError:
        pass
    else:  # pragma: no cover - the contract demands an abstract base
        raise AssertionError("TaskExecutor must be abstract (has no execute impl)")


def test_fake_executor_done_observation():
    """DONE: a successful task produces a success observation with result."""
    ex = FakeTaskExecutor(script={_key(): ExecutionObservation(
        task_id="t1", success=True, result={"read": "X"}, error="", metadata={})})
    obs = ex.execute(_step(), _key())
    assert obs.success is True
    assert obs.result == {"read": "X"}


def test_fake_executor_failed_observation():
    """FAILED: a failed task produces success=False with error."""
    ex = FakeTaskExecutor(script={_key(): ExecutionObservation(
        task_id="t1", success=False, result={}, error="boom", metadata={})})
    obs = ex.execute(_step(), _key())
    assert obs.success is False
    assert obs.error == "boom"


def test_fake_executor_skipped_observation():
    """SKIPPED: a skipped task produces success=False with a skip marker in metadata."""
    ex = FakeTaskExecutor(script={_key(): ExecutionObservation(
        task_id="t1", success=False, result={}, error="",
        metadata={"skipped": True})})
    obs = ex.execute(_step(), _key())
    assert obs.success is False
    assert obs.metadata.get("skipped") is True


def test_fake_executor_unknown_key_raises():
    """An unscripted key raises — a controlled executor must not silently succeed."""
    import pytest

    ex = FakeTaskExecutor(script={})
    with pytest.raises(KeyError):
        ex.execute(_step(), _key())


def test_idempotency_hit_returns_cached_observation():
    """Idempotency hit: same key twice (both through the same ledger) -> cached
    observation, no re-execution."""
    ledger = ExecutionLedger()
    calls = []
    ex = FakeTaskExecutor(
        script={_key(): ExecutionObservation(
            task_id="t1", success=True, result={"read": "X"}, error="", metadata={})},
        on_execute=lambda: calls.append(1),
    )
    # FIRST call: key absent -> executes, records the fact in the ledger
    obs1 = ex.execute(_step(), _key(), ledger=ledger)
    assert len(calls) == 1
    assert ledger.has(_key())

    # SECOND call, same key, same ledger: idempotency hit -> cached, no execute
    obs2 = ex.execute(_step(), _key(), ledger=ledger)
    assert obs2 == obs1
    assert obs2.success is True
    assert len(calls) == 1  # still exactly one real execution


def test_observation_is_convertible_to_taskstate_for_report():
    """Contract alignment: an ExecutionObservation maps to a TaskState the Workflow
    report_task_outcome accepts (the executor never reports itself — the orchestrator
    or caller converts; this proves the shapes are compatible)."""
    obs = ExecutionObservation(task_id="t1", success=True, result={"read": "X"},
                               error="", metadata={})
    status = TaskRunStatus.DONE if obs.success else TaskRunStatus.FAILED
    state = TaskState(task_id=obs.task_id, status=status, attempts=1,
                      result=dict(obs.result), error=obs.error)
    assert state.task_id == "t1"
    assert state.status is TaskRunStatus.DONE
    assert state.result == {"read": "X"}


def test_executor_has_no_authority_write_surface():
    """Authority boundary: executor.py never writes WorkflowRun / task_state and never
    judges run outcome (no _transition, no _registry, no workflow import)."""
    src = (REPO_ROOT / "anima" / "runtime" / "executor.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            if ast.get_docstring(node) is not None:
                node.body = node.body[1:]
    executable = ast.unparse(tree)

    for token in ("_transition", "_registry", "WorkflowInterface", "report_task_outcome",
                  "run.status = ", "task_states = "):
        assert token not in executable, f"executor must not reach workflow: {token!r}"


def test_ledger_is_separate_from_run_snapshot_store():
    """Idempotency Ledger is an independent fact source — it must not reference the
    WorkflowRunStore or run-snapshot semantics (F5 ruling: ledger records side-effect
    facts; WorkflowRunStore records lifecycle state)."""
    src = (REPO_ROOT / "anima" / "runtime" / "execution_ledger.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            if ast.get_docstring(node) is not None:
                node.body = node.body[1:]
    executable = ast.unparse(tree)

    assert "WorkflowRunStore" not in executable
    assert "WorkflowRun" not in executable
    assert "recover_run" not in executable
