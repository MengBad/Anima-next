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
from anima.constitution.need import derive_need
from anima.constitution.planner import derive_plan
from anima.constitution.state import (
    Affect,
    Drive,
    ExecutionContext,
    ExecutionResult,
    ExecutionStatus,
    Goal,
    GoalLifecycle,
    Memory,
    MemoryRecord,
    Need,
    OrientationAxis,
    Perception,
    Plan,
    PlannerContext,
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
    for cls in (Need, Affect, Drive, Plan, ExecutionResult):
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

    return True


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
