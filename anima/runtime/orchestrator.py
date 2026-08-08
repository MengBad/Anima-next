"""PlanOrchestrator — the minimal runtime glue that carries a Plan through the authorities.

E4 slice: implements the minimal closed loop

    Plan -> start_run -> WorkflowRun -> ExecutionRuntime -> ExecutionResult
         -> execution_result_to_task_states -> report_task_outcome -> judged terminal

It is glue, NOT an authority (E3 design):

- It calls ONLY public APIs of the three authorities it orchestrates.
- It never writes run-state: no ``_transition``, no ``_registry``, no ``.status =``,
  no ``PlanStatus`` / ``RunStatus`` assignment. The F-04 judgment lives solely in the
  Workflow Authority; the Orchestrator simply feeds it per-Task snapshots and lets it
  decide.
- Plan appears exactly twice: read (Planning -> Orchestrator) and carry
  (Orchestrator -> ExecutionRuntime). It is never passed WorkflowRun -> Plan (F-02).

Dependencies are injected (defaulting to fresh instances) so tests can pass real
authority objects and the glue can be exercised without touching internals.

F4.2b — persistence timing policy (frozen in the F4.2 design):
- An optional ``WorkflowRunStore`` may be injected; when present, ``run_plan`` makes the
  run durable at every frozen point: right after ``start_run``, after each
  ``report_task_outcome``, and immediately before returning a terminal run
  (terminal => durable, unconditionally).
- The save hook lives ONLY here (runtime layer) as an *observer*: the store reads the
  run returned by the Workflow's public API and serializes it. The Workflow Authority
  never learns a store exists (no storage side-effect in ``constitution``).
- A save failure is NEVER swallowed: ``store.save`` errors propagate to the caller
  (crash-window target is 0 transitions, so a failed write must surface, not pass).
- When no store is injected, ``run_plan`` behaves exactly as before (pure in-memory
  orchestration) — persistence is opt-in at this layer.
"""
from __future__ import annotations

from anima.constitution.errors import ConstitutionViolation
from anima.constitution.execution import (
    ExecutionRuntime,
    execution_result_to_task_states,
)
from anima.constitution.planning_interface import PlanningInterface
from anima.constitution.state import ExecutionContext, Plan
from anima.constitution.workflow import WorkflowInterface
from anima.runtime.persistence import WorkflowRunStore


class PlanOrchestrator:
    """Carry an ACTIVE Plan through Planning -> Workflow -> Execution -> Workflow.

    Holds references to the three authorities (all consumed through their public API)
    and nothing else: no run handle it may write, no registry, no lifecycle authority.
    An optional ``WorkflowRunStore`` is the persistence observer (F4.2b); it reads
    run-state and serializes it, never writes it.
    """

    def __init__(
        self,
        planning: PlanningInterface | None = None,
        workflow: WorkflowInterface | None = None,
        execution: ExecutionRuntime | None = None,
        store: WorkflowRunStore | None = None,
    ) -> None:
        self._planning = planning or PlanningInterface()
        self._workflow = workflow or WorkflowInterface()
        self._execution = execution or ExecutionRuntime()
        self._store = store  # None = persistence disabled (pure in-memory mode)

    def _snapshot(self, run) -> None:
        """Observer: make the run durable at a frozen timing point (F4.2b).

        Reads the run returned by the Workflow's public API and hands it to the store —
        this is the ONLY persistence touchpoint in the runtime layer, and it never
        mutates run-state (the store serializes read-only). A save failure propagates:
        the crash-window target is 0 transitions, so a failed write must surface.
        """
        if self._store is not None:
            self._store.save(run)

    def run_plan(
        self, plan_id: str, context: ExecutionContext | None = None
    ):
        """Run one ACTIVE Plan to its judged terminal state (E4 minimal loop).

        The flow, step by step (each step's authority owner is noted):

        1. ``planning.read_plan(plan_id)``       — Planning reads; Plan crosses into the
           orchestrator. (The caller is responsible for an already-``active`` Plan; the
           Workflow refuses anything else at ``start_run``.)
        2. ``workflow.start_run(plan)``          — Workflow activates the run (sole
           run-activator, GQ9): ``pending`` -> ``running``, snapshots ``task_states``.
           **F4.2b: the run is made durable immediately** (a run that was born must
           already be recoverable).
        3. ``execution.run(plan, context)``      — Execution carries the Plan read-only
           and emits the aggregate ``ExecutionResult``.
        4. ``workflow.read_run(run.id)``         — Workflow reads; the Orchestrator takes
           ``task_ids`` from the run's OWN derived set (``task_states[*].task_id``) — the
           only legal source (F-02: the run holds no Plan; task_ids must come from the run).
        5. ``execution_result_to_task_states``   — pure projection: aggregate -> per-Task
           ``TaskState`` snapshots (E1 adapter).
        6. ``workflow.report_task_outcome``      — per snapshot: Execution reports (via the
           Orchestrator), the Workflow Authority records, judges F-04, and transitions. If a
           report is refused (``ConstitutionViolation``) the run has already been concluded —
           a terminal record is closed — and the Orchestrator stops feeding (it does not
           judge, it obeys the Authority's refusal). **F4.2b: each accepted report makes
           the run durable** (progress is never silently lost).
        7. ``workflow.read_run(run.id)``         — the judged terminal run is returned.
           **F4.2b: the terminal run is made durable before returning** (terminal =>
           durable, unconditionally — a concluded run must never resurrect as running).

        The Orchestrator NEVER decides the run's fate: step 6 hands each TaskState to the
        Workflow and lets ``_judge_run_completion`` conclude the run. No ``if result.status``
        branching on run outcome exists here — the aggregate verdict only selects the
        projection target inside the pure adapter, and the terminal decision is the
        Authority's.
        """
        plan: Plan = self._planning.read_plan(plan_id)

        run = self._workflow.start_run(plan)
        self._snapshot(run)

        result = self._execution.run(plan, context)

        current_run = self._workflow.read_run(run.id)
        task_ids = [state.task_id for state in current_run.task_states]

        states = execution_result_to_task_states(result, task_ids)

        for state in states:
            try:
                run = self._workflow.report_task_outcome(run.id, state.task_id, state)
            except ConstitutionViolation:
                # The Workflow Authority concluded the run (F-04) on an earlier report —
                # a terminal run's record is closed to further reports (RFC-0003 §7.1).
                # That refusal IS the Authority's verdict; the Orchestrator does not judge
                # or override it, it simply stops feeding snapshots into a closed record.
                break
            self._snapshot(run)

        final = self._workflow.read_run(run.id)
        self._snapshot(final)  # terminal => durable (unconditional)
        return final
