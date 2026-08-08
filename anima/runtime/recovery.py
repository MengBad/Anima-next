"""WorkflowRun recovery orchestration (F4.4) — restore a crashed run's lifecycle.

Implements the F4.3 recovery contract:

    store.load(old_run_id) -> resolve active Plan -> workflow.start_run(new_plan)
        -> reconcile old task_states (DONE/FAILED/SKIPPED replay, PENDING kept)
        -> return the recovered run

Design rules (frozen in F4.3 / F4.4 authorization):

- **Restore authority boundary.** Recovery lives in the runtime layer and ONLY drives the
  Workflow through its public API (``start_run`` / ``report_task_outcome`` / ``read_run``).
  It never touches ``_registry``, never writes ``run.status`` / ``task_states`` /
  ``current_task_id``, never calls ``_transition``. Every state change is the Workflow
  Authority's own act; recovery only *replays recorded outcomes*.
- **PlanRef rebinding is implicit.** The old run's ``plan_ref`` points at the crashed
  Plan. Recovery resolves an ACTIVE Plan for the same ``goal_ref`` (via the Planning
  Authority) and calls ``start_run(new_plan)`` — the new run's ``plan_ref`` is naturally
  bound to the new Plan (F-02 snapshot). No manual rebinding is needed.
- **Task resume semantics.** Replaying a recorded outcome is NOT re-executing the task:
  ``DONE`` / ``FAILED`` / ``SKIPPED`` outcomes are re-reported as-is (their recorded
  ``attempts``/``result``/``error`` preserved); ``PENDING`` tasks are left alone and stay
  awaiting real execution. F-04 then concludes the run naturally.
- **Duplicate-execution guard (contract-level).** Recovery never dispatches a task — it
  only re-reports already-recorded outcomes. PENDING work is left to the caller to
  resume; whether a re-dispatch is safe is a real-executor (F5) idempotency question,
  not this module's.
- **No silent fabrication.** If no ACTIVE Plan exists for the goal, recovery raises
  ``ConstitutionViolation`` — it does not invent a Plan (recompute is the Planning
  Authority's job via ``request_plan``, which needs Goal/Drive/Context the recovery
  entry point does not hold; the caller supplies a Plan by resolving it first).
"""
from __future__ import annotations

from anima.constitution.errors import ConstitutionViolation
from anima.constitution.state import (
    PlanStatus,
    TaskRunStatus,
    TaskState,
    WorkflowRun,
)
from anima.constitution.workflow import WorkflowInterface
from anima.constitution.planning_interface import PlanningInterface
from anima.runtime.persistence import WorkflowRunStore


def _resolve_active_plan(
    planning: PlanningInterface, goal_ref: str
):
    """Find the ACTIVE Plan a recovered run must restart from (F4.3 rebinding).

    Recovery needs a Plan the Workflow can consume (``start_run`` requires ``active``).
    The old run's ``goal_ref`` is the join key: the Planning Authority's ``list_plans``
    returns that Goal's Plans, of which exactly the ``active`` one is consumable.

    Raises ``ConstitutionViolation`` when none exists — the caller (or a future F4.4+
    layer) is responsible for recomputing via ``planning.request_plan`` with the Goal /
    Drive / Context; this entry point does not hold them and must not fabricate a Plan.
    """
    plans = [
        p for p in planning.list_plans(goal_ref)
        if p.status is PlanStatus.ACTIVE
    ]
    if not plans:
        raise ConstitutionViolation(
            f"recover_run: no ACTIVE Plan for goal_ref {goal_ref!r}; "
            f"recompute the Plan via the Planning Authority before recovery "
            f"(recovery does not fabricate Plans)"
        )
    return plans[0]


def recover_run(
    store: WorkflowRunStore,
    workflow: WorkflowInterface,
    planning: PlanningInterface,
    persisted_run: WorkflowRun,
) -> WorkflowRun:
    """Restore a crashed run's lifecycle from its persisted snapshot (F4.4).

    Steps (each authority owner noted):

    1. ``store.load`` — the caller has already loaded ``persisted_run`` from disk
       (the store is the ONLY recovery source; Plan / ExecutionResult /
       WorkflowExecutionResult never enter recovery, F3 freeze #1).
    2. ``planning.list_plans(goal_ref)`` — resolve the ACTIVE Plan (rebinding, see
       ``_resolve_active_plan``).
    3. ``workflow.start_run(plan)`` — a fresh run is born (pending -> running, GQ9);
       its ``plan_ref`` is bound to the NEW Plan.
    4. Reconcile: for each OLD task_state —
       ``DONE``/``FAILED``/``SKIPPED`` are re-reported via ``report_task_outcome``
       (recorded payload preserved; the Workflow records, judges F-04, and transitions);
       ``PENDING`` tasks are skipped and stay awaiting real execution.
    5. ``workflow.read_run`` — the recovered run (F-04 has already concluded it if every
       replayed task resolved it; otherwise it stays running with pending work).

    This is replay, not re-execution: no task is dispatched here, and no outcome is
    fabricated for PENDING work. The Workflow Authority remains the only writer of
    run-state; recovery only feeds it recorded facts through its public API.
    """
    plan = _resolve_active_plan(planning, persisted_run.goal_ref)

    # F7.2 — Recovery Identity Preservation: carry each task's ORIGINAL attempts into the
    # new run so the execution identity (source_run_id, task_id, attempts) survives the
    # crash boundary. The run_id component is seeded by the caller as ``source_run_id``
    # (resume_coordinator / orchestrator); the attempts component is carried here. Together
    # they let ``_resume_run`` rebuild the IDENTICAL idempotency key and HIT the pre-crash
    # ledger fact instead of desyncing to a fresh attempt=0 and duplicating a non-idempotent
    # provider's side effect (F7 C1). The ledger key shape is unchanged, so no on-disk
    # migration is required (ledger compatibility strategy).
    carry_attempts = {s.task_id: s.attempts for s in persisted_run.task_states}

    new_run = workflow.start_run(plan, carry_attempts=carry_attempts)

    for old_state in persisted_run.task_states:
        if old_state.status in (
            TaskRunStatus.DONE,
            TaskRunStatus.FAILED,
            TaskRunStatus.SKIPPED,
        ):
            # Re-report the recorded outcome as-is: replay, never re-execute.
            replayed = TaskState(
                task_id=old_state.task_id,
                status=old_state.status,
                attempts=old_state.attempts,
                result=dict(old_state.result),
                error=old_state.error,
            )
            try:
                workflow.report_task_outcome(new_run.id, old_state.task_id, replayed)
            except ConstitutionViolation:
                # The Workflow concluded the run (F-04) on an earlier replay — obey.
                break
        # PENDING: intentionally skipped — stays awaiting real execution.

    return workflow.read_run(new_run.id)
