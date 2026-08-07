"""Planner Interface (M1.6) — the Planning Authority's plan construction.

RFC-0106: ``Plan = p(Goal, Drive, Context)``. The Planning Authority *constructs* Plan
(transient, derived) by reading Goal (owned target identity), Drive (orientation), and
Context (situational snapshot) — and mutating NONE of them (RFC-0106 C1 / C2 / C3).

- ``derive_plan(goal, drive, context)`` is the pure derivation ``p``: same inputs ⇒ same
  Plan; no side effects; reads its upstreams read-only (guarded by
  ``invariants.assert_no_cross_authority_mutation``).
- ``Planner`` is the lifecycle owner: it *owns* plan construction and may *cancel* a plan,
  but it never mutates Goal / Drive / Context. Single-writer (O1): only the Planner may
  build a Plan for a given active Goal. This is the first behavioral layer that touches
  ``Goal + Drive + Plan`` together; it is deliberately the *reader* of all three, never
  their editor — closing the Authority-leak the user named at this joint.
"""
from __future__ import annotations

from anima.constitution.errors import ConstitutionViolation
from anima.constitution.state import Drive, Goal, OrientationAxis, Plan, PlannerContext


def derive_plan(goal: Goal, drive: Drive, context: PlannerContext) -> Plan:
    """``p(Goal, Drive, Context)`` — crystallize an operational structure.

    Heuristic (M1): decompose the Goal's target/success into an ordered task graph; use
    Drive orientation to pick approach/avoid emphasis and Context capabilities to choose
    branches and detect safe-to-retry rollback. Pure: same ``(goal, drive, context)`` ⇒
    same Plan. Reads upstreams read-only — must not mutate goal/drive/context.
    """
    approach = float(drive.orientation.get(OrientationAxis.APPROACH, 0.0))
    avoid = float(drive.orientation.get(OrientationAxis.AVOID, 0.0))
    focus = float(drive.orientation.get(OrientationAxis.FOCUS, 0.0))

    # Task graph: a minimal decomposition of the Goal (RFC-0106 §4.1).
    task_graph = [
        {"id": "t1", "op": "read", "about": goal.target},
        {"id": "t2", "op": "synthesize", "about": goal.success_criterion},
        {"id": "t3", "op": "present", "about": goal.target},
    ]
    ordering = ["t1", "t2", "t3"]

    # Branching (Workflow shape): if a capability is unavailable, fall back.
    branching = {
        "if_browser_missing": (
            "fallback_to_knowledge" if "browser" not in context.capabilities else "use_browser"
        )
    }

    # Rollback: revert to t1 if t2 fails and the context is safe-to-retry.
    rollback = {
        "on_t2_fail": "revert_to_t1" if context.environment.get("safe_to_retry") else "abort"
    }

    # Drive orientation is read as a signal (emphasis), never as the plan decider (C1).
    emphasis = "approach" if approach >= avoid else "avoid"

    # Deterministic, read-only reference to the Context that shaped this Plan (audit only).
    context_ref = "|".join(
        [
            ",".join(sorted(context.capabilities)),
            ",".join(f"{k}={v}" for k, v in sorted(context.environment.items())),
            ",".join(f"{k}={v}" for k, v in sorted(context.resources.items())),
        ]
    )

    return Plan(
        goal_target=goal.target,
        task_graph=task_graph,
        ordering=ordering,
        branching=branching,
        rollback=rollback,
        context_ref=context_ref,
        emphasis=emphasis,
        drive_emphasis=emphasis,  # RFC-0002 §5 field (kept in sync with legacy `emphasis`; exact rename deferred)
        focus=round(focus, 3),
    )


class Planner:
    """Planning Authority's plan-lifecycle owner (single-writer).

    Owns construction (via ``derive_plan``) and cancellation of Plans for the active Goal.
    Holds the active Goal it is pursuing; reads Drive + Context as signals. It NEVER mutates
    Goal / Drive / Context — those are owned by their own Authorities (RFC-0106 C2). The
    Planning Authority's two jobs (adopt Goal per RFC-0105 §6, construct Plan per RFC-0106)
    are both *its own*; owning both grants no right to edit the upstream states.
    """

    def __init__(self) -> None:
        self._active_goal: Goal | None = None
        self._active_plan: Plan | None = None

    def adopt_goal(self, goal: Goal) -> None:
        """Bind the active Goal this Planner pursues (read-only reference).

        Adoption is the Planning Authority's *own* act over Goal (RFC-0105 §6); this method
        only stores the reference. It does NOT mutate the Goal object.
        """
        self._active_goal = goal

    def build_plan(self, drive: Drive, context: PlannerContext) -> Plan:
        """Construct a fresh Plan for the active Goal (the ``p`` derivation)."""
        if self._active_goal is None:
            raise ConstitutionViolation("Planner has no active Goal to plan for")
        plan = derive_plan(self._active_goal, drive, context)
        self._active_plan = plan
        return plan

    def cancel_plan(self) -> None:
        """Discard the active Plan. Plan has no lifecycle, so cancellation = drop the
        reference (not archive). A new Plan is *re-constructed* next time, never *restored*
        (RFC-0106 V5)."""
        self._active_plan = None

    @property
    def active_plan(self) -> Plan | None:
        return self._active_plan
