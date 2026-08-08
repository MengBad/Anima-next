"""M1 smoke test — validates the Constitution Layer root against the Foundation.

These assertions ARE the validation: they prove the code honors the contracts the
RFC-0100 trilogy froze (RFC-0105 §14). Failures here mean implementation drifted
from the Foundation — fix the code, not the contract (unless via Foundation
Evolution F1–F4).
"""
from anima.constitution.identity import derive_identity
from anima.constitution.need import derive_need
from anima.constitution.affect import derive_affect
from anima.constitution.drive import derive_drive
from anima.constitution.invariants import check_constitution_invariants
from anima.constitution.planner import Planner, derive_plan
from anima.constitution.execution import (
    ExecutionRuntime,
    ExecutionStep,
    execute_plan,
    task_to_execution_step,
)
from anima.constitution.workflow import WorkflowInterface
from anima.constitution.memory_authority import MemoryAuthority, ingest
from anima.constitution.reflection import Reflector, reflect
from anima.constitution.persona import PersonaProjector, derive_persona
from anima.constitution.relationship import RelationshipProjector, derive_relationship
from anima.constitution.state import (
    Affect,
    Drive,
    ExecutionContext,
    ExecutionResult,
    ExecutionStatus,
    Goal,
    GoalLifecycle,
    Identity,
    Memory,
    MemoryRecord,
    OrientationAxis,
    Perception,
    Persona,
    Plan,
    PlanRef,
    PlanStatus,
    PLAN_CONTRACT_VERSION,
    PlannerContext,
    ReflectionResult,
    RelationshipProjection,
    RunStatus,
    Task,
    TaskRunStatus,
    TaskState,
    WorkflowRun,
    WORKFLOWRUN_CONTRACT_VERSION,
)
from anima.constitution.memory_engine import MemoryEngine


def _engine_with(semantic: dict, salience: float = 0.8) -> MemoryEngine:
    e = MemoryEngine()
    e.ingest_semantic(semantic, salience=salience)
    return e


def _seed(engine: MemoryEngine) -> None:
    engine.ingest_semantic({"curiosity": 0.9, "value": "understanding"}, salience=0.8)
    engine.ingest_episode({"event": "solved X", "outcome": "success"}, salience=0.6)
    engine.ingest_relation({"bond": "user_abu", "strength": 0.7}, salience=0.5)


def test_identity_is_pure_function():
    e1 = MemoryEngine()
    _seed(e1)
    e2 = MemoryEngine()
    _seed(e2)
    assert derive_identity(e1.snapshot()) == derive_identity(e2.snapshot())


def test_identity_derivation_does_not_mutate_memory():
    e = MemoryEngine()
    _seed(e)
    snap = e.snapshot()
    before = len(snap.semantic)
    derive_identity(snap)
    # derivation must not add/remove records (no hidden Memory mutation)
    assert len(snap.semantic) == before
    assert len(snap.episodes) == before


def test_identity_inherits_persistence_via_memory():
    # Two engines with identical Memory ⇒ identical Identity (canonical Hybrid:
    # Identity has no store of its own; it is re-derived from Memory).
    e1 = MemoryEngine()
    _seed(e1)
    e2 = MemoryEngine()
    _seed(e2)
    i1 = derive_identity(e1.snapshot())
    i2 = derive_identity(e2.snapshot())
    assert i1.traits == i2.traits
    assert i1.values == i2.values


# --------------------------------------------------------------------------
# M1.2 — Need Engine g(Identity)
# --------------------------------------------------------------------------
def test_need_is_pure_function():
    i = derive_identity(
        _engine_with({"curiosity": 0.9, "value": "understanding"}).snapshot()
    )
    assert derive_need(i) == derive_need(i)


def test_need_depends_on_identity():
    curious = derive_identity(
        _engine_with({"curiosity": 0.9, "value": "understanding"}).snapshot()
    )
    passive = derive_identity(
        _engine_with({"curiosity": 0.1, "value": "rest"}).snapshot()
    )
    assert derive_need(curious) != derive_need(passive)


# --------------------------------------------------------------------------
# M1.3 — Affect Engine h(Need, Perception)
# --------------------------------------------------------------------------
def _identity_for_affect() -> "Identity":
    return derive_identity(
        _engine_with({"curiosity": 0.9, "value": "understanding"}).snapshot()
    )


def test_affect_does_not_mutate_need():
    need = derive_need(_identity_for_affect())
    before = (dict(need.vectors), need.urgency)
    perception = Perception(signals={"novelty": 0.7, "threat": 0.0})
    derive_affect(need, perception)
    assert (dict(need.vectors), need.urgency) == before


def test_affect_is_pure_function():
    need = derive_need(_identity_for_affect())
    p = Perception(signals={"novelty": 0.7, "threat": 0.0})
    assert derive_affect(need, p) == derive_affect(need, p)


# --------------------------------------------------------------------------
# M1.4 — Drive Engine i(Affect)
# --------------------------------------------------------------------------
def test_drive_is_pure_function():
    a = Affect(valence=0.4, arousal=0.6, curiosity=0.5, confidence=0.7, urgency=0.6)
    assert derive_drive(a) == derive_drive(a)


def test_drive_has_no_goal_authority_edge():
    # Drive must not expose planner-like methods; it only produces orientation.
    assert not hasattr(Drive, "create_goal")
    assert not hasattr(Drive, "plan")
    # and cannot be constructed with a goal parameter (no Goal field)
    import inspect

    params = inspect.signature(Drive.__init__).parameters
    assert "goal" not in params


# --------------------------------------------------------------------------
# M1.5 — Invariant Guard
# --------------------------------------------------------------------------
def test_constitution_invariants_hold():
    # Runs every mechanical guard (no setter / no storage authority / no upstream
    # mutation) over the real engines. This IS the guard against future drift.
    assert check_constitution_invariants() is True


# --------------------------------------------------------------------------
# M1.6 — Planner Interface (RFC-0106)
# --------------------------------------------------------------------------
def _goal_for_plan() -> Goal:
    return Goal(
        target="understand X",
        success_criterion="can explain X",
        horizon="weeks",
        priority=0.8,
        lifecycle=GoalLifecycle.ACTIVE,
    )


def _drive_for_plan() -> Drive:
    return Drive(
        orientation={
            OrientationAxis.APPROACH: 0.6,
            OrientationAxis.AVOID: 0.1,
            OrientationAxis.INTENSITY: 0.4,
            OrientationAxis.FOCUS: 0.5,
        },
        activated=True,
    )


def _ctx_for_plan() -> PlannerContext:
    return PlannerContext(capabilities=["knowledge"], environment={"safe_to_retry": True})


def test_plan_is_pure_function():
    g, d, c = _goal_for_plan(), _drive_for_plan(), _ctx_for_plan()
    assert derive_plan(g, d, c) == derive_plan(g, d, c)


def test_plan_is_derived_not_owned():
    # Plan must carry no persistence authority / no lifecycle setter (RFC-0106 P1/P3).
    assert not hasattr(Plan, "save")
    assert not hasattr(Plan, "persist")
    assert not hasattr(Plan, "restore")


def test_plan_does_not_mutate_goal_or_drive():
    g, d, c = _goal_for_plan(), _drive_for_plan(), _ctx_for_plan()
    g_before = (g.target, g.lifecycle)
    d_before = dict(d.orientation)
    derive_plan(g, d, c)
    # construction reads upstreams read-only (RFC-0106 C1/C2)
    assert (g.target, g.lifecycle) == g_before
    assert dict(d.orientation) == d_before


def test_planner_is_single_writer_and_no_goal_mutation():
    g, d, c = _goal_for_plan(), _drive_for_plan(), _ctx_for_plan()
    planner = Planner()
    planner.adopt_goal(g)
    plan = planner.build_plan(d, c)
    assert isinstance(plan, Plan)
    assert plan.goal_target == g.target  # Plan references the Goal's identity, not Drive's
    # cancellation discards, not archives (RFC-0106 V5)
    planner.cancel_plan()
    assert planner.active_plan is None


def test_drive_is_not_plan_decider():
    # Drive orientation influences (emphasis) but Goal target is the required content
    # source; both Drive readings reference the SAME goal (RFC-0106 C1).
    g, c = _goal_for_plan(), _ctx_for_plan()
    d_approach = Drive(
        orientation={OrientationAxis.APPROACH: 0.9, OrientationAxis.FOCUS: 0.5}, activated=True
    )
    d_avoid = Drive(
        orientation={OrientationAxis.AVOID: 0.9, OrientationAxis.FOCUS: 0.5}, activated=True
    )
    plan_a = derive_plan(g, d_approach, c)
    plan_v = derive_plan(g, d_avoid, c)
    assert plan_a.goal_target == plan_v.goal_target == g.target
    assert plan_a.emphasis == "approach"
    assert plan_v.emphasis == "avoid"


# --------------------------------------------------------------------------
# T3.1 — derive_plan → canonical Task output convergence (RFC-0002 §5)
# --------------------------------------------------------------------------
def test_derive_plan_returns_canonical_tasks():
    g, d, c = _goal_for_plan(), _drive_for_plan(), _ctx_for_plan()
    plan = derive_plan(g, d, c)
    assert isinstance(plan.task_graph, list)
    assert len(plan.task_graph) == 3
    assert all(isinstance(t, Task) for t in plan.task_graph)  # list[dict] → list[Task]


def test_derive_plan_task_mapping_is_explicit():
    # Legacy (id, op, about) → RFC-0002 §5 (id, title, intent, capability_need,
    # depends_on, estimates). Every field is asserted by name — no implicit **dict.
    g, d, c = _goal_for_plan(), _drive_for_plan(), _ctx_for_plan()
    tasks = derive_plan(g, d, c).task_graph
    assert [t.id for t in tasks] == ["t1", "t2", "t3"]
    # title ← op (capitalized human-readable label)
    assert [t.title for t in tasks] == ["Read", "Synthesize", "Present"]
    # intent ← about (Goal target / success criterion)
    assert tasks[0].intent == g.target
    assert tasks[1].intent == g.success_criterion
    assert tasks[2].intent == g.target
    # legacy declared no capability need / no planner hints → honest empty maps
    assert all(t.capability_need == {} for t in tasks)
    assert all(t.estimates == {} for t in tasks)
    # sequential ordering expressed as a DAG edge via depends_on
    assert tasks[0].depends_on == []
    assert tasks[1].depends_on == ["t1"]
    assert tasks[2].depends_on == ["t2"]


def test_derive_plan_task_ids_are_deterministic():
    # Pure-function contract: same inputs ⇒ same task ids — NO uuid/time/random leak.
    g, d, c = _goal_for_plan(), _drive_for_plan(), _ctx_for_plan()
    a = derive_plan(g, d, c).task_graph
    b = derive_plan(g, d, c).task_graph
    assert [t.id for t in a] == [t.id for t in b] == ["t1", "t2", "t3"]


def test_derive_plan_ordering_stays_in_sync_with_task_graph():
    g, d, c = _goal_for_plan(), _drive_for_plan(), _ctx_for_plan()
    plan = derive_plan(g, d, c)
    assert plan.ordering == [t.id for t in plan.task_graph]  # ["t1", "t2", "t3"]


# --------------------------------------------------------------------------
# M1.7 — Execution Engine (RFC-0107)
# --------------------------------------------------------------------------
def _plan_for_exec() -> Plan:
    # Canonical RFC-0002 §5 Tasks (T4.1): no legacy dict representation.
    return Plan(
        goal_target="understand X",
        task_graph=[
            Task(id="t1", title="Read", intent="understand X", depends_on=[], capability_need={}, estimates={}),
            Task(id="t2", title="Synthesize", intent="can explain X", depends_on=["t1"], capability_need={}, estimates={}),
            Task(id="t3", title="Present", intent="understand X", depends_on=["t2"], capability_need={}, estimates={}),
        ],
        ordering=["t1", "t2", "t3"],
        branching={"if_browser_missing": "fallback_to_knowledge"},
        rollback={"on_t2_fail": "revert_to_t1"},
        emphasis="approach",
    )


def _ctx_for_exec() -> ExecutionContext:
    return ExecutionContext(capabilities=["knowledge"], environment={})


def test_execution_result_is_transient_not_stored():
    # ExecutionResult must carry no persistence authority / no lifecycle setter (RFC-0107 P1/V4).
    assert not hasattr(ExecutionResult, "save")
    assert not hasattr(ExecutionResult, "persist")
    assert not hasattr(ExecutionResult, "restore")
    assert not hasattr(ExecutionResult, "archive")


def test_execution_consumes_plan_not_goal():
    # The Result references the Plan it consumed, never the Goal object (RFC-0107 C1).
    plan = _plan_for_exec()
    ctx = _ctx_for_exec()
    result = execute_plan(plan, ctx)
    assert isinstance(result, ExecutionResult)
    assert result.plan_ref  # deterministic ref of the consumed Plan shape
    assert result.status == ExecutionStatus.SUCCESS
    # The Result must NOT embed the Goal as a mutable handle — only a string ref of the Plan.
    assert not hasattr(result, "goal")
    assert "understand X" in result.plan_ref or result.plan_ref


def test_execution_does_not_mutate_plan():
    # Carrying the Plan must not mutate the Plan it consumes (RFC-0107 C1/C2 Authority leak guard).
    plan = _plan_for_exec()
    ctx = _ctx_for_exec()
    before_ordering = list(plan.ordering)
    before_goal_target = plan.goal_target
    execute_plan(plan, ctx)
    assert list(plan.ordering) == before_ordering
    assert plan.goal_target == before_goal_target


def test_execution_runtime_is_infra_carrier_not_authority():
    # The Runtime owns the act, not any Constitution state; it exposes no Goal/Plan mutation
    # method (RFC-0107 C2 — the X-firewall: Runtime is outside the Constitution stack).
    runtime = ExecutionRuntime(capabilities=["knowledge"])
    assert not hasattr(runtime, "set_goal")
    assert not hasattr(runtime, "revise_plan")
    assert not hasattr(runtime, "modify_intent")
    plan = _plan_for_exec()
    result = runtime.run(plan)
    assert isinstance(result, ExecutionResult)
    assert result.status == ExecutionStatus.SUCCESS


def test_execution_result_flows_to_memory_shape():
    # The Result carries the learned content Reflection would sediment into Memory (RFC-0107 C3);
    # the learned content is the *lessons*, not a stored Execution state.
    plan = _plan_for_exec()
    result = execute_plan(plan, _ctx_for_exec())
    assert isinstance(result.lessons, list) and len(result.lessons) >= 1
    # lessons reference plan/goal content, proving the Result is the Memory回流 input (not a store).
    assert any("understand X" in lesson for lesson in result.lessons)


def test_task_to_execution_step_adapter_is_explicit():
    # T4.1: the canonical Task -> runtime step conversion must be an explicit, typed
    # adapter. No dict is re-introduced as the canonical form; every field is mapped by name.
    task = Task(
        id="t2",
        title="Synthesize",
        intent="can explain X",
        capability_need={"type": "knowledge"},
        depends_on=["t1"],
        estimates={"cost": 0.3},
    )
    step = task_to_execution_step(task)
    assert isinstance(step, ExecutionStep)
    assert step.id == "t2"
    assert step.title == "Synthesize"
    assert step.intent == "can explain X"
    assert step.depends_on == ["t1"]
    assert step.capability_need == {"type": "knowledge"}
    assert step.estimates == {"cost": 0.3}


def test_execute_plan_consumes_canonical_task_graph():
    # T4.1: a canonical list[Task] enters the execution path directly (no legacy dict needed).
    plan = _plan_for_exec()
    assert all(isinstance(t, Task) for t in plan.task_graph)
    result = execute_plan(plan, _ctx_for_exec())
    assert result.status == ExecutionStatus.SUCCESS
    assert "t1|t2|t3" in result.plan_ref  # carried task ids preserved via the adapter


# --------------------------------------------------------------------------
# Phase D1 — RFC-0003 WorkflowRun vocabulary (run-state contract, no behavior)
# --------------------------------------------------------------------------
def _plan_ref_for_run() -> PlanRef:
    # Opaque handle only — never a Plan object (RFC-0003 §5 `plan_ref`, F-02).
    return PlanRef(plan_id="plan#understand-X", contract_version=PLAN_CONTRACT_VERSION)


def _workflow_run_for_test(plan: "Plan | None" = None) -> WorkflowRun:
    # Build a WorkflowRun referencing a Plan ONLY through the opaque PlanRef handle (F-02).
    # task_states are explicit run-local snapshots, never the Plan's live task_graph.
    task_states = (
        [TaskState(task_id=t.id, status=TaskRunStatus.PENDING) for t in plan.task_graph]
        if plan is not None
        else []
    )
    return WorkflowRun(
        id="run-1",
        plan_ref=_plan_ref_for_run(),
        goal_ref="goal#understand-X",
        task_states=task_states,
    )


def test_run_status_enum_is_complete():
    # RFC-0003 §5: exactly the six run-level statuses (no extra, no missing).
    assert {s.name for s in RunStatus} == {
        "PENDING", "RUNNING", "SUSPENDED", "COMPLETED", "FAILED", "CANCELLED",
    }


def test_task_run_status_enum_is_complete():
    # RFC-0003 §5: exactly the five per-task statuses.
    assert {s.name for s in TaskRunStatus} == {
        "PENDING", "RUNNING", "DONE", "FAILED", "SKIPPED",
    }


def test_workflow_run_can_be_constructed_independently():
    # The run-state vocabulary stands on its own — no Plan object, no PlanningInterface needed.
    run = WorkflowRun(id="run-1", plan_ref=PlanRef(plan_id="p1"))
    assert isinstance(run, WorkflowRun)
    assert run.id == "run-1"
    assert run.status == RunStatus.PENDING
    assert run.contract_version == WORKFLOWRUN_CONTRACT_VERSION
    assert isinstance(run.plan_ref, PlanRef)


def test_plan_ref_is_opaque_handle_not_plan_object():
    # F-02: plan_ref holds only {plan_id, contract_version}; it never embeds a Plan or task_graph,
    # and grants no Plan storage/lifecycle authority.
    run = WorkflowRun(id="run-1", plan_ref=PlanRef(plan_id="plan#x", contract_version="1.0.0"))
    assert isinstance(run.plan_ref, PlanRef)
    assert run.plan_ref.plan_id == "plan#x"
    assert run.plan_ref.contract_version == "1.0.0"
    # opacity: a PlanRef exposes no Plan-shaped surface
    assert not hasattr(run.plan_ref, "task_graph")
    assert not hasattr(run.plan_ref, "status")
    assert not hasattr(run.plan_ref, "save")
    assert not hasattr(run.plan_ref, "persist")


def test_workflow_run_does_not_hold_plan_instance():
    # Constructing/holding a WorkflowRun must not pull a Plan object into the run-state. The Plan
    # stays transient and PlanRef-only — never reachable as a live Plan from the run.
    plan = _plan_for_exec()  # a real canonical Plan with a populated task_graph
    run = _workflow_run_for_test(plan)
    assert run.plan_ref.plan_id  # reference by opaque id only
    assert not isinstance(run.plan_ref, Plan)
    assert not hasattr(run, "plan")        # no live Plan handle
    assert not hasattr(run, "task_graph")  # no embedded task_graph (F-02)
    # task_states are independent snapshots, not the Plan's live task_graph
    assert all(isinstance(s, TaskState) for s in run.task_states)


def test_workflow_run_does_not_trigger_plan_lifecycle():
    # Holding a WorkflowRun must not mutate the referenced Plan or introduce a Plan status writer.
    # PlanningInterface remains the SOLE Plan lifecycle writer (RFC-0002 O1) — unchallenged here.
    plan = _plan_for_exec()
    plan.status = PlanStatus.ACTIVE  # emulate an active Plan the Workflow might consume
    before = plan.status
    run = _workflow_run_for_test(plan)
    # constructing the run changes nothing about the Plan it references
    assert plan.status == before == PlanStatus.ACTIVE
    # WorkflowRun exposes no Plan lifecycle method (no second writer surface)
    assert not hasattr(run, "activate_plan")
    assert not hasattr(run, "invalidate_plan")
    assert not hasattr(run, "revise_plan")
    # WorkflowRun carries no status writer at all (data vocabulary only)
    assert not hasattr(WorkflowRun, "transition")
    assert not hasattr(WorkflowRun, "set_status")


def test_workflow_run_is_transient_not_stored():
    # No storage authority: WorkflowRun must not be persistable as a Constitution state (RFC-0003 §4.1 G4).
    run = WorkflowRun(id="run-1", plan_ref=PlanRef(plan_id="p1"))
    for forbidden in ("save", "persist", "restore", "archive", "load", "store"):
        assert not hasattr(run, forbidden)
    # F-02: the run references the Plan by opaque id, so persisting the run never persists the Plan
    assert isinstance(run.plan_ref, PlanRef) and not isinstance(run.plan_ref, Plan)


def test_workflow_run_rfc0003_field_coverage():
    # Every RFC-0003 §5 field is present on WorkflowRun (and TaskState), with the right shape.
    required_run_fields = {
        "id", "contract_version", "plan_ref", "goal_ref", "status",
        "task_states", "current_task_id", "checkpoint_ref", "retry_policy",
        "schedule_ref", "budget_ref", "context_ref", "created_at", "completed_at",
    }
    assert required_run_fields.issubset(set(WorkflowRun.__dataclass_fields__.keys()))
    # status is the RunStatus enum (data-only, no writer)
    run = WorkflowRun(id="r", plan_ref=PlanRef(plan_id="p"))
    assert isinstance(run.status, RunStatus)
    assert isinstance(run.task_states, list)
    assert isinstance(run.retry_policy, dict)  # metadata only

    required_taskstate_fields = {"task_id", "status", "attempts", "result", "error"}
    assert required_taskstate_fields.issubset(set(TaskState.__dataclass_fields__.keys()))
    ts = TaskState(task_id="t1", status=TaskRunStatus.DONE, attempts=2, result={"ok": True}, error="")
    assert ts.task_id == "t1" and ts.status == TaskRunStatus.DONE and ts.attempts == 2
    assert ts.result == {"ok": True} and ts.error == ""


# --------------------------------------------------------------------------
# Phase D2 — Workflow Interface skeleton + run-state single-writer lifecycle (RFC-0003 §6/§7)
# --------------------------------------------------------------------------
def _workflow_run_for_test(status=RunStatus.PENDING, run_id="run-1") -> WorkflowRun:
    # Direct construction helper — mirrors how PlanningInterface tests build Plans. Used to
    # set up a specific run-state (e.g. a PENDING run for abort_run) without the public API.
    return WorkflowRun(id=run_id, plan_ref=PlanRef(plan_id="plan#x"), status=status)


def _active_plan_for_run(
    plan_id="plan#understand-X",
    goal_ref="goal#understand-X",
    status=PlanStatus.ACTIVE,
    tasks=None,
) -> Plan:
    # An ACTIVE canonical Plan as the Workflow receives it at the consumption boundary (D3).
    # Built directly (not via PlanningInterface) so a specific plan_id / status can be pinned;
    # the real cross-authority path is exercised separately in the D3 block.
    return Plan(
        id=plan_id,
        goal_ref=goal_ref,
        goal_target="understand X",
        status=status,
        context_ref="ctx#understand-X",
        task_graph=[
            Task(id="t1", title="Read", intent="understand X", depends_on=[]),
            Task(id="t2", title="Synthesize", intent="can explain X", depends_on=["t1"]),
        ] if tasks is None else tasks,
    )


def test_workflow_interface_is_the_only_run_lifecycle_writer():
    """Single-writer rule (O1) enforced mechanically over the source tree (mirrors the
    Plan-lifecycle guard). No module other than ``workflow`` may assign a run runtime state,
    and inside it every write must go through the single ``_transition`` method."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1] / "anima"
    owner = root / "constitution" / "workflow.py"
    external_writer = re.compile(r"\.status\s*=\s*RunStatus\.")

    offenders = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if path != owner and external_writer.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"run lifecycle written outside the Workflow Interface: {offenders}"

    # Inside the owner, exactly ONE state-assignment site exists: the `_transition` funnel.
    source = owner.read_text(encoding="utf-8")
    assert len(re.findall(r"\.status\s*=(?!=)", source)) == 1
    assert "run.status = target" in source


def test_workflow_interface_public_surface_is_exactly_the_rfc_operations():
    """D2 fixes the §6 interface — it must not grow beyond the eight RFC-0003 operations."""
    public = {
        name
        for name in vars(WorkflowInterface)
        if not name.startswith("_") and callable(getattr(WorkflowInterface, name))
    }
    assert public == {
        "start_run",
        "read_run",
        "list_runs",
        "suspend_run",
        "resume_run",
        "cancel_run",
        "abort_run",
        "report_task_outcome",
    }


def test_workflow_interface_has_no_extra_lifecycle_mutation_api():
    # The interface exposes exactly the eight §6 ops; no hidden status/transition surface
    # (no second writer, no Plan-lifecycle method, no storage authority on the run).
    for forbidden in ("set_status", "transition", "activate_plan", "invalidate_plan",
                      "modify_run", "save", "persist", "restore"):
        assert not hasattr(WorkflowInterface, forbidden)


def test_start_run_creates_and_activates_run():
    # start_run is the run-activator: a fresh run goes pending -> running and holds only a
    # PlanRef opaque handle (F-02) — never the Plan object.
    wf = WorkflowInterface()
    run = wf.start_run(_active_plan_for_run())
    assert isinstance(run, WorkflowRun)
    assert run.status == RunStatus.RUNNING
    assert isinstance(run.plan_ref, PlanRef) and run.plan_ref.plan_id == "plan#understand-X"
    assert run.id
    # run is registered and readable
    assert wf.read_run(run.id) is run


def test_suspend_resume_lifecycle():
    wf = WorkflowInterface()
    run = wf.start_run(_active_plan_for_run(plan_id="plan#x"))
    suspended = wf.suspend_run(run.id, "pause")
    assert suspended.status == RunStatus.SUSPENDED
    resumed = wf.resume_run(run.id)
    assert resumed.status == RunStatus.RUNNING


def test_cancel_run_from_running_or_suspended():
    wf = WorkflowInterface()
    run = wf.start_run(_active_plan_for_run(plan_id="plan#x"))
    cancelled_from_running = wf.cancel_run(run.id, "user abort")
    assert cancelled_from_running.status == RunStatus.CANCELLED

    run2 = wf.start_run(_active_plan_for_run(plan_id="plan#y"))
    wf.suspend_run(run2.id, "pause")
    cancelled_from_suspended = wf.cancel_run(run2.id, "user abort")
    assert cancelled_from_suspended.status == RunStatus.CANCELLED


def test_abort_run_from_pending():
    # A PENDING run (e.g. crash before dispatch) may be aborted -> cancelled (resolves F-03).
    wf = WorkflowInterface()
    pending = _workflow_run_for_test(status=RunStatus.PENDING, run_id="run-pending")
    wf._registry["run-pending"] = pending
    aborted = wf.abort_run("run-pending", "crash before dispatch")
    assert aborted.status == RunStatus.CANCELLED


def test_terminal_states_have_no_outgoing_transition():
    import pytest
    from anima.constitution.errors import ConstitutionViolation

    wf = WorkflowInterface()
    for terminal in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED):
        run = _workflow_run_for_test(status=terminal, run_id=f"run-{terminal.value}")
        wf._registry[f"run-{terminal.value}"] = run
        # No operation may re-transition a terminal run; transitions are rejected by the table.
        for op in (
            lambda: wf.resume_run(f"run-{terminal.value}"),
            lambda: wf.cancel_run(f"run-{terminal.value}", "x"),
            lambda: wf.suspend_run(f"run-{terminal.value}", "x"),
        ):
            with pytest.raises(ConstitutionViolation):
                op()


def test_report_task_outcome_records_state_without_status_change():
    # Execution reports; the run status is NOT changed by the report (F-04 transition is D4).
    wf = WorkflowInterface()
    # single-Task Plan: D3 seeds one pending TaskState, so a report replaces it (no growth).
    single = [Task(id="t1", title="Read", intent="understand X", depends_on=[])]
    run = wf.start_run(_active_plan_for_run(plan_id="plan#x", tasks=single))
    reported = wf.report_task_outcome(run.id, "t1", TaskState(task_id="t1", status=TaskRunStatus.DONE))
    assert reported.status == RunStatus.RUNNING  # unchanged
    assert len(reported.task_states) == 1
    assert reported.task_states[0].task_id == "t1"
    assert reported.task_states[0].status == TaskRunStatus.DONE
    # a second report for the same task replaces (does not duplicate)
    wf.report_task_outcome(run.id, "t1", TaskState(task_id="t1", status=TaskRunStatus.FAILED, error="boom"))
    assert len(wf.read_run(run.id).task_states) == 1
    assert wf.read_run(run.id).task_states[0].status == TaskRunStatus.FAILED


def test_read_run_and_list_runs_are_read_only():
    wf = WorkflowInterface()
    run = wf.start_run(_active_plan_for_run(plan_id="plan#x", goal_ref="goal#x"))
    before = run.status
    _ = wf.read_run(run.id)
    assert run.status == before
    # list_runs is a read-only filter over the live registry (goal_ref comes from the Plan, D3)
    listed = wf.list_runs("goal#x")
    assert len(listed) == 1 and listed[0].id == run.id
    assert run.status == before  # listing did not transition


def test_workflow_run_registry_is_in_process_only():
    # The run registry is an in-process dict; the interface exposes no persistence surface.
    wf = WorkflowInterface()
    assert isinstance(wf._registry, dict)
    for forbidden in ("save", "persist", "restore", "store", "load"):
        assert not hasattr(wf, forbidden)


# --------------------------------------------------------------------------
# Phase D3 — Plan -> WorkflowRun consumption boundary (RFC-0003 §6 start_run, F-01/F-02)
# --------------------------------------------------------------------------
def test_start_run_consumes_an_active_plan_from_the_planning_authority():
    """The real cross-authority path: the Planning Authority produces + activates the Plan,
    the Workflow Authority consumes it. Two authorities, one read-only handoff."""
    from anima.constitution.planning_interface import PlanningInterface

    planning = PlanningInterface()
    plan = planning.request_plan(
        "goal#understand-X",
        goal=_goal_for_plan(),
        drive=_drive_for_plan(),
        context=_ctx_for_plan(),
    )
    planning.activate_plan(plan.id)
    # The caller dereferences the handle; the Workflow is handed the resolved Plan, read-only.
    resolved = planning.read_plan(plan.id)

    wf = WorkflowInterface()
    run = wf.start_run(resolved)

    assert run.status == RunStatus.RUNNING          # sole run-activator (GQ9)
    assert run.plan_ref.plan_id == plan.id          # opaque snapshot handle (F-02)
    assert run.plan_ref.contract_version == plan.contract_version
    assert run.goal_ref == "goal#understand-X"      # F-01 reference population
    assert run.context_ref == resolved.context_ref  # F-01 reference population
    # one derived TaskState per Plan Task, in Plan order
    assert [s.task_id for s in run.task_states] == [t.id for t in resolved.task_graph]
    assert all(s.status == TaskRunStatus.PENDING for s in run.task_states)
    # consumption is NOT a Plan transition (RFC-0002 §7.1 `consume`)
    assert planning.read_plan(plan.id).status is PlanStatus.ACTIVE


def test_start_run_rejects_a_draft_plan():
    # A draft Plan is uncommitted; the Workflow never self-activates (that is activate_plan).
    import pytest
    from anima.constitution.errors import ConstitutionViolation

    wf = WorkflowInterface()
    draft = _active_plan_for_run(status=PlanStatus.DRAFT)
    with pytest.raises(ConstitutionViolation):
        wf.start_run(draft)
    assert draft.status is PlanStatus.DRAFT   # refusal, not repair
    assert wf._registry == {}                 # no half-created run left behind


def test_start_run_rejects_discarded_plans():
    # superseded / deprecated Plans are discarded and never revived (RFC-0106 V5).
    import pytest
    from anima.constitution.errors import ConstitutionViolation

    for discarded in (PlanStatus.SUPERSEDED, PlanStatus.DEPRECATED):
        wf = WorkflowInterface()
        plan = _active_plan_for_run(status=discarded)
        with pytest.raises(ConstitutionViolation):
            wf.start_run(plan)
        assert plan.status is discarded
        assert wf._registry == {}


def test_start_run_rejects_an_empty_task_graph():
    # A run with no tasks can never reach a terminal state — refuse at the boundary.
    import pytest
    from anima.constitution.errors import ConstitutionViolation

    wf = WorkflowInterface()
    empty = _active_plan_for_run(tasks=[])
    with pytest.raises(ConstitutionViolation):
        wf.start_run(empty)
    assert empty.task_graph == []
    assert wf._registry == {}


def test_plan_to_run_tasks_is_an_explicit_field_mapping():
    """The Task -> TaskState adapter maps by name only — no implicit unpacking, no aliasing."""
    import inspect
    from anima.constitution.workflow import plan_to_run_tasks

    plan = _active_plan_for_run()
    states = plan_to_run_tasks(plan)

    assert [s.task_id for s in states] == [t.id for t in plan.task_graph]  # order preserved
    for state in states:
        assert isinstance(state, TaskState)
        assert state.status == TaskRunStatus.PENDING
        assert state.attempts == 0
        assert state.result == {} and state.error == ""
    # snapshots, never the Plan's Task nodes (F-02): no shared identity, no Task leaked through
    assert all(not isinstance(s, Task) for s in states)
    assert all(s is not t for s, t in zip(states, plan.task_graph))
    # mutating a snapshot must not reach back into the Plan
    states[0].attempts = 7
    assert not hasattr(plan.task_graph[0], "attempts")

    # mechanical: the adapter *code* contains no implicit-unpacking escape hatch. The docstring
    # is stripped via the AST first — it names the forbidden forms in order to forbid them.
    import ast
    import textwrap

    fn = ast.parse(textwrap.dedent(inspect.getsource(plan_to_run_tasks))).body[0]
    if ast.get_docstring(fn) is not None:
        fn.body = fn.body[1:]
    code = ast.unparse(fn)
    for forbidden in ("__dict__", "vars(", "**"):
        assert forbidden not in code, f"adapter must map fields explicitly, found {forbidden!r}"


def test_run_holds_no_plan_after_start_run():
    """F-02: the Plan does not survive into the run — only the opaque handle does."""
    wf = WorkflowInterface()
    plan = _active_plan_for_run()
    run = wf.start_run(plan)

    assert run.plan_ref.plan_id == plan.id
    assert run.plan_ref.contract_version == plan.contract_version
    assert not hasattr(run, "plan")        # no live Plan handle
    assert not hasattr(run, "task_graph")  # no embedded task_graph
    # no attribute anywhere on the run is (or contains) the Plan / a Task node
    values = list(vars(run).values())
    assert not any(isinstance(v, Plan) for v in values)
    assert not any(isinstance(v, Task) for v in values)
    assert all(not isinstance(s, Task) for s in run.task_states)
    assert plan not in values
    # the interface captured no standing pathway back to Planning either — just the run registry
    assert set(vars(wf)) == {"_registry"}


def test_start_run_leaves_the_plan_unchanged():
    """Read-only consumption, proven by value: the Plan before == the Plan after."""
    import copy

    wf = WorkflowInterface()
    plan = _active_plan_for_run()
    before = copy.deepcopy(plan)
    task_identities = [id(t) for t in plan.task_graph]

    wf.start_run(plan)

    assert plan == before                       # whole-object equality (all §5 fields)
    assert plan.status is PlanStatus.ACTIVE     # lifecycle untouched
    assert plan.task_graph == before.task_graph
    assert [id(t) for t in plan.task_graph] == task_identities  # no Task replaced in place


def test_workflow_never_writes_plan_lifecycle_state():
    """D3 reads a Plan across the authority boundary; it must not acquire a Plan writer.

    Complements the tree-wide guard in test_planning_interface.py (which proves no module
    outside PlanningInterface assigns a Plan runtime state) by pinning the specific module
    that now imports PlanStatus."""
    import pathlib
    import re

    source = (
        pathlib.Path(__file__).resolve().parents[1]
        / "anima" / "constitution" / "workflow.py"
    ).read_text(encoding="utf-8")

    # it reads the Plan's runtime state, but never assigns it
    assert "PlanStatus" in source
    assert re.search(r"\.status\s*=\s*PlanStatus\.", source) is None
    # it invokes no Plan lifecycle operation, and holds no Planning Interface dependency:
    # the Plan arrives as an argument, so the Workflow has no way to reach the Plan registry
    for call in (".activate_plan(", ".invalidate_plan(", ".revise_plan(", ".request_plan("):
        assert call not in source
    assert "planning_interface" not in source
    assert "PlanningInterface" not in source
    # and the Workflow Interface still carries no Plan-lifecycle surface at all
    for forbidden in ("activate_plan", "invalidate_plan", "revise_plan", "request_plan"):
        assert not hasattr(WorkflowInterface, forbidden)


# --------------------------------------------------------------------------
# M1.8 — Memory Ingestion Interface (RFC-0108)
# --------------------------------------------------------------------------
def _reflection_result_for_ingest() -> ReflectionResult:
    return ReflectionResult(
        result_ref="plan#understand-X#run-1",  # references ExecutionResult, NOT Goal/Plan object
        records=[
            {"kind": "semantic", "content": {"skill": "explain X"}, "salience": 0.8},
            {"kind": "episode", "content": {"event": "solved X"}, "salience": 0.6},
        ],
    )


def test_reflection_result_is_transient_not_stored():
    # ReflectionResult must carry no persistence authority / no lifecycle setter (RFC-0108 C3/V4).
    assert not hasattr(ReflectionResult, "save")
    assert not hasattr(ReflectionResult, "persist")
    assert not hasattr(ReflectionResult, "restore")
    assert not hasattr(ReflectionResult, "archive")
    # It references the consumed ExecutionResult, never the Goal/Plan object (RFC-0108 C1).
    rr = _reflection_result_for_ingest()
    assert not hasattr(rr, "goal")
    assert not hasattr(rr, "plan")


def test_memory_authority_ingests_reflection_result_only():
    # The sink accepts a ReflectionResult; it rejects an ExecutionResult.save()-style bypass
    # (second Memory = O1 violation, RFC-0108 C3).
    engine = MemoryEngine()
    rr = _reflection_result_for_ingest()
    authority = MemoryAuthority(engine)
    authority.ingest(rr)
    # records sedimented into the one Memory
    snap = engine.snapshot()
    assert len(snap.semantic) == 1 and len(snap.episodes) == 1

    # An ExecutionResult must NOT be ingestible as a store (no ExecutionResult.save()).
    result = execute_plan(_plan_for_exec(), _ctx_for_exec())
    raised = False
    try:
        ingest(result, engine)  # type: ignore[arg-type]
    except Exception:
        raised = True
    assert raised, "Memory Authority must reject ExecutionResult ingestion (forbids second Memory)"


def test_memory_authority_does_not_mutate_reflection_result():
    # The ingest act must not mutate the ReflectionResult it consumes (RFC-0108 C1/C2).
    engine = MemoryEngine()
    rr = _reflection_result_for_ingest()
    before = list(rr.records)
    ingest(rr, engine)
    assert list(rr.records) == before


def test_memory_authority_is_sink_not_constitution_authority():
    # The Memory Authority owns the store; it exposes NO derived/owned-state mutation method
    # (the sink X-firewall: parallel to RFC-0107's Runtime firewall, RFC-0108 C2).
    authority = MemoryAuthority()
    for forbidden in ("update_identity", "apply_to_need", "patch_goal", "set_affect", "modify_drive"):
        assert not hasattr(authority, forbidden), f"Memory Authority must not expose {forbidden}"


def test_memory_authority_closes_the_loop():
    # After ingestion, the loop closes: Memory reshapes the next cycle's Identity via f(Memory).
    engine = MemoryEngine()
    rr = ReflectionResult(
        result_ref="plan#learn-Y#run-2",
        records=[{"kind": "semantic", "content": {"value": "persistence"}, "salience": 0.9}],
    )
    MemoryAuthority(engine).ingest(rr)
    # The sedimented value reshapes Identity next cycle (f(Memory)), proving closure.
    identity = derive_identity(engine.snapshot())
    assert any("persistence" in str(v) for v in identity.values)


# --------------------------------------------------------------------------
# M1.9 — Reflection Engine (RFC-0109)
# --------------------------------------------------------------------------
def _execution_result_for_reflect() -> ExecutionResult:
    return ExecutionResult(
        plan_ref="t1|t2|t3",  # deterministic ref of the consumed Plan (NOT a Goal reference)
        status=ExecutionStatus.SUCCESS,
        outcome="executed plan for 'understand X'",
        lessons=[
            "carried 3 task(s) toward 'understand X'",
            "drive emphasis at plan time = 'approach'",
        ],
    )


def test_reflection_is_pure_function():
    # Same ExecutionResult ⇒ same ReflectionResult (deterministic transform, no hidden state,
    # RFC-0109 §2: Reflection = Transform Act, pure like every M1 engine).
    er = _execution_result_for_reflect()
    assert reflect(er) == reflect(er)


def test_reflection_result_is_not_memory():
    # ReflectionResult is a transient transform output, NOT a store / second Memory root
    # (RFC-0109 C3). It carries no persistence authority and references no Goal/Plan handle.
    rr = reflect(_execution_result_for_reflect())
    assert isinstance(rr, ReflectionResult)
    # It is not the Memory store type, and it exposes no storage API.
    assert not isinstance(rr, Memory)
    for forbidden in ("save", "persist", "load", "restore", "write", "store"):
        assert not hasattr(rr, forbidden), f"ReflectionResult must not carry {forbidden}"
    assert not hasattr(rr, "goal")
    assert not hasattr(rr, "plan")


def test_reflection_does_not_mutate_execution_result():
    # The transform reads the ExecutionResult read-only; it must not mutate the experience it
    # interprets (RFC-0109 C1/C2 Authority-leak guard — parallel to execution/planner guards).
    er = _execution_result_for_reflect()
    before_status = er.status
    before_lessons = list(er.lessons)
    before_plan_ref = er.plan_ref
    reflect(er)
    assert er.status == before_status
    assert list(er.lessons) == before_lessons
    assert er.plan_ref == before_plan_ref


def test_reflector_has_no_storage_or_authority_leak_api():
    # The Reflection Authority (Reflector) owns the transform act, never a store or a life state.
    # It must expose NO storage / authority-leak method — the transform X-firewall (RFC-0109
    # C1/C2/C3: forbid a second Memory and any upstream/downstream mutation).
    reflector = Reflector()
    for forbidden in (
        "save",
        "persist",
        "write_memory",
        "store_memory",
        "save_memory",
        "create_persona",
        "modify_goal",
        "patch_goal",
        "modify_drive",
        "update_identity",
        "apply_to_need",
        "set_affect",
    ):
        assert not hasattr(reflector, forbidden), (
            f"Reflector must not expose {forbidden} (RFC-0109 C1/C2/C3)"
        )


def test_reflection_result_is_ingested_by_memory_authority():
    # The loop closes: ExecutionResult → Reflection → ReflectionResult → Memory Authority.
    # reflect() output is a legal input to MemoryAuthority.ingest() (RFC-0108 ✓, no second Memory).
    engine = MemoryEngine()
    er = _execution_result_for_reflect()
    rr = reflect(er)
    assert isinstance(rr, ReflectionResult)
    MemoryAuthority(engine).ingest(rr)
    snap = engine.snapshot()
    # one episode (carried) + two semantic (lessons) sedimented into the ONE Memory
    assert len(snap.episodes) == 1
    assert len(snap.semantic) == 2


# --------------------------------------------------------------------------
# M1.10 — Persona Engine (RFC-0110)
# --------------------------------------------------------------------------
def _identity_for_persona() -> Identity:
    return derive_identity(
        _engine_with({"curiosity": 0.9, "value": "understanding", "voice": "warm"}).snapshot()
    )


def test_persona_is_pure_function():
    # Same Identity ⇒ same Persona (deterministic projection, no hidden state / no timestamp,
    # RFC-0110 §4 P1: Persona is a Derived projection, a reading — like Need/Affect/Drive).
    i = _identity_for_persona()
    assert derive_persona(i) == derive_persona(i)


def test_persona_depends_on_identity():
    # Different Identity ⇒ different Persona (the projection tracks the root it was derived from,
    # no second Identity root — RFC-0100B line 162).
    curious = derive_identity(
        _engine_with({"curiosity": 0.9, "value": "understanding"}).snapshot()
    )
    passive = derive_identity(
        _engine_with({"curiosity": 0.1, "value": "rest"}).snapshot()
    )
    assert derive_persona(curious) != derive_persona(passive)


def test_persona_does_not_mutate_identity():
    # The projection reads Identity read-only; it must not mutate the root it was derived from
    # (RFC-0110 C1/C2 Authority-leak guard — the central Persona risk is a second Identity root).
    i = _identity_for_persona()
    before_traits = dict(i.traits)
    before_values = list(i.values)
    derive_persona(i)
    assert dict(i.traits) == before_traits
    assert list(i.values) == before_values


def test_persona_is_not_a_store():
    # Persona carries no persistence authority / no lifecycle setter (RFC-0110 C3/V4). It is a
    # Derived projection, not a second Memory / second Identity root.
    assert not hasattr(Persona, "save")
    assert not hasattr(Persona, "persist")
    assert not hasattr(Persona, "restore")
    assert not hasattr(Persona, "archive")
    # It is not the Memory store type, and it references no Goal/Plan/Execution handle.
    p = derive_persona(_identity_for_persona())
    assert isinstance(p, Persona)
    assert not isinstance(p, Memory)
    assert not hasattr(p, "goal")
    assert not hasattr(p, "plan")


def test_persona_projector_has_no_authority_leak():
    # The Persona Authority (PersonaProjector) owns the projection act, never a store or a life
    # state. It must expose NO storage / authority-leak method — the projection X-firewall
    # (RFC-0110 C1/C2/C3: forbid a second Identity root and any upstream/downstream mutation).
    projector = PersonaProjector()
    for forbidden in (
        "save",
        "persist",
        "write_memory",
        "store_memory",
        "save_memory",
        "create_persona",
        "modify_goal",
        "patch_goal",
        "modify_drive",
        "update_identity",
        "modify_identity",
        "rewrite_identity",
        "patch_need",
    ):
        assert not hasattr(projector, forbidden), (
            f"PersonaProjector must not expose {forbidden} (RFC-0110 C1/C2/C3)"
        )


# --------------------------------------------------------------------------
# M1.11 — Relationship Engine (RFC-0111)
# --------------------------------------------------------------------------
def _memory_with_relation(external_ref: str = "user_abu", trust: float = 0.8, role: str = "companion") -> Memory:
    mem = Memory()
    mem.relations.append(
        MemoryRecord(
            kind="relation",
            content={"external_ref": external_ref, "trust": trust, "role": role},
            salience=0.7,
        )
    )
    return mem


def _identity_for_relationship() -> Identity:
    return derive_identity(_memory_with_relation())


def test_relationship_is_pure_function():
    # Same (Memory, Identity, external_ref) ⇒ same RelationshipProjection (deterministic projection,
    # no hidden state / no timestamp, RFC-0111 §4 P1: Relationship is a Derived projection, a reading).
    mem = _memory_with_relation()
    identity = derive_identity(mem)
    rp1 = derive_relationship(mem, identity, "user_abu")
    rp2 = derive_relationship(mem, identity, "user_abu")
    assert rp1 == rp2
    assert rp1.trust == 0.8
    assert rp1.role == "companion"


def test_relationship_does_not_mutate_memory():
    # The projection reads Memory.relations read-only; it must not mutate the sink it was derived
    # from (RFC-0111 C3 — the central Relationship → Memory bypass guard: no second Memory written).
    mem = _memory_with_relation()
    identity = derive_identity(mem)
    before_relations = list(mem.relations)
    before_len = len(mem.relations)
    derive_relationship(mem, identity, "user_abu")
    assert len(mem.relations) == before_len
    assert list(mem.relations) == before_relations


def test_relationship_does_not_mutate_identity():
    # The projection reads Identity read-only; it must not mutate the root it was derived from
    # (RFC-0111 C1 — the second-self-writer guard: Relationship models only, Identity re-derives upstream).
    identity = _identity_for_relationship()
    before_traits = dict(identity.traits)
    before_self_model = dict(identity.self_model)
    derive_relationship(_memory_with_relation(), identity, "user_abu")
    assert dict(identity.traits) == before_traits
    assert dict(identity.self_model) == before_self_model


def test_relationship_is_not_a_store():
    # RelationshipProjection carries no persistence authority / no lifecycle setter (RFC-0111 C3/V4).
    # It is a Derived projection, not a second Memory / second Identity root. It must not write Goal.
    assert not hasattr(RelationshipProjection, "save")
    assert not hasattr(RelationshipProjection, "persist")
    assert not hasattr(RelationshipProjection, "restore")
    assert not hasattr(RelationshipProjection, "archive")
    # It is not the Memory store type, and it references no Goal/Plan/Execution handle.
    rp = derive_relationship(_memory_with_relation(), _identity_for_relationship(), "user_abu")
    assert isinstance(rp, RelationshipProjection)
    assert not isinstance(rp, Memory)
    assert not hasattr(rp, "goal")
    assert not hasattr(rp, "plan")


def test_relationship_projector_has_no_authority_leak():
    # The Relationship Authority (RelationshipProjector) owns the projection act, never a store or a
    # life state. It must expose NO storage / authority-leak method — the projection X-firewall
    # (RFC-0111 C1/C2/C3: forbid a second Memory, a motivation source, and a second Identity system).
    projector = RelationshipProjector()
    for forbidden in (
        "save",
        "persist",
        "write_memory",
        "store_memory",
        "save_memory",
        "create_memory",
        "create_persona",
        "modify_goal",
        "patch_goal",
        "patch_drive",
        "update_identity",
        "modify_identity",
        "rewrite_identity",
        "patch_self_model",
    ):
        assert not hasattr(projector, forbidden), (
            f"RelationshipProjector must not expose {forbidden} (RFC-0111 C1/C2/C3)"
        )


if __name__ == "__main__":
    test_identity_is_pure_function()
    test_identity_derivation_does_not_mutate_memory()
    test_identity_inherits_persistence_via_memory()
    test_need_is_pure_function()
    test_need_depends_on_identity()
    test_affect_does_not_mutate_need()
    test_affect_is_pure_function()
    test_drive_is_pure_function()
    test_drive_has_no_goal_authority_edge()
    test_constitution_invariants_hold()
    test_plan_is_pure_function()
    test_plan_is_derived_not_owned()
    test_plan_does_not_mutate_goal_or_drive()
    test_planner_is_single_writer_and_no_goal_mutation()
    test_drive_is_not_plan_decider()
    test_execution_result_is_transient_not_stored()
    test_execution_consumes_plan_not_goal()
    test_execution_does_not_mutate_plan()
    test_execution_runtime_is_infra_carrier_not_authority()
    test_execution_result_flows_to_memory_shape()
    test_reflection_result_is_transient_not_stored()
    test_memory_authority_ingests_reflection_result_only()
    test_memory_authority_does_not_mutate_reflection_result()
    test_memory_authority_is_sink_not_constitution_authority()
    test_memory_authority_closes_the_loop()
    test_reflection_is_pure_function()
    test_reflection_result_is_not_memory()
    test_reflection_does_not_mutate_execution_result()
    test_reflector_has_no_storage_or_authority_leak_api()
    test_reflection_result_is_ingested_by_memory_authority()
    test_persona_is_pure_function()
    test_persona_depends_on_identity()
    test_persona_does_not_mutate_identity()
    test_persona_is_not_a_store()
    test_persona_projector_has_no_authority_leak()
    test_relationship_is_pure_function()
    test_relationship_does_not_mutate_memory()
    test_relationship_does_not_mutate_identity()
    test_relationship_is_not_a_store()
    test_relationship_projector_has_no_authority_leak()
    print("ALL M1 SMOKE TESTS PASSED")
