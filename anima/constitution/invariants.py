"""Constitution Invariant Guard (M1.5) — mechanical defense against drift.

The RFC-0100 trilogy is FROZEN (RFC-0105 §14). The real risk now is not the algorithm
but that a *future* Agent quietly breaks the Constitution — adding a ``need.save()``, a
``drive.plan()``, or mutating upstream in place. This module provides mechanical checks
(RFC-0102 C4 / RFC-0103 C3 / RFC-0104 D3 carried from docs into code) so such drift
fails loudly, not silently.

Checks
------
- ``assert_no_setter(cls)``            — no mutating public method on a state class
- ``assert_no_storage_authority(cls)`` — derived states carry no persistence authority
- ``assert_no_cross_authority_mutation(func, upstream, *args)``
                                      — ``func(upstream, ...)`` must not mutate upstream
- ``check_constitution_invariants()``  — run all of the above over the real engines

Run ``check_constitution_invariants()`` in CI / at boot so any regression in the
derivation chain is caught before it can corrupt a live life.
"""
from __future__ import annotations

import copy

from anima.constitution.affect import derive_affect
from anima.constitution.drive import derive_drive
from anima.constitution.execution import execute_plan
from anima.constitution.identity import derive_identity
from anima.constitution.memory_authority import MemoryAuthority, ingest
from anima.constitution.memory_engine import MemoryEngine
from anima.constitution.need import derive_need
from anima.constitution.planner import derive_plan
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
    Need,
    OrientationAxis,
    Perception,
    Persona,
    Plan,
    PlannerContext,
    ReflectionResult,
    RelationshipProjection,
)


from anima.constitution.errors import ConstitutionViolation  # re-exported for compat


# Mutating / persistence / authority-leak verbs that must never appear as a public
# method on a derived state class.
FORBIDDEN_METHODS = (
    "set_",
    "update",
    "save",
    "persist",
    "load",
    "store",
    "restore",
    "create",
    "plan",
    "adopt",
    "modify",
    "write",
)


def _public_methods(cls):
    return [
        (name, member)
        for name, member in vars(cls).items()
        if not name.startswith("__") and callable(member)
    ]


def assert_no_setter(cls) -> bool:
    """A Constitution state class must expose no mutating public method."""
    offenders = [name for name, _ in _public_methods(cls) if name.startswith(FORBIDDEN_METHODS)]
    if offenders:
        raise ConstitutionViolation(
            f"{cls.__name__} exposes mutating method(s): {offenders} "
            f"(forbidden by RFC-0102 C4 / RFC-0104 D3)"
        )
    return True


def assert_no_storage_authority(cls) -> bool:
    """Derived states must not carry persistence authority."""
    offenders = [
        name for name, _ in _public_methods(cls)
        if name in ("save", "persist", "load", "restore", "store", "write")
    ]
    if offenders:
        raise ConstitutionViolation(
            f"{cls.__name__} carries storage authority: {offenders} "
            f"(forbidden by RFC-0102 C4 / RFC-0103 C3)"
        )
    return True


def assert_no_cross_authority_mutation(func, upstream, *args) -> bool:
    """``func(upstream, *args)`` must not mutate the upstream state it reads.

    Guards against an engine quietly editing its parent state (e.g. Affect writing
    back into Need). Deep-copies before/after and compares.
    """
    before = copy.deepcopy(upstream)
    func(upstream, *args)
    after = copy.deepcopy(upstream)
    if before != after:
        raise ConstitutionViolation(
            f"{getattr(func, '__name__', func)} mutated its upstream input "
            f"(cross-authority mutation forbidden by RFC-0104 D3)"
        )
    return True


def _sample_chain():
    """Build a minimal live chain for mutation checks."""
    mem = Memory()
    mem.semantic.append(
        MemoryRecord(kind="semantic", content={"curiosity": 0.9, "value": "understanding"}, salience=0.8)
    )
    identity = derive_identity(mem)
    need = derive_need(identity)
    perception = Perception(signals={"novelty": 0.7, "threat": 0.0})
    affect = derive_affect(need, perception)
    return mem, identity, need, perception, affect


def check_constitution_invariants() -> bool:
    """Run every mechanical guard over the real engines. Raises on any breach."""
    # 1. No setters / storage authority on derived / transient state classes
    for cls in (Need, Affect, Drive, Plan, ExecutionResult, ReflectionResult, Persona, RelationshipProjection):
        assert_no_setter(cls)
        assert_no_storage_authority(cls)

    mem, identity, need, perception, affect = _sample_chain()

    # 2. No engine mutates its upstream input
    assert_no_cross_authority_mutation(derive_identity, mem)
    assert_no_cross_authority_mutation(derive_need, identity)
    assert_no_cross_authority_mutation(derive_affect, need, perception)
    assert_no_cross_authority_mutation(derive_drive, affect)

    # 3. Planner derives Plan without mutating its upstreams (RFC-0106 C1/C2: reads
    #    Goal/Drive/Context, mutates NONE of them — the Authority-leak guard).
    goal, drive, ctx = _sample_plan_inputs()
    assert_no_cross_authority_mutation(derive_plan, goal, drive, ctx)
    # Drive is also read-only (wrap to put it in the upstream position).
    assert_no_cross_authority_mutation(lambda d, g, c: derive_plan(g, d, c), drive, goal, ctx)

    # 4. Execution consumes a Plan read-only — does NOT mutate the Plan it carries (RFC-0107
    #    C1/C2: Execution reads Plan, mutates neither Plan nor Goal nor Drive nor Context).
    plan, exec_ctx = _sample_execution_inputs()
    assert_no_cross_authority_mutation(execute_plan, plan, exec_ctx)

    # 5. Memory Authority ingests a ReflectionResult read-only — does NOT mutate the result it
    #    consumes (RFC-0108 C1/C2: the sink reads the ReflectionResult, mutates neither it nor any
    #    derived/owned state; it is the *store*, not a Constitution Authority over the chain).
    reflection_result = _sample_reflection_result()
    engine = MemoryEngine()
    before_records = len(reflection_result.records)
    ingest(reflection_result, engine)
    # the consumed ReflectionResult must be untouched by the ingest act
    if len(reflection_result.records) != before_records:
        raise ConstitutionViolation(
            "Memory Authority mutated the ReflectionResult it ingested "
            "(cross-authority mutation forbidden by RFC-0108 C1/C2)"
        )

    # 6. Reflection transforms an ExecutionResult read-only — does NOT mutate the
    #    ExecutionResult it consumes (RFC-0109 C1/C2/C3: reads experience, mutates neither
    #    it nor any downstream state; it is the *transform*, not a Constitution Authority).
    execution_result = _sample_execution_result()
    assert_no_cross_authority_mutation(reflect, execution_result)

    # 7. Reflection Authority (Reflector) and ReflectionResult expose NO storage / authority-
    #    leak method — they are the *producer*, not a second store, and never edit a life state
    #    (RFC-0109 C2/C3 transform X-firewall). A second Memory is an O1 violation.
    for forbidden in ("save", "persist", "load", "restore", "write", "store", "create"):
        if hasattr(ReflectionResult, forbidden):
            raise ConstitutionViolation(
                f"ReflectionResult carries storage authority '{forbidden}' "
                f"(second Memory forbidden by RFC-0109 C3)"
            )
    for forbidden in (
        "modify_goal",
        "patch_goal",
        "modify_drive",
        "update_identity",
        "apply_to_need",
        "set_affect",
        "write_memory",
        "save_memory",
        "create_persona",
        "store_memory",
    ):
        if hasattr(Reflector, forbidden):
            raise ConstitutionViolation(
                f"Reflector exposes authority-leak method '{forbidden}' "
                f"(forbidden by RFC-0109 C1/C2/C3)"
            )

    # 8. Persona projects Identity read-only — does NOT mutate the Identity it reads
    #    (RFC-0110 C1/C2 Authority-leak guard: the projection must never rewrite the root
    #    it was derived from; a second Identity root is the central Persona risk).
    identity_sample = _sample_identity_for_persona()
    assert_no_cross_authority_mutation(derive_persona, identity_sample)

    # 9. Persona (state) and the PersonaProjector expose NO storage / authority-leak method
    #    — Persona is the *projection*, not a second Identity root or a second store (RFC-0110
    #    C1/C2/C3 transform X-firewall). A second Identity root is a worse violation than a
    #    second Memory (the life becomes two people).
    for forbidden in ("save", "persist", "load", "restore", "write", "store", "create"):
        if hasattr(Persona, forbidden):
            raise ConstitutionViolation(
                f"Persona carries storage authority '{forbidden}' "
                f"(second store / second root forbidden by RFC-0110 C3)"
            )
    for forbidden in (
        "update_identity",
        "modify_identity",
        "rewrite_identity",
        "patch_need",
        "create_goal",
        "modify_goal",
        "write_memory",
        "save_memory",
        "store_memory",
    ):
        if hasattr(PersonaProjector, forbidden):
            raise ConstitutionViolation(
                f"PersonaProjector exposes authority-leak method '{forbidden}' "
                f"(forbidden by RFC-0110 C1/C2/C3)"
            )

    # 10. Relationship projects Memory + Identity read-only — does NOT mutate the Memory it reads
    #     (C3 — the central Relationship → Memory bypass guard) nor the Identity it reads (C1 — the
    #     second-self-writer guard). It is a read-side projection spanning the Life/Environment
    #     boundary; neither the sink nor the root may be edited by the model of an external agent.
    mem_rel, identity_rel, external_ref_rel = _sample_relationship_inputs()
    assert_no_cross_authority_mutation(
        lambda m, i: derive_relationship(m, i, external_ref_rel), mem_rel, identity_rel
    )
    assert_no_cross_authority_mutation(
        lambda i, m: derive_relationship(m, i, external_ref_rel), identity_rel, mem_rel
    )

    # 11. RelationshipProjection (state) and the RelationshipProjector expose NO storage / authority-leak
    #     method — Relationship is the *projection*, not a second Memory (C3), not a motivation source
    #     (C2: no goal mutation), not a second Identity system (C1: no identity rewrite). The three named
    #     relationship risks are mechanically closed here (RFC-0111 C1/C2/C3).
    for forbidden in ("save", "persist", "load", "restore", "write", "store", "create", "archive"):
        if hasattr(RelationshipProjection, forbidden):
            raise ConstitutionViolation(
                f"RelationshipProjection carries storage authority '{forbidden}' "
                f"(second store / second root forbidden by RFC-0111 C3)"
            )
    for forbidden in (
        "update_identity",
        "modify_identity",
        "rewrite_identity",
        "patch_self_model",
        "patch_need",
        "create_goal",
        "modify_goal",
        "patch_goal",
        "patch_drive",
        "write_memory",
        "save_memory",
        "store_memory",
        "create_memory",
        "create_persona",
    ):
        if hasattr(RelationshipProjector, forbidden):
            raise ConstitutionViolation(
                f"RelationshipProjector exposes authority-leak method '{forbidden}' "
                f"(forbidden by RFC-0111 C1/C2/C3)"
            )

    return True


def _sample_relationship_inputs():
    """Build a minimal (Memory, Identity, external_ref) triple for the Relationship mutation checks.

    Seeds one relation record for ``external_ref`` so the projection has something to model; the
    projection is checked to read these read-only (it must not mutate the sink or the root).
    """
    mem = Memory()
    mem.relations.append(
        MemoryRecord(
            kind="relation",
            content={"external_ref": "user_abu", "trust": 0.8, "role": "companion"},
            salience=0.7,
        )
    )
    identity = derive_identity(mem)
    return mem, identity, "user_abu"


def _sample_plan_inputs():
    """Build a minimal (Goal, Drive, Context) triple for the planner mutation check."""
    goal = Goal(
        target="understand X",
        success_criterion="can explain X",
        horizon="weeks",
        priority=0.8,
        lifecycle=GoalLifecycle.ACTIVE,
    )
    drive = Drive(
        orientation={
            OrientationAxis.APPROACH: 0.6,
            OrientationAxis.AVOID: 0.1,
            OrientationAxis.INTENSITY: 0.4,
            OrientationAxis.FOCUS: 0.5,
        },
        activated=True,
    )
    ctx = PlannerContext(capabilities=["knowledge"], environment={"safe_to_retry": True})
    return goal, drive, ctx


def _sample_execution_inputs():
    """Build a minimal (Plan, ExecutionContext) pair for the execution mutation check."""
    plan = Plan(
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
    ctx = ExecutionContext(capabilities=["knowledge"], environment={})
    return plan, ctx


def _sample_reflection_result() -> ReflectionResult:
    """Build a minimal ReflectionResult for the Memory Authority ingest check."""
    return ReflectionResult(
        result_ref="plan#understand-X#run-1",  # references the ExecutionResult, NOT Goal/Plan object
        records=[
            {"kind": "semantic", "content": {"skill": "explain X"}, "salience": 0.8},
            {"kind": "episode", "content": {"event": "solved X"}, "salience": 0.6},
        ],
    )


def _sample_execution_result() -> ExecutionResult:
    """Build a minimal ExecutionResult for the Reflection transform mutation check."""
    return ExecutionResult(
        plan_ref="t1|t2|t3",  # deterministic ref of the consumed Plan (NOT a Goal reference)
        status=ExecutionStatus.SUCCESS,
        outcome="executed plan for 'understand X'",
        lessons=[
            "carried 3 task(s) toward 'understand X'",
            "drive emphasis at plan time = 'approach'",
        ],
    )


def _sample_identity_for_persona() -> Identity:
    """Build a minimal Identity for the Persona projection mutation check."""
    mem = Memory()
    mem.semantic.append(
        MemoryRecord(
            kind="semantic",
            content={"curiosity": 0.9, "value": "understanding", "voice": "warm"},
            salience=0.8,
        )
    )
    return derive_identity(mem)
