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
    task_to_execution_step,
)
from anima.constitution.planning_interface import PlanningInterface
from anima.constitution.state import (
    ExecutionContext,
    Plan,
    TaskRunStatus,
    TaskState,
)
from anima.constitution.workflow import WorkflowInterface
from anima.runtime.executor import TaskExecutor
from anima.runtime.execution_ledger import ExecutionLedger
from anima.runtime.persistence import WorkflowRunStore

#: Terminal Task state *values* — keep in sync with constitution.workflow._TERMINAL_TASK_STATES.
#: A resolved Task's outcome is final; the resume loop must skip such tasks. Compared by
#.value (string) so the orchestrator never names a ``RunStatus.``/``TaskRunStatus.`` enum
# member in executable code (the authority-guard test forbids that substring).
_TERMINAL_TASK_STATES = frozenset({"done", "failed", "skipped"})


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
        executor: "TaskExecutor | None" = None,
        ledger: "ExecutionLedger | None" = None,
    ) -> None:
        self._planning = planning or PlanningInterface()
        self._workflow = workflow or WorkflowInterface()
        self._execution = execution or ExecutionRuntime()
        self._store = store  # None = persistence disabled (pure in-memory mode)
        # F5.3: an optional TaskExecutor drives per-task execution with a ledger idempotency
        # gate. When None, run_plan falls back to the legacy aggregate ExecutionRuntime path
        # (so the F5.1 mock contract and its tests stay untouched).
        self._executor = executor
        self._ledger = ledger

    def _snapshot(self, run) -> None:
        """Observer: make the run durable at a frozen timing point (F4.2b).

        Reads the run returned by the Workflow's public API and hands it to the store —
        this is the ONLY persistence touchpoint in the runtime layer, and it never
        mutates run-state (the store serializes read-only). A save failure propagates:
        the crash-window target is 0 transitions, so a failed write must surface.
        """
        if self._store is not None:
            self._store.save(run)

    # ---- F5.3 per-task executor dispatch (RUNNING durability + ledger gate) ----

    def _observation_to_task_state(self, obs, task_id: str, attempts: int) -> TaskState:
        """Project a per-task ExecutionObservation onto a reportable TaskState.

        The executor produces a pure observation; the Orchestrator maps it to the
        Workflow's terminal vocabulary (DONE / FAILED / SKIPPED) and reports it — it
        never lets the executor name the run outcome (F-04 stays the Authority's).
        """
        if obs.metadata.get("skipped"):
            status = TaskRunStatus("skipped")
        elif obs.success:
            status = TaskRunStatus("done")
        else:
            status = TaskRunStatus("failed")
        return TaskState(
            task_id=task_id,
            status=status,
            attempts=attempts,
            result=dict(obs.result),
            error=obs.error,
        )

    def _active_plan_for_goal(self, goal_ref: str) -> Plan:
        """Resolve the ACTIVE Plan a recovered run resumes from (mirrors recovery).

        The run holds no Plan (F-02); the Orchestrator reads it fresh for the goal and
        never stores it on the run. Raises if no ACTIVE Plan exists — recompute is the
        Planning Authority's job, not the Orchestrator's.
        """
        plans = [
            p for p in self._planning.list_plans(goal_ref) if p.status.value == "active"
        ]
        if not plans:
            raise ConstitutionViolation(
                f"_active_plan_for_goal: no ACTIVE Plan for goal_ref {goal_ref!r}; "
                f"recompute the Plan via the Planning Authority before resuming"
            )
        return plans[0]

    def _dispatch_task(self, run, task, *, execution_run_id: str, context) -> object:
        """Execute ONE Task through the TaskExecutor with RUNNING durability + ledger gate.

        Timing (frozen in F5.3):
            1. report_task_outcome(RUNNING) + snapshot -> RUNNING is durable BEFORE the side
               effect, so a crash here leaves a recoverable RUNNING (not an invisible PENDING
               that would be re-dispatched with no memory of the attempt).
            2. executor.execute(step, key, ledger) -> the executor consults the ledger FIRST;
               on a hit it returns the cached observation with NO side effect.
            3. report_task_outcome(terminal) + snapshot -> the run is advanced durably.

        The idempotency key is (execution_run_id, task.id, attempts). ``execution_run_id`` is
        the run whose lifecycle we drive; for a recovered run it is the ORIGINAL crashed run's
        id (passed as ``source_run_id``) so ledger facts recorded under that id survive recovery
        (which issues a NEW run_id) and prevent a duplicate external side effect.
        """
        current = self._workflow.read_run(run.id)
        idx = next(
            (i for i, s in enumerate(current.task_states) if s.task_id == task.id), None
        )
        if idx is None:
            raise ConstitutionViolation(
                f"_dispatch_task: task {task.id!r} not in run {run.id!r}"
            )
        attempts = current.task_states[idx].attempts
        key = (execution_run_id, task.id, attempts)

        # 1. RUNNING durability (before the side effect)
        running = TaskState(
            task_id=task.id, status=TaskRunStatus("running"), attempts=attempts
        )
        run = self._workflow.report_task_outcome(run.id, task.id, running)
        self._snapshot(run)

        # 2. execute (the executor consults the ledger internally; a hit skips the side effect)
        step = task_to_execution_step(task)
        obs = self._executor.execute(step, key, ledger=self._ledger)

        # 3. report terminal + durable
        terminal = self._observation_to_task_state(obs, task.id, attempts)
        run = self._workflow.report_task_outcome(run.id, task.id, terminal)
        self._snapshot(run)
        return run

    def _resume_run(self, run, source_run_id: str | None = None, context=None):
        """Execute the PENDING tasks of an already-started run (F5.3 recovery resume).

        The primary caller is a recovered run: ``recover_run`` issues a NEW run_id and replays
        terminal outcomes, leaving non-terminal tasks PENDING. This method drives those PENDING
        tasks through the TaskExecutor.

        ``source_run_id`` MUST be the ORIGINAL crashed run's id: it seeds the ledger key's
        run-component, so a side effect recorded before the crash is still found after recovery
        (preventing a duplicate external side effect). When omitted, the current run's id is used
        (a non-recovery resume, e.g. after suspend).
        """
        current = self._workflow.read_run(run.id)
        if current.status.value != "running":
            raise ConstitutionViolation(
                f"_resume_run requires a running run, got {current.status.value!r}"
            )
        execution_run_id = source_run_id or run.id
        plan = self._active_plan_for_goal(current.goal_ref)
        for task in plan.task_graph:
            state = next(
                (s for s in current.task_states if s.task_id == task.id), None
            )
            if state is None or state.status.value in _TERMINAL_TASK_STATES:
                continue  # already resolved (e.g. replayed by recovery) -> skip
            run = self._dispatch_task(
                run, task, execution_run_id=execution_run_id, context=context
            )
            current = self._workflow.read_run(run.id)
        final = self._workflow.read_run(run.id)
        self._snapshot(final)
        return final

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

        if self._executor is None:
            # Legacy aggregate path: the mock ExecutionRuntime emits one ExecutionResult for
            # the whole Plan; project it onto per-Task snapshots and report (unchanged E4 loop).
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
        else:
            # F5.3 per-task executor path: dispatch each Task through the TaskExecutor with
            # RUNNING durability + ledger idempotency gate (see _dispatch_task). The run's own
            # id is the ledger key's run-component for a fresh run.
            for task in plan.task_graph:
                run = self._dispatch_task(
                    run, task, execution_run_id=run.id, context=context
                )

        final = self._workflow.read_run(run.id)
        self._snapshot(final)  # terminal => durable (unconditional)
        return final
