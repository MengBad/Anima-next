"""Runtime Orchestration layer — the glue that connects the constitution authorities.

This package sits ABOVE the Constitution Layer (anima/constitution) and BELOW the
legacy agent/user-facing layers. It is deliberately NOT a Constitution Authority:
it owns no state, writes no run/Plan status, and holds no lifecycle authority. It
only calls the public APIs of the three authorities it orchestrates:

    PlanningInterface  (read_plan)     — read an ACTIVE Plan
    WorkflowInterface  (start_run, read_run, report_task_outcome) — run lifecycle
    ExecutionRuntime   (run)           — carry the Plan, emit an aggregate ExecutionResult

Boundary rules (E3 design / E4 slice):
- Plan appears exactly twice: Planning -> Orchestrator (read), Orchestrator -> Execution
  (carry). It NEVER flows WorkflowRun -> Plan (F-02: the run holds only the opaque PlanRef).
- The Orchestrator NEVER judges success/failure: no `if result.status == ...` branching on
  run outcome, no terminal decision. The F-04 judgment belongs to the Workflow Authority
  (report_task_outcome -> _judge_run_completion -> _transition).
- The Orchestrator is glue, not an authority: it calls only public APIs, never `_transition`
  / `_registry` / `_judge_run_completion`, and never assigns `.status` on anything.
- One-way dependency: runtime -> constitution. Nothing in constitution may import runtime.
"""
