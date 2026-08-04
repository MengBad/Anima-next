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
from anima.constitution.execution import ExecutionRuntime, execute_plan
from anima.constitution.memory_authority import MemoryAuthority, ingest
from anima.constitution.state import (
    Affect,
    Drive,
    ExecutionContext,
    ExecutionResult,
    ExecutionStatus,
    Goal,
    GoalLifecycle,
    OrientationAxis,
    Perception,
    Plan,
    PlannerContext,
    ReflectionResult,
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
# M1.7 — Execution Engine (RFC-0107)
# --------------------------------------------------------------------------
def _plan_for_exec() -> Plan:
    return Plan(
        goal_target="understand X",
        task_graph=[
            {"id": "t1", "op": "read", "about": "understand X"},
            {"id": "t2", "op": "synthesize", "about": "can explain X"},
            {"id": "t3", "op": "present", "about": "understand X"},
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
    print("ALL M1 SMOKE TESTS PASSED")
