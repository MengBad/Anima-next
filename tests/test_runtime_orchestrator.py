"""Phase E4 — Runtime Orchestrator tests (anima/runtime/orchestrator.py).

Validates the minimal closed loop:

    Plan -> start_run -> WorkflowRun -> ExecutionRuntime -> ExecutionResult
         -> execution_result_to_task_states -> report_task_outcome -> judged terminal

Boundary guarantees tested:
- Normal loop: SUCCESS ExecutionResult -> WorkflowRun COMPLETED (via the real authorities).
- Failure loop: FAILED ExecutionResult -> WorkflowRun FAILED.
- Authority guard: the orchestrator assigns no run/Plan status, calls no private
  lifecycle internals (_transition/_registry/_judge_run_completion), and imports no
  PlanStatus/RunStatus writing surface.
- Dependency guard: runtime -> constitution is one-way; constitution never imports
  runtime, and runtime never reaches into legacy (agent/ or core/workflows).
"""
from __future__ import annotations

import ast
import inspect
import pathlib
import textwrap

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
)
from anima.constitution.workflow import WorkflowInterface
from anima.runtime.orchestrator import PlanOrchestrator

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


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


def test_orchestrator_success_loop_concludes_the_run_completed():
    """Normal loop: SUCCESS ExecutionResult -> every TaskState DONE -> WorkflowRun COMPLETED."""
    planning = PlanningInterface()
    plan_id = _active_plan(planning)

    orch = PlanOrchestrator(
        planning=planning,
        workflow=WorkflowInterface(),
        execution=ExecutionRuntime(),
    )
    final = orch.run_plan(plan_id, ExecutionContext(capabilities=["knowledge"]))

    assert final.status is RunStatus.COMPLETED
    assert final.completed_at != ""
    assert all(s.status.value == "done" for s in final.task_states)


def test_orchestrator_failure_loop_concludes_the_run_failed():
    """Failure loop: FAILED ExecutionResult -> every TaskState FAILED -> WorkflowRun FAILED."""
    planning = PlanningInterface()
    plan_id = _active_plan(planning)

    class _FailingRuntime(ExecutionRuntime):
        def run(self, plan, context=None):  # noqa: D102
            from anima.constitution.state import ExecutionResult, ExecutionStatus

            return ExecutionResult(
                plan_ref="plan#understand-X#run-fail",
                status=ExecutionStatus.FAILED,
                outcome="boom",
                lessons=["failed"],
            )

    orch = PlanOrchestrator(
        planning=planning,
        workflow=WorkflowInterface(),
        execution=_FailingRuntime(),
    )
    final = orch.run_plan(plan_id, ExecutionContext(capabilities=["knowledge"]))

    assert final.status is RunStatus.FAILED
    assert final.completed_at != ""
    # F-04 semantics: the FIRST failed report concludes the run (running -> failed); the
    # remaining snapshots are never fed (a terminal record is closed) — so the first Task
    # is recorded FAILED and later Tasks stay PENDING. The Orchestrator obeyed the refusal.
    assert final.task_states[0].status.value == "failed"
    assert all(s.status.value == "pending" for s in final.task_states[1:])


def test_orchestrator_returns_the_judged_terminal_run_not_a_copy():
    """The returned run is the Workflow's own registry record (same id), read back via read_run."""
    planning = PlanningInterface()
    plan_id = _active_plan(planning)
    wf = WorkflowInterface()
    orch = PlanOrchestrator(planning=planning, workflow=wf, execution=ExecutionRuntime())

    final = orch.run_plan(plan_id, ExecutionContext(capabilities=["knowledge"]))

    assert wf.read_run(final.id) is final  # same object in the registry


def test_orchestrator_assigns_no_run_or_plan_status():
    """Authority guard (mechanical): the orchestrator never writes `.status` on anything.

    Docstrings are stripped via AST so prose naming the forbidden tokens (e.g. a
    docstring that says "no ``_transition``") cannot false-positive the guard — the
    assertion targets what the code DOES, not what the docs mention.
    """
    tree = ast.parse(
        (REPO_ROOT / "anima" / "runtime" / "orchestrator.py").read_text(encoding="utf-8")
    )

    # strip every docstring (Module / Class / Function) before unparsing
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node)
            if doc is not None:
                node.body = node.body[1:]

    executable = ast.unparse(tree)

    assert ".status = " not in executable
    assert "RunStatus." not in executable
    assert "PlanStatus." not in executable
    # no private lifecycle internals may be named in executable code
    for token in ("_transition", "_registry", "_judge_run_completion"):
        assert token not in executable, f"orchestrator must not reach internals: {token!r}"


def test_orchestrator_public_surface_is_only_run_plan():
    """The glue exposes one operation; it is not a second authority with extra methods."""
    public = {
        name
        for name in vars(PlanOrchestrator)
        if not name.startswith("_") and callable(getattr(PlanOrchestrator, name))
    }
    assert public == {"run_plan"}


def test_orchestrator_dependency_guard_one_way_runtime_to_constitution():
    """Dependency guard: runtime imports constitution; constitution NEVER imports runtime;
    runtime never reaches into legacy (agent/ or core/workflows)."""
    runtime_files = list((REPO_ROOT / "anima" / "runtime").rglob("*.py"))
    assert runtime_files, "runtime package must exist"

    # constitution must not import runtime (one-way dependency)
    for py in (REPO_ROOT / "anima" / "constitution").rglob("*.py"):
        src = py.read_text(encoding="utf-8")
        assert "anima.runtime" not in src, f"constitution must not import runtime: {py}"

    # runtime must not reach into legacy layers
    for py in runtime_files:
        src = py.read_text(encoding="utf-8")
        for token in ("anima.agent", "anima.core.workflows", "core.workflows"):
            assert token not in src, f"runtime must not reach legacy: {token!r} in {py}"


def test_orchestrator_run_plan_never_branches_on_run_outcome():
    """The glue must not judge: no conditional that decides the run's terminal state exists
    in run_plan (the F-04 verdict is the Workflow Authority's alone)."""
    src = textwrap.dedent(inspect.getsource(PlanOrchestrator.run_plan))
    # strip the docstring so prose mentioning status cannot false-positive
    fn = ast.parse(src).body[0]
    if ast.get_docstring(fn) is not None:
        fn.body = fn.body[1:]

    for node in ast.walk(fn):
        if isinstance(node, ast.If):
            cond = ast.unparse(node.test)
            for probe in ("result.status", "final.status", "run.status", "all(", "any("):
                assert probe not in cond, (
                    f"orchestrator must not branch on run outcome: {probe!r} in {cond!r}"
                )
