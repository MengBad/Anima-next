"""Workflow Interface — the Infrastructure Workflow Authority's stable API (RFC-0003 §6 / §7).

Realizes the *interface* the Workflow Authority owns for run-state. It is the **sole** writer
of run-state (single-writer rule O1, RFC-0106 §8 E4 / carried forward from RFC-0002 §7.1):
the in-process run registry and every lifecycle transition (``pending``→``running``,
``running``→``suspended``/``completed``/``failed``/``cancelled``, ``pending``→``cancelled``,
``suspended``→``running``) are performed *only* through this interface.

Authority boundary (RFC-0003 §4.1 / §6):
- The Workflow Interface owns run-state. It is the only writer of ``WorkflowRun.status``.
- It consumes a Plan **read-only** (RFC-0003 §1.2 / F-02) — ``start_run`` receives only the
  opaque ``plan_ref`` handle string and never touches the Planning Authority's Plan object or
  its ``Plan.status``. Plan lifecycle stays owned solely by the Planning Interface (RFC-0002).
- It does NOT execute capabilities, does NOT write Memory (RFC-0004 territory), does NOT persist
  (persistence / recovery is a later slice, RFC-0003 §8 G4 — the registry here is in-process).

Slice scope (D2). This slice establishes the authority *skeleton* only:
- The run registry (in-process dict) + the §7.1 run-state transition table (``_LEGAL_TRANSITIONS``)
  + the single private ``_transition`` funnel (the only write site for run status).
- All eight RFC-0003 §6 operations implemented against the canonical state vocabulary from D1.
- ``start_run`` is the **sole run-activator** (Study GQ9): it creates the run in ``pending`` and
  immediately transitions ``pending``→``running`` (RFC-0003 §7.1 "``pending`` → ``running``
  (start_run)"). A ``pending`` run may still be ``abort_run``-ed before any task dispatches
  (F-03 entry-state exit) — the abort path is exercised against a ``pending`` run held in the
  registry. ``report_task_outcome`` records a Task's outcome but does NOT transition the run
  (Execution reports, the Authority decides); F-04 completion/failure (``{done,skipped}``→
  ``completed`` / unrecoverable ``failed``→``failed``) is the D4 slice, deliberately deferred.

Deferred (cross-boundary, deliberately NOT decided here):
- Plan consumption content (snapshotting ``task_graph``→``task_states``, deriving ``goal_ref``/
  ``context_ref``/``budget_ref`` per F-01) — that is the D3 slice (it reads an ``active`` Plan via
  the Planning Interface). D2 only holds the opaque ``plan_ref`` handle string.
- F-04 run-completion / failure judgment, ExecutionResult hand-back to the Planning Authority —
  D4 slice.
- run-state persistence / recovery (§8 G4) — a later slice; the registry here is in-process only.
- The Checkpoint primitive itself (§8 GQ4) is deferred to the Execution layer; ``suspend_run`` only
  records the resume anchor (``current_task_id``) as the opaque ``checkpoint_ref``.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from anima.constitution.errors import ConstitutionViolation
from anima.constitution.state import (
    PLAN_CONTRACT_VERSION,
    PlanRef,
    RunStatus,
    TaskRunStatus,
    TaskState,
    WorkflowRun,
    WORKFLOWRUN_CONTRACT_VERSION,
)


def _now_iso() -> str:
    """Construction / terminal timestamp — assigned at the moment, never by storage."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# §7 lifecycle model — the single source of transition truth (run-state)
# ---------------------------------------------------------------------------
#: States that END a run's life. A run is *terminal* — it has NO outgoing transition
#: and can never be re-suspended, re-activated, or re-cancelled.
_TERMINAL_STATES: frozenset[RunStatus] = frozenset(
    {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
)

#: The complete RFC-0003 §7.1 transition table, encoded as data so the lifecycle
#: invariant is mechanically checkable instead of being scattered across operations.
#: Any ``(from -> to)`` pair absent here is illegal and is rejected by ``_transition``.
#: ``pending``→``running`` is owned by ``start_run`` (the sole run-activator, GQ9);
#: ``pending``→``cancelled`` is the F-03 entry-state abort; running/suspended terminal
#: exits go through ``cancel_run``; completion/failure (``completed``/``failed``) are
#: reached only once D4 wires the F-04 judgment.
_LEGAL_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    # pending --start_run (run-activator)--> running | --abort_run--> cancelled
    RunStatus.PENDING: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    # running --suspend_run--> suspended | --(F-04, D4)--> completed | --(F-04, D4)--> failed
    #          | --cancel_run--> cancelled
    RunStatus.RUNNING: frozenset(
        {RunStatus.SUSPENDED, RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    # suspended --resume_run--> running | --cancel_run--> cancelled
    RunStatus.SUSPENDED: frozenset(
        {RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.COMPLETED: frozenset(),  # terminal
    RunStatus.FAILED: frozenset(),     # terminal
    RunStatus.CANCELLED: frozenset(),  # terminal
}


class WorkflowInterface:
    """The Infrastructure Workflow Authority's stable API surface (RFC-0003 §6).

    Owns run-state lifecycle transitions (§7) and the in-process run registry.
    **Single-writer**: only this interface mutates run *state* (O1) — every status
    transition funnels through ``_transition``. The run is the runtime form of a Plan
    (RFC-0003); this interface consumes the Plan read-only via the opaque ``plan_ref``
    handle and never writes Plan state. ``report_task_outcome`` records a Task's outcome
    and never transitions the run (Execution reports, the Authority decides — F-04 is D4);
    the Planning Authority retains ownership of the Plan, and Memory (RFC-0004) writes
    Memory.
    """

    def __init__(self) -> None:
        self._registry: dict[str, WorkflowRun] = {}  # in-process ONLY — persistence is a later slice

    # ---- single-writer enclosure (internal helpers) ----
    def _require(self, run_id: str) -> WorkflowRun:
        run = self._registry.get(run_id)
        if run is None:
            raise KeyError(f"WorkflowRun not found: {run_id!r}")
        return run

    def _transition(
        self,
        run: WorkflowRun,
        target: RunStatus,
        *,
        operation: str,
        permitted_from: tuple[RunStatus, ...],
    ) -> WorkflowRun:
        """The **only** place a run's runtime state is written (§7.1 single owner / O1).

        Every lifecycle move funnels through here, so "who may transition a run" has
        exactly one answer in code as it does in the RFC. Two guards, applied in order:

        1. **Operation guard** — the source states this specific operation permits
           (§6/§7.1 per-operation rule, e.g. ``suspend_run`` only from ``running``).
        2. **Table guard** — the global §7.1 transition table (defence in depth: a
           terminal run can never move, whatever an operation claims it wants).

        A rejected transition raises ``ConstitutionViolation`` and leaves the run
        untouched — an invalid transition is never a partial one.
        """
        current = run.status
        if current not in permitted_from:
            allowed = "/".join(state.value.upper() for state in permitted_from)
            raise ConstitutionViolation(
                f"{operation} requires {allowed} state, got {current.value!r}"
            )
        if target not in _LEGAL_TRANSITIONS[current]:
            raise ConstitutionViolation(
                f"illegal run transition {current.value!r} -> {target.value!r} "
                f"via {operation}: not permitted by the RFC-0003 §7.1 transition table"
                + (" (terminal state)" if current in _TERMINAL_STATES else "")
            )
        run.status = target
        return run

    # ---- §6 operations ----
    def start_run(self, plan_ref: str) -> WorkflowRun:
        """Create AND activate a run for an ACTIVE Plan (RFC-0003 §6/§7.1).

        The Plan must already be ``active`` (RFC-0002 ``activate_plan``); consumption of
        Plan *content* (snapshotting ``task_graph`` → ``task_states``, deriving
        ``goal_ref``/``context_ref``/``budget_ref`` per F-01) is the D3 slice. Here the
        Workflow receives only the opaque ``plan_ref`` handle string (F-02) and never
        touches the Plan object or its ``Plan.status``.

        ``start_run`` is the **sole run-activator** (Study GQ9): the run is created in
        ``pending`` and immediately transitions ``pending``→``running`` (RFC-0003 §7.1
        "``pending`` → ``running`` (start_run)"). A ``pending`` run may still be
        ``abort_run``-ed before any task dispatches (F-03 entry-state exit) — the abort
        path is exercised against a ``pending`` run held in the registry.
        """
        run = WorkflowRun(
            id=uuid.uuid4().hex,
            contract_version=WORKFLOWRUN_CONTRACT_VERSION,
            plan_ref=PlanRef(plan_id=plan_ref, contract_version=PLAN_CONTRACT_VERSION),
            goal_ref="",  # populated by D3 Plan consumption (F-01); empty until then
            status=RunStatus.PENDING,
            created_at=_now_iso(),
        )
        self._registry[run.id] = run
        # Sole run-activator: pending -> running (§7.1, single-writer _transition funnel).
        self._transition(
            run,
            RunStatus.RUNNING,
            operation="start_run",
            permitted_from=(RunStatus.PENDING,),
        )
        return run

    def read_run(self, run_id: str) -> WorkflowRun:
        """Return the current run-state (introspection / RFC-0004/0005 consumption).

        Read-only with respect to the lifecycle: reading never transitions a run.
        """
        return self._require(run_id)

    def list_runs(self, goal_ref: str) -> list[WorkflowRun]:
        """List runs (incl. terminal) for a Goal, for audit / RFC-0004/0005 introspection.

        Read-only: listing never transitions a run. (In D2 ``goal_ref`` is populated by
        the D3 Plan-consumption slice; until then runs share an empty ``goal_ref``.)
        """
        return [run for run in self._registry.values() if run.goal_ref == goal_ref]

    def report_task_outcome(
        self, run_id: str, task_id: str, outcome: TaskState
    ) -> WorkflowRun:
        """Execution reports a single Task's result (RFC-0003 §6).

        The Workflow Authority (this interface) records the outcome; it does **NOT**
        transition the run's lifecycle here — Execution reports, the Authority decides
        (and the F-04 completion/failure judgment is the D4 slice). So in D2 a run's
        status is unchanged by a report: a ``running`` run stays ``running`` (it was
        already activated by ``start_run``), and ``pending``→``running`` is owned solely
        by ``start_run`` (GQ9). The recorded ``TaskState`` is the run-local snapshot.
        """
        run = self._require(run_id)
        # Upsert the reported TaskState (run-state content, Authority-owned — not a status write).
        others = [t for t in run.task_states if t.task_id != task_id]
        run.task_states = others + [outcome]
        return run

    def suspend_run(self, run_id: str, reason: str) -> WorkflowRun:
        """Pause a running run and capture the resume anchor (RFC-0003 §6/§7.1).

        Transitions ``running``→``suspended``. The Checkpoint primitive itself is deferred
        (§8 GQ4); we record the resume anchor (``current_task_id``) as the opaque
        ``checkpoint_ref`` so ``resume_run`` can continue from it. Only the Workflow
        Authority may suspend.
        """
        run = self._require(run_id)
        self._transition(
            run,
            RunStatus.SUSPENDED,
            operation="suspend_run",
            permitted_from=(RunStatus.RUNNING,),
        )
        run.checkpoint_ref = run.current_task_id  # resume anchor (F-05); empty iff none set
        return run

    def resume_run(self, run_id: str) -> WorkflowRun:
        """Continue a suspended run from its checkpoint / current_task_id (RFC-0003 §6/§7.1).

        Transitions ``suspended``→``running``. Only the Workflow Authority may resume.
        """
        return self._transition(
            self._require(run_id),
            RunStatus.RUNNING,
            operation="resume_run",
            permitted_from=(RunStatus.SUSPENDED,),
        )

    def cancel_run(self, run_id: str, reason: str) -> WorkflowRun:
        """Terminate a running/suspended run (user abort / Goal cancelled, RFC-0003 §6/§7.1).

        Transitions ``running``/``suspended``→``cancelled`` (terminal). Only the Workflow
        Authority may cancel; Execution may *request* cancellation via ``report_task_outcome``
        (terminal failure) but the state transition is the Authority's (§7.1).
        """
        run = self._transition(
            self._require(run_id),
            RunStatus.CANCELLED,
            operation="cancel_run",
            permitted_from=(RunStatus.RUNNING, RunStatus.SUSPENDED),
        )
        run.completed_at = _now_iso()
        return run

    def abort_run(self, run_id: str, reason: str) -> WorkflowRun:
        """Authority abort BEFORE first task dispatch (RFC-0003 §6/§7.1, F-03).

        Transitions ``pending``→``cancelled`` (terminal) — the entry-state exit so a run can
        be aborted before any task runs (process crash before dispatch, or an aborted
        request). Only the Workflow Authority may abort; Execution never calls this (it has
        not started reporting yet).
        """
        run = self._transition(
            self._require(run_id),
            RunStatus.CANCELLED,
            operation="abort_run",
            permitted_from=(RunStatus.PENDING,),
        )
        run.completed_at = _now_iso()
        return run
