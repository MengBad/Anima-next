"""Planning Interface — the Planning Authority's stable API (RFC-0002 §6 / §7).

Realizes the *interface* the Planning Authority owns. It is the **sole** writer of
Plan runtime state (single-writer rule O1, RFC-0106 §6.2 / RFC-0002 §6 authority
boundary): the in-process Plan registry and every lifecycle transition
(``draft``→``active``, ``draft``/``active``→``superseded``, ``draft``/``active``→
``deprecated``) are performed *only* through this interface.

Transient boundary (RFC-0002 §8): the registry is an **in-process** dict, never
persisted — no database, no json store, no history archive, no superseded-archive.
``list_plans`` reads the live registry only. Plan realization is recomputed, never
restored (RFC-0106 V5).

This slice (T2.1) implements the interface skeleton + lifecycle + registry. The
actual Plan *construction* (resolving ``goal_ref``/``context_ref`` to Goal/Drive/
Context and calling ``derive_plan``) is the runtime-wiring slice (T3) and is
intentionally NOT performed here: ``request_plan``/``revise_plan`` therefore produce
a ``draft`` Plan shell carrying correct identity/version/source/status/created_at;
population of ``task_graph``/etc. is deferred to T3.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from anima.constitution.errors import ConstitutionViolation
from anima.constitution.state import PLAN_CONTRACT_VERSION, Plan, PlanSource, PlanStatus


def _now_iso() -> str:
    """Construction timestamp — assigned at construction time, never by storage (§8)."""
    return datetime.now(timezone.utc).isoformat()


class PlanningInterface:
    """The Planning Authority's stable API surface (RFC-0002 §6).

    Owns Plan lifecycle transitions (§7) and the in-process Plan registry.
    **Single-writer**: only this interface mutates Plan state (O1). The actual
    Plan *construction* (``derive_plan``) is delegated to a constructor supplied by
    the runtime (T3 wiring); for T2.1 the interface manages lifecycle + registry,
    and ``request_plan``/``revise_plan`` emit a ``draft`` shell whose ``task_graph``
    is populated later (T3). The optional ``planner`` is the M1.6 construction impl
    (RFC-0002 §6: "the Planner is its implementation"); it is a documented seam for
    T3 and is not invoked in this slice.
    """

    def __init__(self, planner: object | None = None) -> None:
        self._planner = planner  # M1.6 construction impl (seam for T3); unused at T2.1
        self._registry: dict[str, Plan] = {}  # in-process ONLY — never persisted (§8)

    # ---- single-writer enclosure (internal helpers) ----
    def _require(self, plan_id: str) -> Plan:
        plan = self._registry.get(plan_id)
        if plan is None:
            raise KeyError(f"Plan not found: {plan_id!r}")
        return plan

    # ---- §6 operations ----
    def request_plan(self, goal_ref: str, context_ref: str) -> Plan:
        """Produce a fresh Plan for an active Goal (RFC-0002 §6).

        Creates a ``draft`` Plan carrying correct identity / version / source /
        status / created_at / context_ref. Population of ``task_graph``/etc. is the
        runtime-wiring slice (T3); until then the Plan is a ``draft`` shell, not yet
        consumable by Workflow (it must be ``activate_plan``-ed first).
        """
        plan = Plan(
            id=uuid.uuid4().hex,
            goal_ref=goal_ref,
            context_ref=context_ref,
            contract_version=PLAN_CONTRACT_VERSION,
            source=PlanSource.PLANNER,
            status=PlanStatus.DRAFT,
            created_at=_now_iso(),
        )
        self._registry[plan.id] = plan
        return plan

    def activate_plan(self, plan_id: str) -> Plan:
        """Move a Plan from ``draft`` to ``active`` — the single draft→active transition (§7.1)."""
        plan = self._require(plan_id)
        if plan.status is not PlanStatus.DRAFT:
            raise ConstitutionViolation(
                f"activate_plan requires DRAFT state, got {plan.status.value!r}"
            )
        plan.status = PlanStatus.ACTIVE
        return plan

    def read_plan(self, plan_id: str) -> Plan:
        """Return the current Plan structure (introspection / RFC-0003 consumption)."""
        return self._require(plan_id)

    def revise_plan(self, goal_ref: str, reason: str) -> Plan:
        """Produce a NEW Plan for the same Goal; supersede prior Plans (§7.1).

        The prior DRAFT/ACTIVE Plans for ``goal_ref`` become ``superseded`` (never
        mutated, never archived — RFC-0106 V5). ``reason`` is accepted per the
        contract for audit but is not persisted (§8 forbids persistence).
        """
        for plan in self._registry.values():
            if plan.goal_ref == goal_ref and plan.status in (
                PlanStatus.DRAFT,
                PlanStatus.ACTIVE,
            ):
                plan.status = PlanStatus.SUPERSEDED
        new_plan = Plan(
            id=uuid.uuid4().hex,
            goal_ref=goal_ref,
            contract_version=PLAN_CONTRACT_VERSION,
            source=PlanSource.PLANNER,
            status=PlanStatus.DRAFT,
            created_at=_now_iso(),
        )
        self._registry[new_plan.id] = new_plan
        return new_plan

    def list_plans(self, goal_ref: str) -> list[Plan]:
        """List Plans (incl. superseded) for a Goal — live registry only (§8, in-process)."""
        return [plan for plan in self._registry.values() if plan.goal_ref == goal_ref]

    def invalidate_plan(self, plan_id: str, reason: str) -> Plan:
        """Declare a Plan INVALID without replacement → ``deprecated`` (§7.1).

        Valid only from DRAFT/ACTIVE; a ``superseded``/``deprecated`` Plan cannot be
        re-invalidated. Workflow (RFC-0003) MUST NOT consume a ``deprecated`` Plan.
        ``reason`` is audit metadata, not persisted (§8).
        """
        plan = self._require(plan_id)
        if plan.status in (PlanStatus.SUPERSEDED, PlanStatus.DEPRECATED):
            raise ConstitutionViolation(
                f"invalidate_plan requires DRAFT/ACTIVE state, got {plan.status.value!r}"
            )
        plan.status = PlanStatus.DEPRECATED
        return plan
