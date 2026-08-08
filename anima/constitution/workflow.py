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

Slice scope (D2 + D3 + D4 + D5 + D5b). D2 established the authority *skeleton*; D3 opened the Plan
boundary; D4 closed the Execution boundary and the run's completion semantics; D5 closes the
lifecycle itself:
- The run registry (in-process dict) + the §7.1 run-state transition table (``_LEGAL_TRANSITIONS``)
  + the single private ``_transition`` funnel (the only write site for run status).
- All eight RFC-0003 §6 operations implemented against the canonical state vocabulary from D1.
- ``start_run`` is the **sole run-activator** (Study GQ9): it creates the run in ``pending`` and
  immediately transitions ``pending``→``running`` (RFC-0003 §7.1 "``pending`` → ``running``
  (start_run)"). A ``pending`` run may still be ``abort_run``-ed before any task dispatches
  (F-03 entry-state exit) — the abort path is exercised against a ``pending`` run held in the
  registry.
- **D3 — the Plan→run consumption boundary.** ``start_run`` takes the *resolved* canonical Plan
  (the caller dereferences the handle via the Planning Interface; the Workflow is given no standing
  pathway back into the Planning registry, so it can never re-read or re-resolve a Plan later).
  It gates eligibility (``active`` only, non-empty ``task_graph``), reduces the Plan to the opaque
  ``PlanRef`` handle + derived ``TaskState`` snapshots via the explicit ``plan_to_run_tasks``
  adapter, populates the F-01 reference fields (``goal_ref`` / ``context_ref``) — and then **drops
  the Plan**. No Plan object, no ``task_graph``, and no live reference survives into the run: the
  Plan stays transient and single-writer-owned by the Planning Authority (RFC-0002 §8 / F-02).
- **D4 — the Execution outcome boundary + F-04 completion semantics.** ``report_task_outcome``
  is the *inbound* boundary for Execution, and it splits three responsibilities that D2 left
  merged: Execution **reports**, the Authority **decides**, ``_transition`` **writes**. A report
  is copied into the run's own ``TaskState`` (via the explicit ``outcome_to_task_state`` adapter,
  so the reported object never becomes run-state) and then the Authority re-derives the F-04
  verdict from the *whole* task set — ``_judge_run_completion`` is handed the run and nothing
  else, so a report can never name a run outcome. ``report_task_outcome`` itself never calls
  ``_transition``: an Execution consumer holding only that entry point cannot start, suspend,
  resume, cancel or abort a run.
- **D5 — lifecycle closure.** F-04 is stated over ``running``, so outcomes reported while a run
  is ``suspended`` are recorded but not judged (D4, deliberate). That left the machine open:
  after ``resume_run`` nobody re-asked the question, so a run whose last Task finished during the
  pause returned to ``running`` with no event left that could ever conclude it. ``resume_run``
  now re-asks the same ``_judge_run_completion`` once the run is judgeable again. No transition
  and no authority is added — the run moves ``suspended``→``running`` (this operation's own §7.1
  edge) and only then, if the verdict says so, ``running``→``completed``/``failed`` (F-04's own
  edge). It never jumps from ``suspended`` to a terminal state; ``_LEGAL_TRANSITIONS`` is
  unchanged, the verdict still lives in exactly one function, and ``_transition`` is still the
  one write site.

- **D5b — Plan consumption invariant hardening (GAP-3, found in D5 analysis).** The D5 closure
  exposed a consumption-boundary hole: a Plan whose ``task_graph`` carries a duplicate or empty
  ``Task.id`` is consumed without complaint, but the resulting run can never satisfy F-04 — a
  duplicate id yields two ``TaskState`` slots the second of which is unreachable (lookup resolves
  to the first), and an empty id can be reported against by nothing. ``_validate_consumable_plan``
  now rejects non-canonical ``Task.id`` values at the *same* boundary D3 built for
  eligibility (``active`` status + non-empty ``task_graph``), and before any registry mutation.
  This is a *Workflow consumption invariant* — the Workflow validates the Plan it consumes and
  refuses a bad one, exactly as it refuses a ``draft``/empty one. It is **not** a Plan-schema
  change (``state.py`` untouched), **not** a Planning-Authority responsibility
  (the ``Planning Interface`` and ``derive_plan`` are untouched), and **not** a second ``Task`` model: the
  Plan object is never written.

Deferred (cross-boundary, deliberately NOT decided here):
- The retry / branching / rollback engines behind F-04's second clause — this slice implements
  the *criterion* only (see ``_recovery_path_exists``), never a dispatcher.
- ExecutionResult hand-back to the Planning Authority, and Execution runtime wiring (nothing here
  calls, or is called by, ``execution.py``) — a later slice.
- ~~Plan *canonicality* invariants at the consumption boundary~~ — **resolved in D5b.**
  ``Task.id`` must be unique and non-empty within a ``task_graph``; a duplicate id yields two
  ``TaskState`` slots the second of which is unreachable (lookup resolves to the first), so the
  run can never satisfy F-04. This is a *Workflow consumption invariant* (the Workflow refuses a
  non-canonical Plan at ``start_run``), not a Plan-schema change, not a Planning-Authority duty,
  and not a second ``Task`` model — ``state.py`` / the ``Planning Interface`` / ``derive_plan`` are all
  untouched.
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
    WorkflowExecutionResult,
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
#: reached only through the F-04 judgment (``_judge_run_completion``, D4).
_LEGAL_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    # pending --start_run (run-activator)--> running | --abort_run--> cancelled
    RunStatus.PENDING: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    # running --suspend_run--> suspended | --(F-04)--> completed | --(F-04)--> failed
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


# ---------------------------------------------------------------------------
# D4 — the Execution outcome boundary + F-04 completion semantics (RFC-0003 §6 / §7)
# ---------------------------------------------------------------------------
#: Task states that count as *satisfactorily resolved* for the F-04 completion criterion
#: (RFC-0003 §7: "``running`` → ``completed`` when every ``TaskState.status`` is ``done`` or
#: ``skipped``" — a skipped Task is a resolved Task, not an unfinished one). Encoded as data so
#: the criterion lives in one auditable place rather than inside a conditional.
_SATISFACTORILY_RESOLVED: frozenset[TaskRunStatus] = frozenset(
    {TaskRunStatus.DONE, TaskRunStatus.SKIPPED}
)

#: Task states that END a Task's run-local life. A resolved Task's outcome is final: it is not
#: re-opened by a second report — there is no retry / re-dispatch engine here (see
#: ``_recovery_path_exists``), so accepting one would silently rewrite recorded history.
_TERMINAL_TASK_STATES: frozenset[TaskRunStatus] = frozenset(
    {TaskRunStatus.DONE, TaskRunStatus.FAILED, TaskRunStatus.SKIPPED}
)


def outcome_to_task_state(task_id: str, outcome: TaskState) -> TaskState:
    """Explicit, auditable mapping: an Execution-reported outcome -> the run's own ``TaskState``.

    The *inbound* mirror of ``plan_to_run_tasks``: the run's two intake boundaries (Planning at
    ``start_run``, Execution at ``report_task_outcome``) each cross through one explicit adapter.
    Every field is assigned by name — no ``TaskState(**outcome.__dict__)``, no ``vars(outcome)``,
    no dict unpacking — so the conversion stays reviewable as the §5 contract evolves.

    Two properties matter more than the copying itself:

    1. **The reported object never becomes run-state.** A fresh ``TaskState`` is constructed and
       the recorded payload is copied into a new dict, so the reporter keeps no live handle into
       the run. Execution reports *values*; it does not get to hold — and later mutate — run-state
       (RFC-0003 §6: only the Workflow Interface writes run-state).
    2. **The slot's identity belongs to the Authority, not the reporter.** ``task_id`` is taken
       from the run's own task set (the slot the caller already verified), never from the payload,
       so a report can neither re-label a Task nor introduce one the consumed Plan never had.
    """
    return TaskState(
        task_id=task_id,
        status=outcome.status,
        attempts=outcome.attempts,
        result=dict(outcome.result),
        error=outcome.error,
    )


def build_workflow_execution_result(run: WorkflowRun) -> WorkflowExecutionResult:
    """The run-terminal ledger (RFC-0003 §5): a snapshot of ``run.task_states`` (F2).

    Produced from a terminal ``WorkflowRun`` by the Workflow Authority (RFC-0003 §5: the
    Workflow reports its run's terminal output to the Planning Authority / audit). It is the
    **ledger** counterpart to Execution's ``ExecutionResult`` envelope (RFC-0107): two
    authorities, two producers, two consumers — this one is the Workflow's lifecycle truth.

    Deliberately a pure function of the run, NOT a second state machine:

    - **Snapshot, not handle.** Each ``TaskState`` is copied by name (``result`` into a fresh
      dict), so the ledger holds no live reference into ``run.task_states`` — mutating the
      ledger cannot rewrite run-state, and mutating the run after the fact cannot rewrite the
      ledger. Mirrors ``outcome_to_task_state`` / ``plan_to_run_tasks`` snapshot discipline.
    - **Reads terminal run-state only.** It does not transition anything: the run is already
      terminal when this is called; construction adds no lifecycle event and no status write
      (the single ``_transition`` write site is untouched).
    - **No authority grant.** It builds a report; it does not decide the verdict — ``status``
      is read from the run's own terminal state, ``summary``/``completed_at`` are echoed, and
      the F-04 judgment that concluded the run stays the Workflow's alone.

    ``plan_ref`` is the opaque ``PlanRef`` snapshot (F-02) rendered as a string, so the ledger
    carries no Plan handle and no path back into the Planning registry.
    """
    return WorkflowExecutionResult(
        run_id=run.id,
        plan_ref=f"{run.plan_ref.plan_id}@{run.plan_ref.contract_version}",
        goal_ref=run.goal_ref,
        status=run.status,
        task_results=[
            TaskState(
                task_id=state.task_id,
                status=state.status,
                attempts=state.attempts,
                result=dict(state.result),
                error=state.error,
            )
            for state in run.task_states
        ],
        summary=f"{run.status.value} after {len(run.task_states)} task(s)",
        completed_at=run.completed_at,
    )


#: Audit label for the terminal transition an F-04 verdict produces (D5). Names the *judgment*,
#: not whichever occasion asked for it — since D5 both ``report_task_outcome`` and ``resume_run``
#: ask, and one verdict reached two ways is still one verdict.
_F04_JUDGMENT = "F-04 judgment (RFC-0003 §7)"


def _recovery_path_exists(run: WorkflowRun, task_state: TaskState) -> bool:
    """F-04's second clause: does a failed Task still have a way forward? (RFC-0003 §7)

    The criterion is "``running`` → ``failed`` when a Task reports ``failed`` **and** no
    branching / rollback / remaining-retry path remains". This slice implements the *criterion*,
    not the engines: there is no branching evaluator, no rollback, and no retry dispatcher here,
    so no recovery path can exist and a reported failure is unrecoverable by construction.

    It is a named predicate rather than an inlined ``False`` so the deferred clause stays visible
    and has exactly one place to grow when those engines arrive. Note ``WorkflowRun.retry_policy``
    is METADATA ONLY (§5): reading it as an instruction would quietly turn this interface into a
    retry engine, which it is not.
    """
    return False


class WorkflowInterface:
    """The Infrastructure Workflow Authority's stable API surface (RFC-0003 §6).

    Owns run-state lifecycle transitions (§7) and the in-process run registry.
    **Single-writer**: only this interface mutates run *state* (O1) — every status
    transition funnels through ``_transition``. The run is the runtime form of a Plan
    (RFC-0003); this interface consumes the Plan read-only via the opaque ``plan_ref``
    handle and never writes Plan state. ``report_task_outcome`` records a Task's outcome
    and then the Authority *itself* re-derives the F-04 verdict from the whole task set —
    Execution reports, the Authority decides, ``_transition`` writes; the Planning Authority
    retains ownership of the Plan, and Memory (RFC-0004) writes Memory.
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

    def _validate_consumable_plan(self, plan: Plan) -> None:
        """Workflow consumption boundary (D3 eligibility + D5b canonicality).

        Rejects — without touching the Plan — anything the Workflow may not consume. Two
        gates, both applied **before any registry mutation**:

        1. **Eligibility (D3, unchanged).** The Plan must be ``active`` (``draft`` is
           uncommitted; ``superseded``/``deprecated`` are discarded and never revived,
           RFC-0106 V5; the Workflow never self-activates — that is ``activate_plan``) and
           carry a non-empty ``task_graph`` (a run with no work can never terminate).
        2. **Canonicality (D5b).** Every ``Task.id`` must be **non-empty** and **unique**
           within the ``task_graph``. A duplicate id yields two ``TaskState`` slots the
           second of which is unreachable (lookup resolves to the first), so the run can
           never satisfy F-04; an empty id can be reported against by nothing. The Workflow
           *validates the Plan it consumes and refuses a non-canonical one* — it does not
           repair the Plan or claim any Plan-writer authority. This is a **Workflow
           consumption invariant**, not a Plan-schema change (``state.py`` untouched), not a
           Planning-Authority responsibility (the ``Planning Interface`` and
           ``derive_plan`` are untouched), and not a second ``Task`` model.

        Refusal, not repair: a non-consumable Plan raises ``ConstitutionViolation`` and no
        run is created, no registry entry is left behind, and the Plan is returned unchanged.
        The Workflow may read a Plan to decide, never to fix it.
        """
        # --- eligibility (D3) ---
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
        # --- canonicality (D5b: Workflow consumption invariant) ---
        seen: set[str] = set()
        for task in plan.task_graph:
            if not task.id:
                raise ConstitutionViolation(
                    "start_run requires every Task to have a non-empty id: "
                    "a Task without an id cannot be tracked or reported against (F-04)"
                )
            if task.id in seen:
                raise ConstitutionViolation(
                    f"start_run requires unique Task.id values within a Plan; duplicate "
                    f"{task.id!r} makes the run's task set unresolvable (F-04 can never be satisfied)"
                )
            seen.add(task.id)

    def _require_task_state(self, run: WorkflowRun, task_id: str) -> int:
        """Locate the run-local slot a report may write, or refuse it (D4).

        A run's task set is fixed at ``start_run`` from the consumed Plan's ``task_graph`` and
        never grows: the Plan is gone (F-02) and the Workflow cannot invent Tasks. So an unknown
        ``task_id`` is not an insert — it is a report about work this run does not own, and it is
        refused rather than absorbed.

        Returns the slot *index*, so the update replaces in place and the run's task order (which
        mirrors the consumed ``task_graph`` order) survives every report.
        """
        for index, state in enumerate(run.task_states):
            if state.task_id == task_id:
                return index
        known = ", ".join(sorted(s.task_id for s in run.task_states)) or "<none>"
        raise ConstitutionViolation(
            f"report_task_outcome: unknown task_id {task_id!r} for run {run.id!r}; "
            f"a run's task set is fixed at start_run (known: {known})"
        )

    def _judge_run_completion(self, run: WorkflowRun) -> WorkflowRun:
        """The Authority's F-04 verdict on a run — derived from run-state, never reported (D4).

        Deliberately takes **only the run**: not the reported outcome, not the ``task_id``.
        Execution cannot name a run outcome; it can only change one ``TaskState``, after which the
        Authority re-derives the verdict from the *whole* task set. That asymmetry is the boundary
        (RFC-0003 §6: Execution reports; only the Workflow Interface writes run-state).

        The RFC-0003 §7 criterion, applied in order:

        1. a Task is ``failed`` and no recovery path remains -> ``running`` → ``failed``
        2. every Task is ``done`` or ``skipped``             -> ``running`` → ``completed``

        Only a ``running`` run is judged. A ``suspended`` run is left alone on purpose: a straggler
        report arriving after ``suspend_run`` records its outcome but must not resurrect the run's
        lifecycle behind the Authority's back. Terminal runs never reach here — a report into one
        is refused upstream. ``start_run`` guarantees a non-empty task set, so the "every Task"
        test is never vacuously true.

        Because the judgment is skipped while ``suspended``, it must be re-run when the run
        becomes judgeable again — otherwise a run whose last outcome arrived during the pause is
        never judged at all (the D5 closure gap). Hence two occasions call this: a Task reporting
        (D4) and ``resume_run`` (D5). Both hand over the same thing — the run — and get the same
        verdict function; there is no second judgment, only a second moment at which the one
        judgment is asked.
        """
        if run.status is not RunStatus.RUNNING:
            return run
        if any(
            state.status is TaskRunStatus.FAILED and not _recovery_path_exists(run, state)
            for state in run.task_states
        ):
            return self._conclude(run, RunStatus.FAILED, operation=_F04_JUDGMENT)
        if all(state.status in _SATISFACTORILY_RESOLVED for state in run.task_states):
            return self._conclude(run, RunStatus.COMPLETED, operation=_F04_JUDGMENT)
        return run

    def _conclude(
        self, run: WorkflowRun, verdict: RunStatus, *, operation: str
    ) -> WorkflowRun:
        """Carry an F-04 verdict out: the terminal transition plus its timestamp (D4).

        The verdict is *decided* in ``_judge_run_completion`` and *written* by ``_transition`` —
        this only carries it across, so both terminal exits (``completed`` / ``failed``) stamp
        ``completed_at`` the same way ``cancel_run`` / ``abort_run`` do.

        ``operation`` is the audit label ``_transition`` puts in its refusal message, supplied by
        the caller rather than hard-coded here (D5). It is a label, never a second decision: the
        verdict is always the judgment's, and ``permitted_from`` stays pinned to ``running``
        whatever the caller passes — this remains a pass-through to the single write funnel.

        The label is deliberately occasion-*neutral*. Since D5 the judgment has two occasions —
        a Task reporting (D4) and a run resuming (D5) — but it is one verdict, over one task set,
        by one Authority; ``"report_task_outcome (F-04)"`` was accurate only while the first
        occasion was the only one. Naming the *judgment* rather than its trigger keeps the trail
        true for both without threading a caller identity through
        ``_judge_run_completion(self, run)``, whose signature is itself the D4 boundary (the
        judgment is handed the run and nothing else).
        """
        self._transition(
            run,
            verdict,
            operation=operation,
            permitted_from=(RunStatus.RUNNING,),
        )
        run.completed_at = _now_iso()
        return run

    # ---- §6 operations ----
    def start_run(
        self,
        plan: Plan,
        *,
        carry_attempts: "dict[str, int] | None" = None,
    ) -> WorkflowRun:
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

        ``carry_attempts`` (optional, recovery-only) — a ``{task_id: attempts}`` map letting a
        recovered run preserve each task's pre-crash attempt count (F7.2 Recovery Identity
        Preservation). Omit it for a normal start; the canonical zero-attempts begin applies.

        Raises ``ConstitutionViolation`` if the Plan is not consumable (see
        ``_validate_consumable_plan``); in that case no run is created.
        """
        self._validate_consumable_plan(plan)
        task_states = plan_to_run_tasks(plan)
        # F7.2 — attempt persistence: a recovery caller may carry each task's original
        # attempts into the new run so the execution identity (run_id, task_id, attempts)
        # survives a crash boundary. Without this, a PENDING task that had attempts > 0 at
        # crash would be reset to 0, desyncing the idempotency key on resume and risking a
        # duplicate external side effect. Default None preserves the canonical zero-attempts
        # start (every freshly started run begins each task at attempt 0).
        if carry_attempts:
            for ts in task_states:
                carried = carry_attempts.get(ts.task_id)
                if carried is not None:
                    ts.attempts = carried
        run = WorkflowRun(
            id=uuid.uuid4().hex,
            contract_version=WORKFLOWRUN_CONTRACT_VERSION,
            # Snapshot handle, NOT a live Plan reference (F-02).
            plan_ref=PlanRef(plan_id=plan.id, contract_version=plan.contract_version),
            goal_ref=plan.goal_ref,        # F-01 reference population (opaque ref, read-only)
            status=RunStatus.PENDING,
            task_states=task_states,  # derived snapshots (optionally attempt-carried), never live task_graph
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
        """Execution reports a single Task's result — the inbound boundary (RFC-0003 §6, D4).

        Three separated responsibilities, in order, of which this method owns only the first:

        1. **record** — the reported outcome is copied into the run's own ``TaskState`` slot
           (``outcome_to_task_state``); the reported object itself never becomes run-state. Since
           D3 seeds one ``pending`` ``TaskState`` per consumed Plan Task, a report *replaces* that
           seeded entry in place — the task set neither grows nor is reordered.
        2. **decide** — the Authority re-derives the F-04 verdict from the whole task set
           (``_judge_run_completion``, which is handed the run and nothing else).
        3. **write** — any resulting lifecycle move goes through ``_transition``, the single
           run-status write site (O1).

        A report is therefore never a transition *request*: this method does not call
        ``_transition``, and an Execution consumer holding only this entry point cannot start,
        suspend, resume, cancel or abort a run. It is refused outright — leaving the run untouched,
        never partially applied — when the run is already terminal (its record is closed; §7.1
        gives a terminal run no outgoing transition), when ``task_id`` is not part of the run's
        fixed task set, or when that Task is already resolved (a resolved outcome is final; this
        slice has no re-open path).
        """
        run = self._require(run_id)
        if run.status in _TERMINAL_STATES:
            raise ConstitutionViolation(
                f"report_task_outcome: run {run_id!r} is {run.status.value!r} (terminal); "
                f"a concluded run's record is closed to further reports"
            )
        index = self._require_task_state(run, task_id)
        recorded = run.task_states[index]
        if recorded.status in _TERMINAL_TASK_STATES:
            raise ConstitutionViolation(
                f"report_task_outcome: task {task_id!r} of run {run_id!r} is already "
                f"{recorded.status.value!r}; a resolved Task's outcome is final"
            )
        # Record (run-state content, Authority-owned — not a status write), then let the
        # Authority judge. Execution's payload lands in a slot; it never lands on the run.
        run.task_states[index] = outcome_to_task_state(task_id, outcome)
        return self._judge_run_completion(run)

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

        **Then it re-asks the F-04 judgment (D5 — lifecycle closure).** Outcomes reported while
        the run was ``suspended`` are recorded but deliberately not judged (F-04 is stated over
        ``running``, and a straggler must not move the lifecycle behind the Authority's back).
        Resuming is the moment that backlog becomes judgeable — and the only such moment, because
        the *other* occasion, a Task reporting, is unreachable for work that already resolved
        during the pause. Without this the state machine does not close: a run whose last Task
        finished while suspended would return to ``running`` with nothing left that could ever
        conclude it, and sit there permanently — semantically over, structurally unfinished.

        This adds no transition and no authority. ``suspended``→``running`` is this operation's
        own §7.1 edge; any terminal move that follows is ``running``→``completed``/``failed``,
        F-04's own edge, decided by the same ``_judge_run_completion`` and written by the same
        ``_transition``. The run passes *through* ``running``; it never jumps from ``suspended``
        to a terminal state, so ``_LEGAL_TRANSITIONS`` and ``_conclude``'s
        ``permitted_from=(RUNNING,)`` are untouched. If work is still outstanding the judgment
        returns the run unchanged and it simply resumes.
        """
        run = self._transition(
            self._require(run_id),
            RunStatus.RUNNING,
            operation="resume_run",
            permitted_from=(RunStatus.SUSPENDED,),
        )
        return self._judge_run_completion(run)

    def cancel_run(self, run_id: str, reason: str) -> WorkflowRun:
        """Terminate a running/suspended run (user abort / Goal cancelled, RFC-0003 §6/§7.1).

        Transitions ``running``/``suspended``→``cancelled`` (terminal). ``cancelled`` is an
        *external* termination — the work was called off, not attempted and lost. A Task-level
        failure is a different verdict and takes a different route: the F-04 judgment terminates
        the run as ``failed`` (D4). Only the Workflow Authority may cancel, and Execution — whose
        only entry point is ``report_task_outcome`` — never reaches this operation (§7.1).
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
