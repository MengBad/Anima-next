"""Phase F2 — Workflow Ledger Alignment tests (RFC-0003 §5 WorkflowExecutionResult).

Validates the F1 reconciliation (C-scheme): ExecutionResult stays Execution's observation
envelope (RFC-0107, untouched); WorkflowExecutionResult is the Workflow's run-terminal
ledger, a snapshot of WorkflowRun.task_states taken via build_workflow_execution_result.

Coverage per F2 authorization:
1. The ledger's source is WorkflowRun.task_states (snapshot, not a live handle).
2. ExecutionResult and WorkflowExecutionResult are distinct types, never conflated.
3. Execution cannot construct the ledger (execution.py must not know the type).
4. Reflection's old envelope path is unaffected.
5. Runtime orchestration authority does not change (no ledger construction there).
"""
from __future__ import annotations

import pathlib

from anima.constitution.execution import (
    ExecutionResult,
    ExecutionRuntime,
    ExecutionStatus,
)
from anima.constitution.planner import derive_plan
from anima.constitution.planning_interface import PlanningInterface
from anima.constitution.reflection import reflect
from anima.constitution.state import (
    Drive,
    ExecutionContext,
    Goal,
    GoalLifecycle,
    OrientationAxis,
    PlannerContext,
    RunStatus,
    TaskRunStatus,
    TaskState,
    WorkflowExecutionResult,
)
from anima.constitution.workflow import (
    WorkflowInterface,
    build_workflow_execution_result,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


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


def _completed_run(wf: WorkflowInterface, planning: PlanningInterface):
    """Drive a real run to COMPLETED via the real cross-authority path + F-04."""
    plan_id = _active_plan_id(planning)
    plan = planning.read_plan(plan_id)
    run = wf.start_run(plan)
    for state in run.task_states:
        wf.report_task_outcome(
            run.id,
            state.task_id,
            TaskState(
                task_id=state.task_id,
                status=TaskRunStatus.DONE,
                attempts=1,
                result={"ok": True},
                error="",
            ),
        )
    return wf.read_run(run.id)


def test_ledger_source_is_workflowrun_task_states():
    """F2-1: the ledger's task_results are a snapshot of the run's OWN task_states."""
    planning = PlanningInterface()
    wf = WorkflowInterface()
    run = _completed_run(wf, planning)

    ledger = build_workflow_execution_result(run)

    assert ledger.run_id == run.id
    assert ledger.status is RunStatus.COMPLETED
    assert [t.task_id for t in ledger.task_results] == [s.task_id for s in run.task_states]
    assert all(t.status is TaskRunStatus.DONE for t in ledger.task_results)
    # snapshot, not a live handle: mutating the run after the fact cannot rewrite the ledger
    run.task_states[0].status = TaskRunStatus.FAILED
    assert ledger.task_results[0].status is TaskRunStatus.DONE


def test_ledger_is_a_copy_not_a_reference():
    """F2-1b: mutating a ledger TaskState cannot rewrite run-state."""
    planning = PlanningInterface()
    wf = WorkflowInterface()
    run = _completed_run(wf, planning)

    ledger = build_workflow_execution_result(run)
    ledger.task_results[0].status = TaskRunStatus.FAILED

    assert run.task_states[0].status is TaskRunStatus.DONE


def test_execution_result_and_ledger_are_distinct_types():
    """F2-2: ExecutionResult (envelope) and WorkflowExecutionResult (ledger) never conflate."""
    assert WorkflowExecutionResult is not ExecutionResult
    # envelope fields stay on the envelope; ledger fields live only on the ledger
    env = ExecutionResult(plan_ref="p", status=ExecutionStatus.SUCCESS, outcome="ok")
    assert not hasattr(env, "task_results")
    assert not hasattr(env, "run_id")
    assert not hasattr(env, "goal_ref")
    assert not hasattr(env, "completed_at")
    ledger = WorkflowExecutionResult(run_id="r", status=RunStatus.COMPLETED)
    assert not hasattr(ledger, "lessons")     # learning context stays on the envelope
    assert not hasattr(ledger, "outcome")
    assert ledger.task_results == []


def test_execution_cannot_construct_the_ledger():
    """F2-3: execution.py must not know WorkflowExecutionResult (Execution cannot make a ledger)."""
    src = (REPO_ROOT / "anima" / "constitution" / "execution.py").read_text(encoding="utf-8")
    assert "WorkflowExecutionResult" not in src
    # and ExecutionRuntime produces ExecutionResult, never the ledger
    rt = ExecutionRuntime()
    plan = derive_plan(
        Goal(target="x", success_criterion="y", horizon="weeks"),
        Drive(),
        PlannerContext(),
    )
    out = rt.run(plan, ExecutionContext())
    assert isinstance(out, ExecutionResult)
    assert not isinstance(out, WorkflowExecutionResult)


def test_reflection_envelope_path_is_unaffected():
    """F2-4: reflect() still consumes the RFC-0107 ExecutionResult envelope, unchanged."""
    env = ExecutionResult(
        plan_ref="plan#understand-X#run-1",
        status=ExecutionStatus.SUCCESS,
        outcome="executed plan for 'understand X'",
        lessons=["carried 2 task(s) toward 'understand X'"],
    )
    rr = reflect(env)
    assert rr.result_ref != ""
    assert any(r["kind"] == "episode" for r in rr.records)
    assert any(r["kind"] == "semantic" for r in rr.records)


def test_runtime_orchestration_authority_is_unchanged():
    """F2-5: the orchestrator constructs no ledger — the Workflow Authority owns it."""
    src = (
        REPO_ROOT / "anima" / "runtime" / "orchestrator.py"
    ).read_text(encoding="utf-8")
    assert "WorkflowExecutionResult" not in src
    assert "build_workflow_execution_result" not in src


def test_ledger_builder_is_a_pure_function_of_the_run():
    """F2-1c: build_workflow_execution_result writes no run-state and adds no lifecycle."""
    import ast
    import inspect
    import textwrap

    fn = ast.parse(
        textwrap.dedent(inspect.getsource(build_workflow_execution_result))
    ).body[0]
    if ast.get_docstring(fn) is not None:
        fn.body = fn.body[1:]

    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                assert not (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                ), f"ledger builder must not assign attributes: {ast.unparse(target)}"
        if isinstance(node, ast.AugAssign):
            assert not (
                isinstance(node.target, ast.Attribute)
                and isinstance(node.target.value, ast.Name)
            ), f"ledger builder must not assign attributes: {ast.unparse(node.target)}"
    # it may READ the run read-only (run.id / run.status / run.task_states), but it must
    # never CALL a lifecycle transition (those are the Workflow Authority's writes)
    calls = [
        node.func.attr
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert not (set(calls) & {"_transition", "report_task_outcome", "start_run",
                              "cancel_run", "abort_run", "suspend_run", "resume_run"})
