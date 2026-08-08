"""F5.3 — Recovery + Executor integration (the F5 closure).

Proves the core F5 value proposition:

    Recovery + Executor + Ledger + RUNNING durability  =>  NO duplicate external side effect

Three integration scenarios, exercised end-to-end through the real authorities
(Workflow + Planning + WorkflowRunStore + PersistenceProvider) and the orchestrator
glue, with a crash simulated at precise points in the window:

- Scenario A (normal):        PENDING -> RUNNING(durable) -> execute -> DONE(durable) + ledger fact
- Scenario B (crash after side effect):
                              RUNNING saved -> side effect -> ledger.put -> CRASH -> recover
                              -> resume -> ledger HIT -> cached observation, NO re-execution
                              (external side-effect count == 1)
- Scenario C (crash before side effect):
                              RUNNING saved -> CRASH (no ledger fact) -> recover -> resume
                              -> ledger MISS -> re-execute (count == 1, it never happened)
                              + a fresh ledger record is created

The ledger key's run-component for a resumed run is the ORIGINAL crashed run's id
(passed as ``source_run_id``), so the facts recorded before the crash are still found
after ``recover_run`` issues a NEW run_id — this is what prevents the duplicate.

Discipline: the orchestrator only drives public Workflow APIs; it never writes run
state, never names the run outcome, and never couples to the recovery module.
"""
from __future__ import annotations

import tempfile

from anima.constitution.errors import ConstitutionViolation
from anima.constitution.execution import ExecutionRuntime
from anima.constitution.planning_interface import PlanningInterface
from anima.constitution.state import (
    Drive,
    ExecutionContext,
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


# --------------------------------------------------------------------------- #
# fixtures / helpers
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


# --------------------------------------------------------------------------- #
# Scenario A — normal execution with RUNNING durability
# --------------------------------------------------------------------------- #
def test_scenario_a_normal_execution_records_fact_and_concludes():
    with tempfile.TemporaryDirectory() as td:
        pp = PersistenceProvider(td)
        store = WorkflowRunStore(pp)
        ledger = ExecutionLedger(pp)
        planning = PlanningInterface()
        plan_id = _active_plan(planning)

        executed = []
        executor = _RecordingExecutor(_obs(), executed)
        orch = PlanOrchestrator(
            planning=planning, workflow=WorkflowInterface(),
            store=store, executor=executor, ledger=ledger,
        )
        final = orch.run_plan(plan_id, ExecutionContext(capabilities=["knowledge"]))

        # run concluded, every task DONE, exactly one side effect per task
        assert final.status is RunStatus.COMPLETED
        assert all(s.status is TaskRunStatus.DONE for s in final.task_states)
        assert len(executed) == len(final.task_states)
        # the ledger recorded the last task's fact under the (run_id, task_id, attempts) key
        assert ledger.get(executor.last_key) is not None
        assert ledger.get(executor.last_key).success is True


# --------------------------------------------------------------------------- #
# Scenario B — crash AFTER side effect + ledger.put: resume must HIT the ledger
# --------------------------------------------------------------------------- #
def test_scenario_b_crash_after_side_effect_no_duplicate():
    with tempfile.TemporaryDirectory() as td:
        pp = PersistenceProvider(td)
        store = WorkflowRunStore(pp)
        ledger = ExecutionLedger(pp)
        planning = PlanningInterface()
        plan_id = _active_plan(planning)
        workflow = WorkflowInterface()

        # FIRST run: t1, t2 complete (side effect + ledger put); t3 executes its side
        # effect + ledger.put, THEN crashes before the DONE report.
        executed_first = []
        crash_exec = _RecordingExecutor(
            _obs(), executed_first, crash_on_task_id="t3"
        )
        orch1 = PlanOrchestrator(
            planning=planning, workflow=workflow,
            store=store, executor=crash_exec, ledger=ledger,
        )
        try:
            orch1.run_plan(plan_id, ExecutionContext(capabilities=["knowledge"]))
        except RuntimeError:
            pass  # simulated crash on t3

        # every task's side effect happened once in the first run, and the facts are durable
        assert len(executed_first) == len(workflow.list_runs("goal#understand-X")[0].task_states)
        original_run_id = workflow.list_runs("goal#understand-X")[0].id
        assert ledger.get(crash_exec.last_key) is not None  # t3's fact is on disk

        # RECOVER (new run_id) then RESUME with the original run id as the ledger seed
        recovered = _recovered_run(store, workflow, planning, original_run_id)
        assert recovered.status.value == "running"

        executed_resume = []
        # resume executor: empty script -> on a ledger HIT it returns the cached obs
        # WITHOUT executing (no new side effect). t1/t2 are already terminal (skipped).
        resume_exec = FakeTaskExecutor(script={}, on_execute=lambda: executed_resume.append(1))
        orch2 = PlanOrchestrator(
            planning=planning, workflow=workflow,
            store=store, executor=resume_exec, ledger=ledger,
        )
        final = orch2._resume_run(recovered, source_run_id=original_run_id)

        # the run is now concluded and every task resolved from cached facts
        assert final.status is RunStatus.COMPLETED
        assert all(s.status is TaskRunStatus.DONE for s in final.task_states)
        # CRITICAL: nothing was re-executed on resume — t3's ledger HIT skipped its side effect
        assert len(executed_resume) == 0
        assert "t3" in executed_first and "t3" not in executed_resume
        # the recovered task's result matches the cached observation
        assert final.task_states[-1].result == {"v": 1}


# --------------------------------------------------------------------------- #
# Scenario C — crash BEFORE side effect: resume must MISS ledger and re-execute
# --------------------------------------------------------------------------- #
def test_scenario_c_crash_before_side_effect_retries_once():
    with tempfile.TemporaryDirectory() as td:
        pp = PersistenceProvider(td)
        store = WorkflowRunStore(pp)
        ledger = ExecutionLedger(pp)
        planning = PlanningInterface()
        plan_id = _active_plan(planning)
        workflow = WorkflowInterface()

        # FIRST run: crash on the very first task BEFORE any side effect (no ledger fact).
        crash_exec = _CrashBeforeSideEffectExecutor(_obs())
        orch1 = PlanOrchestrator(
            planning=planning, workflow=workflow,
            store=store, executor=crash_exec, ledger=ledger,
        )
        try:
            orch1.run_plan(plan_id, ExecutionContext(capabilities=["knowledge"]))
        except RuntimeError:
            pass

        # nothing executed; no fact recorded
        original_run_id = workflow.list_runs("goal#understand-X")[0].id
        assert ledger.get(crash_exec.last_key) is None

        recovered = _recovered_run(store, workflow, planning, original_run_id)

        # RESUME: ledger MISS for every task -> the executor runs each exactly once
        executed_resume = []
        resume_exec = _RecordingExecutor(_obs(), executed_resume)
        orch2 = PlanOrchestrator(
            planning=planning, workflow=workflow,
            store=store, executor=resume_exec, ledger=ledger,
        )
        final = orch2._resume_run(recovered, source_run_id=original_run_id)

        assert final.status is RunStatus.COMPLETED
        assert all(s.status is TaskRunStatus.DONE for s in final.task_states)
        # each task's side effect happened exactly once on the resume (none before the crash)
        assert len(executed_resume) == len(final.task_states)
        # fresh ledger records now exist for every task
        for tid in ("t1", "t2", "t3"):
            assert ledger.get((original_run_id, tid, 0)) is not None


# --------------------------------------------------------------------------- #
# Resume skips already-terminal (recovered) tasks
# --------------------------------------------------------------------------- #
def test_resume_skips_already_terminal_tasks():
    """A recovered run that already has terminal tasks must not re-dispatch them; a
    PENDING task whose ledger fact exists must hit the ledger (no re-execution)."""
    with tempfile.TemporaryDirectory() as td:
        pp = PersistenceProvider(td)
        store = WorkflowRunStore(pp)
        ledger = ExecutionLedger(pp)
        planning = PlanningInterface()
        plan_id = _active_plan(planning)
        workflow = WorkflowInterface()

        # t1, t2 complete (facts put); t3 executes its side effect + ledger.put, then crashes
        executed_first = []
        crash_exec = _RecordingExecutor(_obs(), executed_first, crash_on_task_id="t3")
        orch1 = PlanOrchestrator(
            planning=planning, workflow=workflow,
            store=store, executor=crash_exec, ledger=ledger,
        )
        try:
            orch1.run_plan(plan_id, ExecutionContext(capabilities=["knowledge"]))
        except RuntimeError:
            pass

        original_run_id = workflow.list_runs("goal#understand-X")[0].id
        recovered = _recovered_run(store, workflow, planning, original_run_id)
        # t1, t2 DONE (replayed by recovery), t3 PENDING (was RUNNING at crash)

        executed_resume = []
        # resume executor: empty script -> on a ledger HIT it returns the cached obs without
        # executing. t1/t2 are already terminal (skipped by _resume_run); t3 hits its fact.
        resume_exec = FakeTaskExecutor(script={}, on_execute=lambda: executed_resume.append(1))
        orch2 = PlanOrchestrator(
            planning=planning, workflow=workflow,
            store=store, executor=resume_exec, ledger=ledger,
        )
        orch2._resume_run(recovered, source_run_id=original_run_id)

        # t1/t2 were terminal (skipped, never reaching the executor); t3 hit the ledger
        # (no re-execution). So resume executed NOTHING.
        assert executed_resume == []
        # t1's side effect happened only in the first run, never on resume
        assert "t1" in executed_first and "t1" not in executed_resume


# --------------------------------------------------------------------------- #
# Layering / surface guards (keep F5.3 disciplined)
# --------------------------------------------------------------------------- #
def test_orchestrator_public_surface_still_only_run_plan():
    """F5.3 added _resume_run but it MUST stay private (the glue exposes one op)."""
    import ast
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "anima" / "runtime" / "orchestrator.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    public = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "PlanOrchestrator":
            for m in node.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and not m.name.startswith("_"):
                    public.add(m.name)
    assert public == {"run_plan"}


def test_orchestrator_f5_does_not_couple_to_recovery():
    """The orchestrator must not reach into the recovery primitive (F5.3 boundary).

    Docstrings are stripped (mirroring the existing authority guard) so prose that
    *names* recovery cannot false-positive the coupling assertion.
    """
    import ast
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "anima" / "runtime" / "orchestrator.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node)
            if doc is not None:
                node.body = node.body[1:]
    executable = ast.unparse(tree)
    for tok in ("recover_run", "recover_all", "RecoveryModule", "anima.runtime.recovery"):
        assert tok not in executable, f"orchestrator must not couple to recovery: {tok!r}"


def test_orchestrator_f5_legacy_mock_path_untouched():
    """Without an executor, run_plan still uses the aggregate ExecutionRuntime mock."""
    planning = PlanningInterface()
    plan_id = _active_plan(planning)
    orch = PlanOrchestrator(
        planning=planning, workflow=WorkflowInterface(), execution=ExecutionRuntime()
    )
    final = orch.run_plan(plan_id, ExecutionContext(capabilities=["knowledge"]))
    assert final.status is RunStatus.COMPLETED
    assert all(s.status is TaskRunStatus.DONE for s in final.task_states)
