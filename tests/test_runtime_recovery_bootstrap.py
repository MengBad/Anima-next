"""Phase F4.6a — runtime recovery bootstrap tests (anima/runtime/bootstrap.py).

Validates recover_all(store, workflow, planning):

1. Enumerate persisted WorkflowRun snapshots (via store.persistence.data_dir,
   workflow_runs/*.json — no store API change, no private access).
2. Filter terminal runs (completed/failed/cancelled skipped).
3. Delegate each non-terminal run to recover_run (bootstrap never re-implements
   recovery logic).
4. Failure isolation: a single run's recovery failure does not block the rest
   (returns the successes, logs the failures).
5. Authority boundary: bootstrap never writes run state directly (AST guard).
"""
from __future__ import annotations

import ast
import pathlib
import tempfile

from anima.constitution.planning_interface import PlanningInterface
from anima.constitution.state import (
    Drive,
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
from anima.runtime.bootstrap import recover_all
from anima.runtime.persistence import WorkflowRunStore

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


def _snapshot(run_id: str, status: RunStatus) -> WorkflowRun:
    return WorkflowRun(
        id=run_id,
        contract_version="1.0.0",
        plan_ref=PlanRef(plan_id="plan#OLD", contract_version="1.0.0"),
        goal_ref="goal#understand-X",
        status=status,
        task_states=[
            TaskState(task_id="t1", status=TaskRunStatus.DONE, attempts=1,
                      result={"read": "X"}, error=""),
            TaskState(task_id="t2", status=TaskRunStatus.PENDING, attempts=0,
                      result={}, error=""),
        ],
        current_task_id="t2",
        checkpoint_ref="t1",
        retry_policy={},
        schedule_ref="",
        budget_ref="",
        context_ref="ctx#understand-X",
        created_at="2026-08-08T00:00:00+00:00",
        completed_at="2026-08-08T00:00:01+00:00" if status in (
            RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED
        ) else "",
    )


def _fresh_store(td: str) -> WorkflowRunStore:
    return WorkflowRunStore(PersistenceProvider(data_dir=td))


# ---------------------------------------------------------------------------
# Test 1 — enumerate + recover non-terminal runs
# ---------------------------------------------------------------------------
def test_recover_all_recovers_non_terminal_runs():
    """All non-terminal snapshots are recovered; the returned list contains them."""
    with tempfile.TemporaryDirectory() as td:
        planning = PlanningInterface()
        _active_plan_id(planning)
        store = _fresh_store(td)
        store.save(_snapshot("run#pending-1", RunStatus.PENDING))
        store.save(_snapshot("run#running-2", RunStatus.RUNNING))

        recovered = recover_all(store, WorkflowInterface(), planning)

    # recovery births NEW runs (fresh uuid ids) — assert by rebound plan_ref + non-terminal
    assert len(recovered) == 2
    assert all(r.plan_ref.plan_id != "plan#OLD" for r in recovered)   # rebound to NEW plan
    assert all(r.status in (RunStatus.PENDING, RunStatus.RUNNING,
                            RunStatus.SUSPENDED, RunStatus.COMPLETED) for r in recovered)
    assert len({r.id for r in recovered}) == 2  # two distinct recovered runs


# ---------------------------------------------------------------------------
# Test 2 — terminal snapshots skipped
# ---------------------------------------------------------------------------
def test_recover_all_skips_terminal_runs():
    """completed/failed/cancelled snapshots are skipped: no new run is born for them."""
    with tempfile.TemporaryDirectory() as td:
        planning = PlanningInterface()
        _active_plan_id(planning)
        store = _fresh_store(td)
        store.save(_snapshot("run#done-1", RunStatus.COMPLETED))
        store.save(_snapshot("run#failed-2", RunStatus.FAILED))
        store.save(_snapshot("run#cancelled-3", RunStatus.CANCELLED))

        recovered = recover_all(store, WorkflowInterface(), planning)

    assert recovered == []  # nothing non-terminal existed => nothing recovered


# ---------------------------------------------------------------------------
# Test 3 — failure isolation
# ---------------------------------------------------------------------------
def test_recover_all_isolates_single_run_failure():
    """A failing run does not block others: successes are returned, failure is isolated."""
    with tempfile.TemporaryDirectory() as td:
        planning = PlanningInterface()  # empty registry: NO active Plan for any goal
        store = _fresh_store(td)
        store.save(_snapshot("run#a", RunStatus.RUNNING))   # will fail (no active Plan)
        store.save(_snapshot("run#b", RunStatus.RUNNING))   # will fail too (no active Plan)

        # with no active Plan both fail => empty result, but no exception escapes
        recovered = recover_all(store, WorkflowInterface(), planning)

    assert recovered == []


def test_recover_all_returns_successes_when_some_fail():
    """Mixed: run A fails (no Plan), run B succeeds (Plan exists) => returns [B]."""
    with tempfile.TemporaryDirectory() as td:
        planning = PlanningInterface()
        _active_plan_id(planning)          # Plan exists for goal#understand-X
        store = _fresh_store(td)
        # run#a uses a DIFFERENT goal_ref that has no active Plan -> its recovery fails
        store.save(_snapshot("run#a", RunStatus.RUNNING))     # goal#understand-X: OK (has Plan)
        orphan = _snapshot("run#b", RunStatus.RUNNING)
        orphan.goal_ref = "goal#no-plan"                       # no Plan for this goal
        store.save(orphan)

        recovered = recover_all(store, WorkflowInterface(), planning)

    assert len(recovered) == 1                              # B failed, A recovered
    assert recovered[0].goal_ref == "goal#understand-X"     # the successful one is A's goal


# ---------------------------------------------------------------------------
# Test 4 — authority boundary (AST)
# ---------------------------------------------------------------------------
def test_bootstrap_has_no_authority_write_surface():
    """bootstrap.py must never write run state — it delegates to recover_run (public API).

    AST guard over the executable code (docstrings stripped): no `_registry`, no
    `_transition`, no `.status =`, no `task_states =`; it may call `recover_run`
    (the runtime recovery entry point) and `store.load` / store persistence scan.
    """
    src = (REPO_ROOT / "anima" / "runtime" / "bootstrap.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            if ast.get_docstring(node) is not None:
                node.body = node.body[1:]
    executable = ast.unparse(tree)

    assert "_registry" not in executable
    assert "_transition" not in executable
    assert ".status = " not in executable
    assert "task_states = " not in executable
    assert "run.status = " not in executable

    # it delegates to recover_run — the one legitimate recovery entry point
    assert "recover_run(" in executable
    # and scans the store's directory via the public persistence handle (no private access)
    assert "data_dir" in executable
