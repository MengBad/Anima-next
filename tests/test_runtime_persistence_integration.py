"""Phase F4.2b — runtime persistence timing integration tests.

Validates the frozen F4.2 timing policy wired into PlanOrchestrator:

- store.save exists ONLY in the runtime layer (observer): the Workflow Authority never
  learns a store exists, and no save hook lives in constitution.
- start_run 后 durable: a run that was born is immediately recoverable (even if the
  subsequent execution crashes).
- 每次 report_task_outcome 后 durable: accepted progress is never silently lost.
- terminal run 返回前 durable: terminal => durable, unconditionally.
- save failure 不静默吞掉: a failed write propagates to the caller (crash-window
  target is 0 transitions).
"""
from __future__ import annotations

import pathlib
import tempfile

from anima.constitution.execution import ExecutionResult, ExecutionRuntime, ExecutionStatus
from anima.constitution.planning_interface import PlanningInterface
from anima.constitution.state import (
    Drive,
    ExecutionContext,
    Goal,
    GoalLifecycle,
    OrientationAxis,
    PlannerContext,
    RunStatus,
)
from anima.constitution.workflow import WorkflowInterface
from anima.core.persistence import PersistenceProvider
from anima.runtime.orchestrator import PlanOrchestrator
from anima.runtime.persistence import WorkflowRunStore


class _RecordingStore:
    """Wraps a real store and records the status sequence of every save (timing evidence)."""

    def __init__(self, inner: WorkflowRunStore):
        self._inner = inner
        self.saves: list[str] = []

    def save(self, run) -> None:
        self.saves.append(run.status.value)
        self._inner.save(run)


class _BoomExecution(ExecutionRuntime):
    """Execution that crashes right after start_run (proves start_run was already durable)."""

    def run(self, plan, context=None):  # noqa: D102
        raise RuntimeError("execution crashed")


class _FailingExecution(ExecutionRuntime):
    """Execution that reports a FAILED aggregate (F-04 fires on the first report)."""

    def run(self, plan, context=None):  # noqa: D102
        return ExecutionResult(
            plan_ref="plan#understand-X#run-fail",
            status=ExecutionStatus.FAILED,
            outcome="boom",
            lessons=["failed"],
        )


class _BoomStore:
    """Store whose write fails — save failure must propagate, never be swallowed."""

    def save(self, run) -> None:  # noqa: D102
        raise RuntimeError("disk full")


def _active_plan_id(planning: PlanningInterface) -> str:
    goal = Goal(
        target="understand X",
        success_criterion="can explain X",
        horizon="weeks",
        priority=0.8,
        lifecycle=GoalLifecycle.ACTIVE,
    )
    drive = Drive(
        orientation={
            OrientationAxis.APPROACH: 0.6,
            OrientationAxis.AVOID: 0.1,
            OrientationAxis.FOCUS: 0.5,
        }
    )
    ctx = PlannerContext(capabilities=["knowledge"], environment={"safe_to_retry": True})
    plan = planning.request_plan("goal#understand-X", goal=goal, drive=drive, context=ctx)
    planning.activate_plan(plan.id)
    return plan.id


def test_start_run_is_durable_before_execution():
    """F4.2b-1: the run is snapshotted right after start_run — a crash in execution
    still leaves a recoverable (running) snapshot on disk."""
    with tempfile.TemporaryDirectory() as td:
        planning = PlanningInterface()
        plan_id = _active_plan_id(planning)
        store = WorkflowRunStore(PersistenceProvider(data_dir=td))
        orch = PlanOrchestrator(
            planning=planning,
            workflow=WorkflowInterface(),
            execution=_BoomExecution(),
            store=store,
        )
        try:
            orch.run_plan(plan_id, ExecutionContext(capabilities=["knowledge"]))
        except RuntimeError:
            pass  # execution crashed — expected
        else:  # pragma: no cover - the crash is the point of this test
            raise AssertionError("execution was supposed to crash")

        files = list(pathlib.Path(td).glob("workflow_runs/*.json"))
        assert len(files) == 1, f"start_run must leave exactly one snapshot, got {files}"
        run_id = files[0].stem
        restored = store.load(run_id)
        assert restored.status is RunStatus.RUNNING  # born, durable, awaiting execution


def test_each_report_is_durable_and_terminal_before_return():
    """F4.2b-2/3: SUCCESS flow — every accepted report is snapshotted, and the terminal
    run is snapshotted before run_plan returns. Timing evidence = the recorded sequence."""
    with tempfile.TemporaryDirectory() as td:
        planning = PlanningInterface()
        plan_id = _active_plan_id(planning)
        inner = WorkflowRunStore(PersistenceProvider(data_dir=td))
        store = _RecordingStore(inner)
        orch = PlanOrchestrator(
            planning=planning,
            workflow=WorkflowInterface(),
            execution=ExecutionRuntime(),
            store=store,
        )
        final = orch.run_plan(plan_id, ExecutionContext(capabilities=["knowledge"]))

    assert final.status is RunStatus.COMPLETED
    # 1 (start_run=running) + N reports + 1 (terminal=completed)
    assert store.saves[0] == "running"                # start_run 后 durable
    assert store.saves[-1] == "completed"             # terminal 返回前 durable
    assert store.saves.count("running") >= 1          # 至少 start_run 一次
    # every accepted report left a snapshot in between (3 tasks => 3 running/completed)
    assert len(store.saves) >= 1 + 3 + 1


def test_failed_report_makes_terminal_durable_at_first_report():
    """F4.2b-3b: FAILED aggregate — the first report concludes the run (F-04), and that
    terminal verdict is snapshotted; later snapshots stay out (refused reports)."""
    with tempfile.TemporaryDirectory() as td:
        planning = PlanningInterface()
        plan_id = _active_plan_id(planning)
        inner = WorkflowRunStore(PersistenceProvider(data_dir=td))
        store = _RecordingStore(inner)
        orch = PlanOrchestrator(
            planning=planning,
            workflow=WorkflowInterface(),
            execution=_FailingExecution(),
            store=store,
        )
        final = orch.run_plan(plan_id, ExecutionContext(capabilities=["knowledge"]))

    assert final.status is RunStatus.FAILED
    assert store.saves[0] == "running"
    assert store.saves[-1] == "failed"    # terminal verdict durable
    assert "failed" in store.saves        # the F-04 terminal snapshot happened


def test_save_failure_is_not_swallowed():
    """F4.2b-4: a failed store.write propagates out of run_plan — crash-window target is
    0 transitions, so a failed save must surface, never pass silently."""
    import pytest

    planning = PlanningInterface()
    plan_id = _active_plan_id(planning)
    orch = PlanOrchestrator(
        planning=planning,
        workflow=WorkflowInterface(),
        execution=ExecutionRuntime(),
        store=_BoomStore(),
    )
    with pytest.raises(RuntimeError, match="disk full"):
        orch.run_plan(plan_id, ExecutionContext(capabilities=["knowledge"]))


def test_no_store_injected_keeps_prior_behavior():
    """F4.2b-5: without a store, run_plan behaves exactly as before (pure in-memory)."""
    planning = PlanningInterface()
    plan_id = _active_plan_id(planning)
    orch = PlanOrchestrator(
        planning=planning,
        workflow=WorkflowInterface(),
        execution=ExecutionRuntime(),
    )
    final = orch.run_plan(plan_id, ExecutionContext(capabilities=["knowledge"]))
    assert final.status is RunStatus.COMPLETED


def test_constitution_has_no_store_reference():
    """F4.2b-6: the save hook lives ONLY in the runtime layer — constitution files never
    mention the store (Workflow Authority has no storage side-effect).

    Docstrings are stripped via AST: prose that says "persistence is a later slice" is
    documentation, not a store reference — the guard targets executable code and the
    store's actual symbols, never the general word.
    """
    import ast

    root = pathlib.Path(__file__).resolve().parents[1]
    for rel in ("anima/constitution/workflow.py", "anima/constitution/state.py",
                "anima/constitution/execution.py"):
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
                if ast.get_docstring(node) is not None:
                    node.body = node.body[1:]
        executable = ast.unparse(tree)
        for token in ("WorkflowRunStore", "PersistenceProvider", "store.save"):
            assert token not in executable, f"{rel} must not know the store: {token!r}"
        # no import of the runtime persistence module either
        assert "runtime.persistence" not in executable, f"{rel} must not import the store"
