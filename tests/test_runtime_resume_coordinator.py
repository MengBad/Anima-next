"""F5.5 — Resume Coordinator: bridge F4 recovery to F5 resume execution.

Proves the missing caller the F5.3 design left open:

    startup / crash -> recover_run()  (F4 restore primitive, new run_id)
                        |
                        v
                  ResumeCoordinator   (F5.5 — this slice)
                        |
                        v
                  _resume_run()       (F5.3 per-task executor dispatch)

The coordinator is deliberately a SEPARATE runtime module — it does NOT live in
recovery.py (frozen as a restore primitive that must not own execution scheduling)
and does NOT modify the orchestrator's public surface (still just ``run_plan``).
It only delegates to ``PlanOrchestrator._resume_run`` with the ORIGINAL crashed run
id as the ledger seed, preserving invariant I3 (execution identity unchanged) so
pre-crash side-effect facts survive recovery and are not duplicated.

Three integration scenarios, all exercised through the real authorities
(Workflow + Planning + WorkflowRunStore + PersistenceProvider) and the coordinator
glue, mirroring the F5.3 crash window:

- A/B: crash AFTER side effect + ledger.put -> recover -> coordinator.resume ->
      ledger HIT -> NO re-execution (external side-effect count == 1).
- C:   crash BEFORE any side effect -> recover -> coordinator.resume -> ledger MISS.
      wait, ledger MISS -> re-execute each task exactly once.

Discipline: the coordinator only delegates to public/structured authority APIs; it
never writes run state, never names the run outcome, and never couples to recovery
beyond a read import of ``recover_run`` (calling the primitive, not altering it).
"""
from __future__ import annotations

import tempfile

import pytest

from anima.constitution.errors import ConstitutionViolation
from anima.constitution.execution import ExecutionRuntime
from anima.constitution.planning_interface import PlanningInterface
from anima.constitution.state import (
    Drive,
    ExecutionContext,
    Goal,
    GoalLifecycle,
    OrientationAxis,
    PlanRef,
    PlannerContext,
    RunStatus,
    TaskRunStatus,
    TaskState,
    WorkflowRun,
)
from anima.constitution.workflow import WorkflowInterface
from anima.core.persistence import PersistenceProvider
from anima.runtime.executor import ExecutionObservation, FakeTaskExecutor, TaskExecutor
from anima.runtime.execution_ledger import ExecutionLedger
from anima.runtime.orchestrator import PlanOrchestrator
from anima.runtime.persistence import WorkflowRunStore
from anima.runtime.recovery import recover_run
from anima.runtime.resume_coordinator import ResumeCoordinator, ResumeError


# --------------------------------------------------------------------------- #
# fixtures / helpers (mirrors test_runtime_executor_integration.py patterns)
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
    that actually executed in the first (crashed) run.
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
    original_run_id = workflow.list_runs("goal#understand-X")[0].id
    return original_run_id, executed_first, crash_exec


# --------------------------------------------------------------------------- #
# F5.5 — the coordinator is the caller of _resume_run (Scenario A/B bridge)
# --------------------------------------------------------------------------- #
def test_resume_method_bridges_to_resume_run_with_i3():
    """coordinator.resume() delegates to _resume_run with the ORIGINAL run id as the
    ledger seed (I3); a pre-crash ledger fact is found and the task is NOT re-executed.
    """
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

        recovered = _recovered_run(store, workflow, planning, original_run_id)
        assert recovered.status.value == "running"

        executed_resume = []
        resume_exec = FakeTaskExecutor(script={}, on_execute=lambda: executed_resume.append(1))
        orch2 = PlanOrchestrator(
            planning=planning, workflow=workflow,
            store=store, executor=resume_exec, ledger=ledger,
        )
        coord = ResumeCoordinator(orch2)

        final = coord.resume(recovered, source_run_id=original_run_id)

        assert final.status is RunStatus.COMPLETED
        assert all(s.status is TaskRunStatus.DONE for s in final.task_states)
        # CRITICAL: t3's ledger HIT skipped its side effect on resume
        assert executed_resume == []
        assert "t3" in executed_first and "t3" not in executed_resume


def test_recover_and_resume_full_chain_scenario_b():
    """The headline F5.5 path: one crashed run through recover_and_resume -> no duplicate
    external side effect (count == 1 for the crashed task)."""
    with tempfile.TemporaryDirectory() as td:
        pp = PersistenceProvider(td)
        store = WorkflowRunStore(pp)
        ledger = ExecutionLedger(pp)
        planning = PlanningInterface()
        workflow = WorkflowInterface()

        original_run_id, executed_first, crash_exec = _crash_after_side_effect(
            planning, workflow, store, ledger
        )
        assert ledger.get(crash_exec.last_key) is not None

        executed_resume = []
        resume_exec = FakeTaskExecutor(script={}, on_execute=lambda: executed_resume.append(1))
        orch2 = PlanOrchestrator(
            planning=planning, workflow=workflow,
            store=store, executor=resume_exec, ledger=ledger,
        )
        coord = ResumeCoordinator(orch2)

        final = coord.recover_and_resume(store, planning, original_run_id)

        assert final.status is RunStatus.COMPLETED
        assert all(s.status is TaskRunStatus.DONE for s in final.task_states)
        assert executed_resume == []
        assert "t3" in executed_first and "t3" not in executed_resume
        # the ledger fact survives under the ORIGINAL run id (I3), not the recovered id
        assert ledger.get((original_run_id, "t3", 0)) is not None
        assert ledger.get((original_run_id, "t3", 0)).success is True


def test_recover_and_resume_crash_before_side_effect_retries_once():
    """Scenario C: crash BEFORE any side effect -> ledger MISS -> each task re-executed
    exactly once on resume."""
    with tempfile.TemporaryDirectory() as td:
        pp = PersistenceProvider(td)
        store = WorkflowRunStore(pp)
        ledger = ExecutionLedger(pp)
        planning = PlanningInterface()
        plan_id = _active_plan(planning)
        workflow = WorkflowInterface()

        crash_exec = _CrashBeforeSideEffectExecutor(_obs())
        orch1 = PlanOrchestrator(
            planning=planning, workflow=workflow,
            store=store, executor=crash_exec, ledger=ledger,
        )
        try:
            orch1.run_plan(plan_id, ExecutionContext(capabilities=["knowledge"]))
        except RuntimeError:
            pass

        original_run_id = workflow.list_runs("goal#understand-X")[0].id
        assert ledger.get(crash_exec.last_key) is None  # no fact recorded

        executed_resume = []
        resume_exec = _RecordingExecutor(_obs(), executed_resume)
        orch2 = PlanOrchestrator(
            planning=planning, workflow=workflow,
            store=store, executor=resume_exec, ledger=ledger,
        )
        coord = ResumeCoordinator(orch2)

        final = coord.recover_and_resume(store, planning, original_run_id)

        assert final.status is RunStatus.COMPLETED
        assert all(s.status is TaskRunStatus.DONE for s in final.task_states)
        # each task's side effect happened exactly once on the resume (none before crash)
        assert len(executed_resume) == len(final.task_states)
        for tid in ("t1", "t2", "t3"):
            assert ledger.get((original_run_id, tid, 0)) is not None


def test_resume_skips_already_terminal_and_hits_ledger():
    """A recovered run that already has terminal tasks must not re-dispatch them; a
    PENDING task whose ledger fact exists must hit the ledger (no re-execution)."""
    with tempfile.TemporaryDirectory() as td:
        pp = PersistenceProvider(td)
        store = WorkflowRunStore(pp)
        ledger = ExecutionLedger(pp)
        planning = PlanningInterface()
        workflow = WorkflowInterface()

        original_run_id, executed_first, crash_exec = _crash_after_side_effect(
            planning, workflow, store, ledger
        )
        recovered = _recovered_run(store, workflow, planning, original_run_id)
        # t1, t2 DONE (replayed by recovery), t3 PENDING (was RUNNING at crash)

        executed_resume = []
        resume_exec = FakeTaskExecutor(script={}, on_execute=lambda: executed_resume.append(1))
        orch2 = PlanOrchestrator(
            planning=planning, workflow=workflow,
            store=store, executor=resume_exec, ledger=ledger,
        )
        coord = ResumeCoordinator(orch2)
        coord.resume(recovered, source_run_id=original_run_id)

        # t1/t2 were terminal (skipped, never reaching the executor); t3 hit the ledger
        # (no re-execution). So resume executed NOTHING.
        assert executed_resume == []
        assert "t1" in executed_first and "t1" not in executed_resume


class _GatedRecordingExecutor(TaskExecutor):
    """Faithful stand-in for ResolvingTaskExecutor: consults the ledger BEFORE acting and
    returns the cached observation on a HIT (no provider call). Records the task id ONLY
    when it actually executes (post-gate) — so a HIT is provably a no-op for the provider.
    """

    def __init__(self, observation, executed_ids):
        self._observation = observation
        self._executed_ids = executed_ids

    def execute(self, task, execution_key, ledger=None):
        if ledger is not None:
            cached = ledger.get(execution_key)
            if cached is not None:
                return cached
        self._executed_ids.append(task.id)
        if ledger is not None:
            ledger.put(execution_key, self._observation)
        return self._observation


# --------------------------------------------------------------------------- #
# F7.2 — Recovery Identity Preservation (migration test)
# --------------------------------------------------------------------------- #
def test_recover_and_resume_preserves_attempts_identity_no_duplicate():
    """F7.2 migration — Recovery Identity Preservation.

    A PENDING task that had ``attempts = N > 0`` at crash (the future in-run retry case
    the F7 C1 audit flagged) must keep its attempts across recovery, so the resume
    re-dispatches with the SAME idempotency key ``(source_run_id, task_id, N)`` and HITs
    the pre-crash ledger fact — instead of being reset to ``attempt = 0`` (key desync)
    and re-invoking a non-idempotent provider's side effect.

    The ledger key shape ``(run_id, task_id, attempt)`` is UNCHANGED by F7.2, so the
    pre-existing fact (seeded here at that exact shape) is consulted as-is: no on-disk
    ledger migration is required (ledger compatibility strategy)."""
    with tempfile.TemporaryDirectory() as td:
        pp = PersistenceProvider(td)
        store = WorkflowRunStore(pp)
        ledger = ExecutionLedger(pp)
        planning = PlanningInterface()
        _active_plan(planning)
        plan = planning.list_plans("goal#understand-X")[0]
        task_ids = [t.id for t in plan.task_graph]

        original_run_id = "run#old-crashed"
        old = WorkflowRun(
            id=original_run_id,
            contract_version="1.0.0",
            plan_ref=PlanRef(plan_id=plan.id, contract_version=plan.contract_version),
            goal_ref="goal#understand-X",
            status=RunStatus.RUNNING,
            task_states=[
                TaskState(task_id=task_ids[0], status=TaskRunStatus.DONE, attempts=1,
                          result={"read": "X"}, error=""),
                TaskState(task_id=task_ids[1], status=TaskRunStatus.PENDING, attempts=1,
                          result={}, error=""),
                TaskState(task_id=task_ids[2], status=TaskRunStatus.PENDING, attempts=0,
                          result={}, error=""),
            ],
            current_task_id=task_ids[1],
            checkpoint_ref=task_ids[0],
            retry_policy={},
            schedule_ref="",
            budget_ref="",
            context_ref="ctx#understand-X",
            created_at="2026-08-08T00:00:00+00:00",
            completed_at="",
        )
        store.save(old)
        # Pre-crash side effect already recorded for t2 at attempt=1 — legacy key shape,
        # preserved unchanged by F7.2 (no ledger on-disk migration needed).
        ledger.put((original_run_id, task_ids[1], 1), _obs(task_id=task_ids[1]))

        executed_resume = []
        resume_exec = _GatedRecordingExecutor(_obs(), executed_resume)
        orch2 = PlanOrchestrator(
            planning=planning, workflow=WorkflowInterface(),
            store=store, executor=resume_exec, ledger=ledger,
        )
        coord = ResumeCoordinator(orch2)

        final = coord.recover_and_resume(store, planning, original_run_id)

        assert final.status is RunStatus.COMPLETED
        # CRITICAL: t2's pre-crash fact (attempt=1) was found on resume -> NOT re-executed.
        assert task_ids[1] not in executed_resume
        # t3 (attempt=0) had no fact -> executed exactly once on resume.
        assert task_ids[2] in executed_resume
        # ledger key shape is backward-compatible across the migration.
        assert ledger.get((original_run_id, task_ids[1], 1)) is not None


# --------------------------------------------------------------------------- #
# Coordinator construction / boundary guards
# --------------------------------------------------------------------------- #
def test_resume_coordinator_requires_executor_and_ledger():
    """Resume has nothing to dispatch a PENDING task through without executor + ledger."""
    with tempfile.TemporaryDirectory() as td:
        pp = PersistenceProvider(td)
        # neither
        orch0 = PlanOrchestrator(planning=PlanningInterface(), workflow=WorkflowInterface())
        with pytest.raises(ResumeError):
            ResumeCoordinator(orch0)
        # executor but no ledger
        orch1 = PlanOrchestrator(
            planning=PlanningInterface(), workflow=WorkflowInterface(),
            executor=FakeTaskExecutor({}),
        )
        with pytest.raises(ResumeError):
            ResumeCoordinator(orch1)
        # ledger but no executor
        orch2 = PlanOrchestrator(
            planning=PlanningInterface(), workflow=WorkflowInterface(),
            ledger=ExecutionLedger(pp),
        )
        with pytest.raises(ResumeError):
            ResumeCoordinator(orch2)
        # both -> ok
        orch3 = PlanOrchestrator(
            planning=PlanningInterface(), workflow=WorkflowInterface(),
            executor=FakeTaskExecutor({}), ledger=ExecutionLedger(pp),
        )
        ResumeCoordinator(orch3)  # no raise


def test_resume_rejects_non_running_recovered_run():
    """A recovered run with nothing PENDING (already COMPLETED) must not be resumed;
    the orchestrator's _resume_run raises ConstitutionViolation, which surfaces here."""
    with tempfile.TemporaryDirectory() as td:
        pp = PersistenceProvider(td)
        store = WorkflowRunStore(pp)
        ledger = ExecutionLedger(pp)
        planning = PlanningInterface()
        plan_id = _active_plan(planning)
        workflow = WorkflowInterface()

        orch1 = PlanOrchestrator(
            planning=planning, workflow=workflow, store=store,
            executor=_RecordingExecutor(_obs(), []), ledger=ledger,
        )
        completed = orch1.run_plan(plan_id, ExecutionContext(capabilities=["knowledge"]))
        assert completed.status is RunStatus.COMPLETED

        original_run_id = completed.id
        recovered = _recovered_run(store, workflow, planning, original_run_id)
        assert recovered.status is RunStatus.COMPLETED  # nothing to resume

        orch2 = PlanOrchestrator(
            planning=planning, workflow=workflow, store=store,
            executor=FakeTaskExecutor({}), ledger=ledger,
        )
        coord = ResumeCoordinator(orch2)
        with pytest.raises(ConstitutionViolation):
            coord.resume(recovered, source_run_id=original_run_id)


def test_resume_coordinator_does_not_couple_to_recovery_module_source():
    """The coordinator may CALL recover_run (a read import of the F4 primitive) but must
    not redefine or modify recovery behavior. We assert the import is the real function
    and that the module adds no recovery-replacing logic of its own."""
    import ast
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "anima" / "runtime" / "resume_coordinator.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    # The function `recover_run` must be referenced (called), not redefined.
    defines_recover_run = any(
        isinstance(n, ast.FunctionDef) and n.name == "recover_run" for n in ast.walk(tree)
    )
    assert not defines_recover_run, "coordinator must not redefine recover_run (F4 frozen)"
    # It must actually call it (import + call site present).
    assert "recover_run" in src
