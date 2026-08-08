"""F5.6 — Startup Recovery Coordinator: discover interrupted runs at boot and resume all.

Closes gap C from the F5.5 design: F5.5 resumes EXACTLY ONE crashed run; F5.6 enumerates
every persisted run left in a non-terminal (RUNNING) state at startup and recover_and_resume
each through the F5.5 ResumeCoordinator.

    restart
        |
        v
    StartupRecoveryCoordinator.recover_interrupted_runs()
        |
        v  (for each persisted RUNNING run_id)
    ResumeCoordinator.recover_and_resume(store, planning, run_id)
        |
        v
    PlanOrchestrator._resume_run (F5.3 executor dispatch + ledger idempotency gate)

Discipline (consistent with ADR-0003 / F4-F5 frozen boundary):

- The coordinator only ENUMERATES (via the store's persisted directory) + DELEGATES. It
  performs no capability selection (I1), writes no run/workflow state (I2), and preserves
  execution identity (I3) by delegating ``source_run_id`` = original run id verbatim.
- Discovery does NOT add a list API to the frozen ``WorkflowRunStore`` (F4.1): we glob the
  store's persisted directory (its own path convention ``workflow_runs/{run_id}.json``) and
  load each run via ``store.load`` (proper deserialization + deep copy).
- One failing run must not block recovery of the others; results are collected and returned
  with failures recorded for operator review.
- Idempotency / orphan: ``recover_and_resume`` issues a NEW run_id and leaves the ORIGINAL
  crash snapshot RUNNING on disk (the frozen store has no status-writer, by design). So the
  original RUNNING record is re-enumerated on every boot. This is SAFE (ledger dedup
  guarantees no duplicate side effect — I3); the original record is preserved as forensic
  crash evidence. A future slice may add a recovery marker. We do NOT write run.status
  (that would bypass the Workflow Authority).
"""
from __future__ import annotations

import tempfile

import pytest

from anima.constitution.errors import ConstitutionViolation
from anima.constitution.execution import ExecutionContext
from anima.constitution.planning_interface import PlanningInterface
from anima.constitution.state import (
    Drive,
    Goal,
    GoalLifecycle,
    OrientationAxis,
    PlannerContext,
    RunStatus,
    TaskRunStatus,
)
from anima.constitution.workflow import WorkflowInterface
from anima.core.persistence import PersistenceProvider
from anima.runtime.executor import ExecutionObservation, FakeTaskExecutor, TaskExecutor
from anima.runtime.execution_ledger import ExecutionLedger
from anima.runtime.orchestrator import PlanOrchestrator
from anima.runtime.persistence import WorkflowRunStore
from anima.runtime.recovery import recover_run
from anima.runtime.resume_coordinator import ResumeCoordinator, ResumeError
from anima.runtime.startup_recovery import (
    RunRecoveryResult,
    StartupRecoveryCoordinator,
    recover_interrupted_runs,
)


# --------------------------------------------------------------------------- #
# fixtures / helpers (mirrors test_runtime_resume_coordinator.py patterns)
# --------------------------------------------------------------------------- #
def _active_plan(planning: PlanningInterface) -> str:
    """Create + activate a canonical Plan via the REAL Planning Authority path."""
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


def _obs(task_id="t1", success=True, result=None, error="", metadata=None):
    return ExecutionObservation(
        task_id=task_id,
        success=success,
        result=result if result is not None else {"v": 1},
        error=error,
        metadata=metadata if metadata is not None else {},
    )


class _RecordingExecutor(TaskExecutor):
    """Controlled executor: records each execution, writes the ledger fact, optional crash."""

    def __init__(self, observation, executed_ids, crash_on_task_id=None):
        self._observation = observation
        self._executed_ids = executed_ids
        self._crash_on = crash_on_task_id
        self.last_key = None
        self.last_obs = None

    def execute(self, task, execution_key, ledger=None):
        self._executed_ids.append(task.id)
        self.last_key = execution_key
        self.last_obs = self._observation
        if ledger is not None:
            ledger.put(execution_key, self._observation)
        if self._crash_on is not None and task.id == self._crash_on:
            raise RuntimeError(f"simulated crash on {task.id}")
        return self._observation


class _CrashBeforeSideEffectExecutor(TaskExecutor):
    """Crash BEFORE any side effect — no ledger fact is ever recorded."""

    def __init__(self, observation):
        self._observation = observation
        self.last_key = None

    def execute(self, task, execution_key, ledger=None):
        self.last_key = execution_key  # captured, but nothing happens
        raise RuntimeError("simulated crash before side effect (no ledger fact)")


def _recovered_run(store, workflow, planning, original_run_id):
    """Load the crashed snapshot and run recovery (issues a NEW run_id)."""
    persisted = store.load(original_run_id)
    return recover_run(store, workflow, planning, persisted)


def _crash_after_side_effect(planning, workflow, store, ledger):
    """Run a plan that crashes on t3 AFTER t3's side effect + ledger.put.

    Returns (original_run_id, executed_first) where executed_first records every task
    that actually executed in the first (crashed) run. The original run is left persisted
    as RUNNING on disk (the crash artifact).
    """
    plan_id = _active_plan(planning)
    executed_first = []
    crash_exec = _RecordingExecutor(_obs(), executed_first, crash_on_task_id="t3")
    orch1 = PlanOrchestrator(
        planning=planning, workflow=workflow,
        store=store, executor=crash_exec, ledger=ledger,
    )
    try:
        orch1.run_plan(plan_id, ExecutionContext(capabilities=["knowledge"]))
    except RuntimeError:
        pass  # simulated crash on t3
    original_run_id = workflow.list_runs("goal#understand-X")[-1].id
    return original_run_id, executed_first, crash_exec


def _completed_run(planning, workflow, store, ledger):
    """Run a plan to completion (no crash). Returns the completed run id."""
    plan_id = _active_plan(planning)
    orch = PlanOrchestrator(
        planning=planning, workflow=workflow,
        store=store, executor=_RecordingExecutor(_obs(), []), ledger=ledger,
    )
    completed = orch.run_plan(plan_id, ExecutionContext(capabilities=["knowledge"]))
    assert completed.status is RunStatus.COMPLETED
    return completed.id


# --------------------------------------------------------------------------- #
# F5.6 — startup discovery + batch resume (headline path)
# --------------------------------------------------------------------------- #
def test_startup_recovers_interrupted_run_idempotently():
    """At startup, the interrupted (RUNNING) run is discovered and recover_and_resume'd;
    the pre-crash ledger fact (t3) is found (I3) so its side effect is NOT re-executed."""
    with tempfile.TemporaryDirectory() as td:
        pp = PersistenceProvider(td)
        store = WorkflowRunStore(pp)
        ledger = ExecutionLedger(pp)
        planning = PlanningInterface()
        workflow = WorkflowInterface()

        original_run_id, executed_first, crash_exec = _crash_after_side_effect(
            planning, workflow, store, ledger
        )
        assert ledger.get(crash_exec.last_key) is not None  # t3 fact on disk

        executed_resume = []
        resume_exec = FakeTaskExecutor(script={}, on_execute=lambda: executed_resume.append(1))
        orch2 = PlanOrchestrator(
            planning=planning, workflow=workflow,
            store=store, executor=resume_exec, ledger=ledger,
        )
        coord = ResumeCoordinator(orch2)

        src = StartupRecoveryCoordinator()
        results = src.recover_interrupted_runs(store, planning, coord)

        assert len(results) == 1
        r = results[0]
        assert isinstance(r, RunRecoveryResult)
        assert r.original_run_id == original_run_id
        assert r.status == "recovered"
        assert r.final_status == "completed"
        assert r.error is None
        # CRITICAL: t3's ledger HIT skipped its side effect on resume (idempotent)
        assert executed_resume == []
        assert "t3" in executed_first and "t3" not in executed_resume
        assert ledger.get((original_run_id, "t3", 0)) is not None
        assert ledger.get((original_run_id, "t3", 0)).success is True


def test_startup_module_function_builds_coordinator():
    """The bootstrap convenience entry point builds the ResumeCoordinator and recovers."""
    with tempfile.TemporaryDirectory() as td:
        pp = PersistenceProvider(td)
        store = WorkflowRunStore(pp)
        ledger = ExecutionLedger(pp)
        planning = PlanningInterface()
        workflow = WorkflowInterface()

        original_run_id, executed_first, crash_exec = _crash_after_side_effect(
            planning, workflow, store, ledger
        )

        executed_resume = []
        orch2 = PlanOrchestrator(
            planning=planning, workflow=workflow,
            store=store, executor=FakeTaskExecutor(script={}, on_execute=lambda: executed_resume.append(1)),
            ledger=ledger,
        )

        results = recover_interrupted_runs(store, planning, orch2)

        assert len(results) == 1
        assert results[0].status == "recovered"
        assert results[0].final_status == "completed"
        assert executed_resume == []
        assert "t3" in executed_first and "t3" not in executed_resume


def test_no_interrupted_runs_returns_empty():
    """A store with only a COMPLETED run yields no recovery results (nothing RUNNING)."""
    with tempfile.TemporaryDirectory() as td:
        pp = PersistenceProvider(td)
        store = WorkflowRunStore(pp)
        ledger = ExecutionLedger(pp)
        planning = PlanningInterface()
        workflow = WorkflowInterface()

        _completed_run(planning, workflow, store, ledger)

        orch2 = PlanOrchestrator(
            planning=planning, workflow=workflow,
            store=store, executor=FakeTaskExecutor({}), ledger=ledger,
        )
        coord = ResumeCoordinator(orch2)

        results = StartupRecoveryCoordinator().recover_interrupted_runs(store, planning, coord)
        assert results == []


def test_skips_terminal_but_recovers_running():
    """Mixed store (one COMPLETED + one RUNNING): only the RUNNING run is recovered."""
    with tempfile.TemporaryDirectory() as td:
        pp = PersistenceProvider(td)
        store = WorkflowRunStore(pp)
        ledger = ExecutionLedger(pp)
        planning = PlanningInterface()
        workflow = WorkflowInterface()

        completed_id = _completed_run(planning, workflow, store, ledger)
        # second run on the same goal/plan crashes -> leaves a RUNNING artifact
        original_run_id, _, crash_exec = _crash_after_side_effect(
            planning, workflow, store, ledger
        )
        assert completed_id != original_run_id

        orch2 = PlanOrchestrator(
            planning=planning, workflow=workflow,
            store=store, executor=FakeTaskExecutor({}), ledger=ledger,
        )
        coord = ResumeCoordinator(orch2)

        results = StartupRecoveryCoordinator().recover_interrupted_runs(store, planning, coord)

        assert len(results) == 1
        assert results[0].original_run_id == original_run_id
        assert results[0].status == "recovered"


def test_one_failing_run_does_not_block_others():
    """Per-run isolation: a failure recovering one interrupted run must not block the rest."""
    with tempfile.TemporaryDirectory() as td:
        pp = PersistenceProvider(td)
        store = WorkflowRunStore(pp)
        ledger = ExecutionLedger(pp)
        planning = PlanningInterface()
        workflow = WorkflowInterface()

        # two crash artifacts -> two persisted RUNNING runs
        id_a, _, _ = _crash_after_side_effect(planning, workflow, store, ledger)
        id_b, _, _ = _crash_after_side_effect(planning, workflow, store, ledger)
        assert id_a != id_b

        discovered = StartupRecoveryCoordinator._discover_run_ids(store)
        assert {id_a, id_b} <= set(discovered)

        # a flaky coordinator that fails on id_a but succeeds on id_b
        class _FlakyCoordinator:
            def __init__(self, fail_id):
                self._fail_id = fail_id
                self._fake_final = type(
                    "F", (), {"status": type("S", (), {"value": "completed"})()}
                )()

            def recover_and_resume(self, store, planning, run_id):
                if run_id == self._fail_id:
                    raise RuntimeError("boom")
                return self._fake_final

        flaky = _FlakyCoordinator(fail_id=id_a)
        results = StartupRecoveryCoordinator().recover_interrupted_runs(store, planning, flaky)

        by_id = {r.original_run_id: r for r in results}
        assert set(by_id) == {id_a, id_b}
        assert by_id[id_a].status == "failed"
        assert by_id[id_a].error is not None
        assert by_id[id_b].status == "recovered"
        assert by_id[id_b].final_status == "completed"


def test_discovers_run_ids_via_store_glob():
    """Discovery globs the store's persisted directory and returns run ids (no store API change)."""
    with tempfile.TemporaryDirectory() as td:
        pp = PersistenceProvider(td)
        store = WorkflowRunStore(pp)
        ledger = ExecutionLedger(pp)
        planning = PlanningInterface()
        workflow = WorkflowInterface()

        original_run_id, _, _ = _crash_after_side_effect(planning, workflow, store, ledger)
        ids = StartupRecoveryCoordinator._discover_run_ids(store)

        assert original_run_id in ids
        # only one persisted run exists (recovery is in-memory; original is the artifact)
        assert ids == [original_run_id]


# --------------------------------------------------------------------------- #
# Boundary guards (consistent with F5.5's "does not couple to recovery source")
# --------------------------------------------------------------------------- #
def test_startup_coordinator_does_not_couple_to_recovery_source():
    """The startup coordinator may CALL ResumeCoordinator.recover_and_resume (which calls
    recover_run) but must not redefine recover_run, must not start a scheduler in
    recovery.py, and must enumerate via the store directory (not re-implement recovery)."""
    import ast
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "anima" / "runtime" / "startup_recovery.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    # must not redefine recover_run (F4 frozen)
    defines_recover_run = any(
        isinstance(n, ast.FunctionDef) and n.name == "recover_run" for n in ast.walk(tree)
    )
    assert not defines_recover_run, "startup coordinator must not redefine recover_run"
    # must call the resume coordinator (delegation, not re-implementation)
    assert "recover_and_resume" in src
    # must not redefine recover_run (F4 frozen) — delegate via the F5.5 coordinator instead
    defines_recover_run = any(
        isinstance(n, ast.FunctionDef) and n.name == "recover_run" for n in ast.walk(tree)
    )
    assert not defines_recover_run, "startup coordinator must not redefine recover_run (F4 frozen)"
    # must not start a scheduler (F4 primitive purity; boundary rule from PM)
    assert "start_scheduler" not in src
    # must enumerate via the persisted directory, not via WorkflowInterface.list_runs
    # (which reads the in-memory registry that is empty at a fresh boot)
    assert "workflow_runs" in src  # glob discovery convention
    assert "list_runs" not in src  # we do NOT enumerate via the in-memory workflow registry
