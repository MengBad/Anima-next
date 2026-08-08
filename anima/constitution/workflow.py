"""Workflow Interface — the Infrastructure Workflow Authority's stable API (RFC-0003 §6 / §7).

Realizes the *interface* the Workflow Authority owns for run-state. It is the **sole** writer
of run-state (single-writer rule O1, RFC-0106 §8 E4 / carried forward from RFC-0002 §7.1):
the in-process run registry and every lifecycle transition (``pending``→``running``,
``running``→``suspended``/``completed``/``failed``/``cancelled``, ``pending``→``cancelled``,
``suspended``→``running``) are performed *only* through this interface.

Authority boundary (RFC-0003 §4.1 / §6):
- The Workflow Interface owns run-state. It is the only writer of ``WorkflowRun.status``.
- It consumes a Plan **read-only** (RFC-0003 §1.2 / F-02) — ``start_run`` reads an ``active``
  Plan once, at the consumption boundary, and never writes it: not its content, not its runtime
  state. Plan lifecycle stays owned solely by the Planning Interface (RFC-0002).
- It does NOT execute capabilities, does NOT write Memory (RFC-0004 territory), does NOT persist
  (persistence / recovery is a later slice, RFC-0003 §8 G4 — the registry here is in-process).

Slice scope (D2 + D3). D2 established the authority *skeleton*; D3 opened the Plan boundary:
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
- **D3 — the Plan→run consumption boundary.** ``start_run`` takes the *resolved* canonical Plan
  (the caller dereferences the handle via the Planning Interface; the Workflow is given no standing
  pathway back into the Planning registry, so it can never re-read or re-resolve a Plan later).
  It gates eligibility (``active`` only, non-empty ``task_graph``), reduces the Plan to the opaque
  ``PlanRef`` handle + derived ``TaskState`` snapshots via the explicit ``plan_to_run_tasks``
  adapter, populates the F-01 reference fields (``goal_ref`` / ``context_ref``) — and then **drops
  the Plan**. No Plan object, no ``task_graph``, and no live reference survives into the run: the
  Plan stays transient and single-writer-owned by the Planning Authority (RFC-0002 §8 / F-02).

Deferred (cross-boundary, deliberately NOT decided here):
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
    Plan,
    PlanRef,
    PlanStatus,
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
# D3 — the Plan consumption boundary (RFC-0003 §1.2 / §6 start_run, F-01 / F-02)
# ---------------------------------------------------------------------------
#: The ONLY Plan runtime state a run may be started from (RFC-0002 §7.1 "consume" edge /
#: RFC-0003 §6: "Workflow never self-activates; it consumes an already-``active`` Plan").
#: A ``draft`` Plan is not yet committed; ``superseded`` / ``deprecated`` are discarded and are
#: never revived (RFC-0106 V5). Encoded as data so the gate is one auditable place.
_CONSUMABLE_PLAN_STATUS: frozenset[PlanStatus] = frozenset({PlanStatus.ACTIVE})


def plan_to_run_tasks(plan: Plan) -> list[TaskState]:
    """Explicit, auditable mapping: canonical ``Plan.task_graph`` -> run-local ``TaskState``s.

    The **only** conversion from the Planning Authority's representation into run-state, and the
    reason the run never needs the Plan again. Mirrors ``task_to_execution_step`` (T4.1): every
    field is assigned by name — no ``TaskState(**task.__dict__)``, no ``vars(task)``, no dict
    unpacking — so the conversion stays reviewable as the §5 Plan contract evolves.

    What crosses the boundary is deliberately minimal: only ``Task.id``. Title / intent /
    ``capability_need`` / ``depends_on`` / ``estimates`` stay on the Planning side — the run
    tracks *state*, not Plan content (RFC-0003 §5). Each ``TaskState`` is a freshly constructed
    snapshot at its initial value (``pending``, zero attempts, no recorded result or error): it
    is never an alias of, nor a live reference back into, the Plan's ``Task`` node (F-02).

    Note on "no value yet": the D1 §5 contract types ``result`` as ``dict`` and ``error`` as
    ``str``, so their un-set representation is the empty value, not ``None`` — the adapter does
    not widen the contract's declared types.
    """
    return [
        TaskState(
            task_id=task.id,
            status=TaskRunStatus.PENDING,
            attempts=0,
            result={},
            error="",
        )
        for task in plan.task_graph
    ]


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

    def _assert_plan_consumable(self, plan: Plan) -> None:
        """Read-only eligibility gate for the Plan a run is about to be started from (D3).

        Rejects — without touching the Plan — anything the Workflow may not consume:

        1. **Not ``active``** — ``draft`` is uncommitted; ``superseded``/``deprecated`` are
           discarded and never revived (RFC-0106 V5). The Workflow never self-activates a Plan
           (that is ``activate_plan``, the Planning Authority's transition): it *refuses*.
        2. **Empty ``task_graph``** — a Plan with nothing to carry yields a run with no
           reachable terminal state; there is no work whose outcome could ever complete it.

        Refusal, not repair: an ineligible Plan raises ``ConstitutionViolation`` and no run is
        created, no registry entry is left behind, and the Plan is returned unchanged. This is
        the boundary's whole point — the Workflow may read a Plan to decide, never to fix it.
        """
        if plan.status not in _CONSUMABLE_PLAN_STATUS:
            allowed = "/".join(sorted(s.value.upper() for s in _CONSUMABLE_PLAN_STATUS))
            raise ConstitutionViolation(
                f"start_run requires an {allowed} Plan, got {plan.status.value!r}: "
                f"the Workflow consumes an already-activated Plan and never activates one"
            )
        if not plan.task_graph:
            raise ConstitutionViolation(
                "start_run requires a Plan with a non-empty task_graph: "
                "a run with no tasks can never reach a terminal state"
            )

    # ---- §6 operations ----
    def start_run(self, plan: Plan) -> WorkflowRun:
        """Create AND activate a run for an ACTIVE Plan (RFC-0003 §6/§7.1, F-01/F-02).

        The Plan crosses the authority boundary here exactly once, **read-only**. The caller
        dereferences the Plan handle (the Planning Interface's ``read_plan``) and hands the
        resolved Plan in; ``start_run`` reads it, reduces it, and lets go of it:

        - ``plan_ref``   — the opaque ``PlanRef(plan_id, contract_version)`` *snapshot* handle.
          The run holds no Plan object and no ``task_graph`` (F-02); it cannot re-read, revive
          or write the Plan, and persisting a run would never persist a Plan.
        - ``task_states``— derived snapshots built by the explicit ``plan_to_run_tasks`` adapter,
          never aliases of the Plan's ``Task`` nodes.
        - ``goal_ref`` / ``context_ref`` — F-01 reference population, copied as opaque strings.
          (``budget_ref`` / ``schedule_ref`` have no source in the §5 Plan contract and stay
          empty rather than being invented here.)

        The Plan itself is left **untouched**: its runtime state stays owned by the Planning
        Interface (RFC-0002 O1), and consumption is not a Plan transition (§7.1 ``consume``
        leaves the Plan ``active``; it ends by being discarded, not by a consumer).

        ``start_run`` is the **sole run-activator** (Study GQ9): the run is created in
        ``pending`` and immediately transitions ``pending``→``running`` (RFC-0003 §7.1
        "``pending`` → ``running`` (start_run)"). A ``pending`` run may still be
        ``abort_run``-ed before any task dispatches (F-03 entry-state exit) — the abort
        path is exercised against a ``pending`` run held in the registry.

        Raises ``ConstitutionViolation`` if the Plan is not consumable (see
        ``_assert_plan_consumable``); in that case no run is created.
        """
        self._assert_plan_consumable(plan)
        run = WorkflowRun(
            id=uuid.uuid4().hex,
            contract_version=WORKFLOWRUN_CONTRACT_VERSION,
            # Snapshot handle, NOT a live Plan reference (F-02).
            plan_ref=PlanRef(plan_id=plan.id, contract_version=plan.contract_version),
            goal_ref=plan.goal_ref,        # F-01 reference population (opaque ref, read-only)
            status=RunStatus.PENDING,
            task_states=plan_to_run_tasks(plan),  # derived snapshots, never the live task_graph
            context_ref=plan.context_ref,  # F-01 reference population (opaque ref, read-only)
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

        Read-only: listing never transitions a run. ``goal_ref`` is the F-01 reference
        copied from the consumed Plan at ``start_run`` (D3) — an opaque string, never a Goal
        handle, so listing cannot reach the Goal state either.
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
        by ``start_run`` (GQ9). The recorded ``TaskState`` is the run-local snapshot; since D3
        seeds one ``pending`` ``TaskState`` per consumed Plan Task, a report *replaces* that
        seeded entry rather than growing the list.
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
