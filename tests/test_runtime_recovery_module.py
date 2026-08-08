"""Phase F4.7b — RecoveryModule startup wiring tests.

Validates the application-level wiring: RecoveryModule (anima/runtime/recovery_module.py)
satisfies the core Module lifecycle and delegates restore() to recover_all.

1. RecoveryModule is a Module (lifecycle contract): name set, init/persist/shutdown
   are no-op-safe, restore() runs recovery.
2. restore() calls recover_all: non-terminal snapshots are recovered through the
   full chain Module.restore() -> recover_all() -> recover_run() -> start_run + replay.
3. Terminal runs are not recovered.
4. Single-run failure does not block other recoveries.
5. Authority boundary: the module never touches _registry / run.status / task_states,
   and never bypasses Workflow public API.
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
from anima.core.module import Module
from anima.core.persistence import PersistenceProvider
from anima.runtime.persistence import WorkflowRunStore
from anima.runtime.recovery_module import RecoveryModule

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


def _fresh_module(td: str, planning=None) -> RecoveryModule:
    store = WorkflowRunStore(PersistenceProvider(data_dir=td))
    wf = WorkflowInterface()
    pl = planning or PlanningInterface()
    return RecoveryModule(store=store, workflow=wf, planning=pl)


# ---------------------------------------------------------------------------
# Test 1 — Module lifecycle contract
# ---------------------------------------------------------------------------
def test_recovery_module_is_a_module():
    """RecoveryModule satisfies the core Module contract: name + lifecycle methods."""
    m = _fresh_module(tempfile.mkdtemp())
    assert isinstance(m, Module)
    assert m.name  # non-empty name (WebUI/registry uniqueness)
    # lifecycle methods exist and are awaitable
    import inspect

    assert inspect.iscoroutinefunction(m.init)
    assert inspect.iscoroutinefunction(m.restore)
    assert inspect.iscoroutinefunction(m.persist)
    assert inspect.iscoroutinefunction(m.shutdown)


# ---------------------------------------------------------------------------
# Test 2 — restore() delegates to recover_all (full recovery chain)
# ---------------------------------------------------------------------------
def test_restore_recovers_non_terminal_runs():
    """restore() runs the full chain: Module.restore() -> recover_all() -> recover_run()
    -> start_run(new_plan) + report replay. Non-terminal snapshots are recovered."""
    import asyncio

    with tempfile.TemporaryDirectory() as td:
        planning = PlanningInterface()
        _active_plan_id(planning)
        store = WorkflowRunStore(PersistenceProvider(data_dir=td))
        store.save(_snapshot("run#pending-1", RunStatus.PENDING))
        store.save(_snapshot("run#running-2", RunStatus.RUNNING))
        m = RecoveryModule(store=store, workflow=WorkflowInterface(), planning=planning)

        asyncio.run(m.restore())

    # recovery happened: both runs were reborn via start_run (rebound plan_ref)
    # the module does not retain recovered runs publicly, but the chain ran without error
    # and the workflow registry now holds the recovered runs — verify via the module's
    # own workflow handle (the Workflow Authority owns the registry)
    wf = m._workflow  # module holds the workflow it injected (public-API-only access)
    runs = wf.list_runs("goal#understand-X")
    assert len(runs) == 2  # two recovered runs now live in the Workflow registry


# ---------------------------------------------------------------------------
# Test 3 — terminal runs not recovered
# ---------------------------------------------------------------------------
def test_restore_skips_terminal_runs():
    """completed/failed/cancelled snapshots are not recovered: no new run is born."""
    import asyncio

    with tempfile.TemporaryDirectory() as td:
        planning = PlanningInterface()
        _active_plan_id(planning)
        store = WorkflowRunStore(PersistenceProvider(data_dir=td))
        store.save(_snapshot("run#done", RunStatus.COMPLETED))
        store.save(_snapshot("run#failed", RunStatus.FAILED))
        store.save(_snapshot("run#cancelled", RunStatus.CANCELLED))
        m = RecoveryModule(store=store, workflow=WorkflowInterface(), planning=planning)

        asyncio.run(m.restore())

    assert m._workflow.list_runs("goal#understand-X") == []  # nothing recovered


# ---------------------------------------------------------------------------
# Test 4 — single-run failure isolation
# ---------------------------------------------------------------------------
def test_restore_isolates_single_run_failure():
    """One run's recovery failure does not block the others (delegated isolation)."""
    import asyncio

    with tempfile.TemporaryDirectory() as td:
        planning = PlanningInterface()
        _active_plan_id(planning)
        store = WorkflowRunStore(PersistenceProvider(data_dir=td))
        store.save(_snapshot("run#good", RunStatus.RUNNING))      # goal#understand-X: has Plan
        orphan = _snapshot("run#orphan", RunStatus.RUNNING)
        orphan.goal_ref = "goal#no-plan"                          # no active Plan -> fails
        store.save(orphan)
        m = RecoveryModule(store=store, workflow=WorkflowInterface(), planning=planning)

        # must not raise: orphan fails, good recovers
        asyncio.run(m.restore())

    assert len(m._workflow.list_runs("goal#understand-X")) == 1   # good recovered
    assert m._workflow.list_runs("goal#no-plan") == []            # orphan failed, no run


# ---------------------------------------------------------------------------
# Test 5 — authority boundary (AST)
# ---------------------------------------------------------------------------
def test_recovery_module_has_no_authority_write_surface():
    """RecoveryModule never writes run state: it delegates to recover_all (public API).
    AST guard: no _registry, no _transition, no run.status=, no task_states=."""
    src = (REPO_ROOT / "anima" / "runtime" / "recovery_module.py").read_text(encoding="utf-8")
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
    # it delegates to the runtime recovery entry point
    assert "recover_all(" in executable


def test_cli_wiring_does_not_import_core_into_runtime():
    """Layering guard: recovery_module.py must not import anima.core behavior — the ONLY
    legitimate core import is the Module ABC (from anima.core.module) for the contract.
    core stays the host; runtime never depends on core behavior."""
    src = (REPO_ROOT / "anima" / "runtime" / "recovery_module.py").read_text(encoding="utf-8")
    # the Module base class is the one legitimate core import
    assert "from anima.core.module import Module" in src
    # nothing else from core may be imported
    for token in ("from anima.core.persistence", "from anima.core.anima_core",
                  "from anima.core import", "import anima.core"):
        assert token not in src, f"runtime must not import core behavior: {token!r}"
