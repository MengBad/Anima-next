"""Phase F4.4 — WorkflowRun recovery orchestration tests.

Validates the F4.3 recovery contract as implemented in anima/runtime/recovery.py:

    store.load(old_run_id) -> resolve Plan -> workflow.start_run(new_plan)
        -> reconcile old task_states (DONE/FAILED/SKIPPED replay, PENDING kept)
        -> return recovered run

Key F4.3 assumption under proof:
    new run (start_run) + report_task_outcome(replayed state) == restored lifecycle

Coverage:
1. report replay feasibility — a fresh run (all PENDING) accepts DONE replay, F-04 converges.
2. FAILED replay — a persisted failed task restores the run into the failed terminal,
   without writing status directly.
3. PENDING kept — pending tasks are not fabricated as done; the run stays awaiting execution.
4. PlanRef rebinding — the recovered run carries the NEW plan's ref, not the old one.
5. authority boundary — recovery.py never touches _registry / run.status / task_states /
   _transition; it only calls Workflow public APIs.
6. blocking evidence — recovery with no active Plan raises a clear error (not silent).
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
from anima.runtime.persistence import WorkflowRunStore
from anima.runtime.recovery import recover_run

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


def _persisted_run(status=RunStatus.RUNNING, task_states=None) -> WorkflowRun:
    """A WorkflowRun as it would appear on disk after a crash (mid-run snapshot)."""
    return WorkflowRun(
        id="run#old-crashed",
        contract_version="1.0.0",
        plan_ref=PlanRef(plan_id="plan#OLD", contract_version="1.0.0"),
        goal_ref="goal#understand-X",
        status=status,
        task_states=task_states or [
            TaskState(task_id="t1", status=TaskRunStatus.DONE, attempts=1,
                      result={"read": "X"}, error=""),
            TaskState(task_id="t2", status=TaskRunStatus.PENDING, attempts=0,
                      result={}, error=""),
            TaskState(task_id="t3", status=TaskRunStatus.PENDING, attempts=0,
                      result={}, error=""),
        ],
        current_task_id="t2",
        checkpoint_ref="t1",
        retry_policy={},
        schedule_ref="",
        budget_ref="",
        context_ref="ctx#understand-X",
        created_at="2026-08-08T00:00:00+00:00",
        completed_at="",
    )


def _fresh_store(td: str) -> WorkflowRunStore:
    return WorkflowRunStore(PersistenceProvider(data_dir=td))


# ---------------------------------------------------------------------------
# Test 1 — report replay feasibility (the F4.3 core assumption)
# ---------------------------------------------------------------------------
def test_report_replay_converges_f04_to_completed():
    """A fresh run (all PENDING) accepts DONE replay and F-04 naturally concludes it."""
    with tempfile.TemporaryDirectory() as td:
        planning = PlanningInterface()
        new_plan_id = _active_plan_id(planning)
        store = _fresh_store(td)
        old = _persisted_run(task_states=[
            TaskState(task_id="t1", status=TaskRunStatus.DONE, attempts=1,
                      result={"read": "X"}, error=""),
            TaskState(task_id="t2", status=TaskRunStatus.DONE, attempts=1,
                      result={"synth": True}, error=""),
            TaskState(task_id="t3", status=TaskRunStatus.DONE, attempts=1,
                      result={"present": True}, error=""),
        ])
        store.save(old)

        recovered = recover_run(store, WorkflowInterface(), planning, old)

    # replay made every replayed task DONE and F-04 concluded the run — without any
    # direct status write by the recovery code
    assert recovered.status is RunStatus.COMPLETED
    assert all(s.status is TaskRunStatus.DONE for s in recovered.task_states)
    assert recovered.plan_ref.plan_id == new_plan_id  # rebound to the NEW plan


# ---------------------------------------------------------------------------
# Test 2 — FAILED replay
# ---------------------------------------------------------------------------
def test_failed_replay_restores_terminal_failed():
    """A persisted FAILED task replays into the failed terminal via the Authority's F-04."""
    with tempfile.TemporaryDirectory() as td:
        planning = PlanningInterface()
        _active_plan_id(planning)
        store = _fresh_store(td)
        old = _persisted_run(task_states=[
            TaskState(task_id="t1", status=TaskRunStatus.FAILED, attempts=2,
                      result={}, error="boom"),
            TaskState(task_id="t2", status=TaskRunStatus.PENDING, attempts=0,
                      result={}, error=""),
        ])
        store.save(old)

        recovered = recover_run(store, WorkflowInterface(), planning, old)

    assert recovered.status is RunStatus.FAILED
    assert recovered.task_states[0].status is TaskRunStatus.FAILED
    assert recovered.completed_at != ""


# ---------------------------------------------------------------------------
# Test 3 — PENDING preserved
# ---------------------------------------------------------------------------
def test_pending_tasks_are_kept_not_fabricated():
    """PENDING tasks are not replayed; the run stays RUNNING awaiting real execution."""
    with tempfile.TemporaryDirectory() as td:
        planning = PlanningInterface()
        _active_plan_id(planning)
        store = _fresh_store(td)
        old = _persisted_run()  # t1 DONE, t2/t3 PENDING
        store.save(old)

        recovered = recover_run(store, WorkflowInterface(), planning, old)

    assert recovered.status is RunStatus.RUNNING   # t2/t3 still pending => not terminal
    assert recovered.task_states[0].status is TaskRunStatus.DONE
    assert recovered.task_states[1].status is TaskRunStatus.PENDING
    assert recovered.task_states[2].status is TaskRunStatus.PENDING


# ---------------------------------------------------------------------------
# Test 4 — PlanRef rebinding
# ---------------------------------------------------------------------------
def test_plan_ref_is_rebound_to_new_plan():
    """The recovered run references the NEW active Plan, never the old one."""
    with tempfile.TemporaryDirectory() as td:
        planning = PlanningInterface()
        new_plan_id = _active_plan_id(planning)
        store = _fresh_store(td)
        old = _persisted_run()
        assert old.plan_ref.plan_id == "plan#OLD"   # the crashed run referenced the old Plan
        store.save(old)

        recovered = recover_run(store, WorkflowInterface(), planning, old)

    assert recovered.plan_ref.plan_id == new_plan_id
    assert recovered.plan_ref.plan_id != "plan#OLD"


# ---------------------------------------------------------------------------
# Test 5 — authority boundary (AST)
# ---------------------------------------------------------------------------
def test_recovery_has_no_authority_write_surface():
    """recovery.py must never write run state directly — only call Workflow public APIs.

    AST guard over the executable code (docstrings stripped): no `_registry`,
    no `run.status =`, no `task_states =`, no `_transition`, and no direct mutation
    of the new run's fields.
    """
    src = (REPO_ROOT / "anima" / "runtime" / "recovery.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            if ast.get_docstring(node) is not None:
                node.body = node.body[1:]
    executable = ast.unparse(tree)

    assert "_registry" not in executable
    assert "_transition" not in executable
    assert "run.status = " not in executable
    assert "task_states = " not in executable
    assert ".status = " not in executable

    # it may only reach Workflow via public API calls
    for token in ("start_run", "report_task_outcome", "read_run"):
        assert token in executable  # the legitimate public surface it must use


# ---------------------------------------------------------------------------
# Test 6 — blocking evidence: no active Plan
# ---------------------------------------------------------------------------
def test_recovery_without_active_plan_raises():
    """Recovery needs a resolvable active Plan; a missing one must raise, not silently
    fabricate a run (RFC-0003 E3: recompute is the Planning side's job, and a recovery
    without a Plan cannot proceed)."""
    import pytest

    from anima.constitution.errors import ConstitutionViolation

    with tempfile.TemporaryDirectory() as td:
        planning = PlanningInterface()  # empty registry: no Plan for the goal
        store = _fresh_store(td)
        old = _persisted_run()
        store.save(old)

        with pytest.raises(ConstitutionViolation):
            recover_run(store, WorkflowInterface(), planning, old)
