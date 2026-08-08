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


class PlanOrchestrator:
    """Carry an ACTIVE Plan through Planning -> Workflow -> Execution -> Workflow.

    Holds references to the three authorities (all consumed through their public API)
    and nothing else: no run handle it may write, no registry, no lifecycle authority.
    """

    def __init__(
        self,
        planning: PlanningInterface | None = None,
        workflow: WorkflowInterface | None = None,
        execution: ExecutionRuntime | None = None,
    ) -> None:
        self._planning = planning or PlanningInterface()
        self._workflow = workflow or WorkflowInterface()
        self._execution = execution or ExecutionRuntime()

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
           judge, it obeys the Authority's refusal).
        7. ``workflow.read_run(run.id)``         — the judged terminal run is returned.

        The Orchestrator NEVER decides the run's fate: step 6 hands each TaskState to the
        Workflow and lets ``_judge_run_completion`` conclude the run. No ``if result.status``
        branching on run outcome exists here — the aggregate verdict only selects the
        projection target inside the pure adapter, and the terminal decision is the
        Authority's.
        """
        plan: Plan = self._planning.read_plan(plan_id)

        run = self._workflow.start_run(plan)

        result = self._execution.run(plan, context)

        current_run = self._workflow.read_run(run.id)
        task_ids = [state.task_id for state in current_run.task_states]

        states = execution_result_to_task_states(result, task_ids)

        for state in states:
            try:
                self._workflow.report_task_outcome(run.id, state.task_id, state)
            except ConstitutionViolation:
                # The Workflow Authority concluded the run (F-04) on an earlier report —
                # a terminal run's record is closed to further reports (RFC-0003 §7.1).
                # That refusal IS the Authority's verdict; the Orchestrator does not judge
                # or override it, it simply stops feeding snapshots into a closed record.
                break

        return self._workflow.read_run(run.id)
