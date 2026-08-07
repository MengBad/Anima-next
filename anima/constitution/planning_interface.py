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

Lifecycle (T2.2): the §7.1 transition table is encoded as data (``_LEGAL_TRANSITIONS``)
and every state write funnels through the single private ``_transition`` — the lifecycle
has exactly one owner in code, as it does in the RFC. ``superseded`` (a replacement
exists) and ``deprecated`` (no replacement) are both **terminal**: a discarded Plan is
never re-transitioned and never restored (RFC-0106 V5).

Slice scope. T2.1 established the interface skeleton + registry; T2.2 completes the §7
lifecycle semantics. The actual Plan *construction* (resolving ``goal_ref``/
``context_ref`` to Goal/Drive/Context and calling ``derive_plan``) is the runtime-wiring
slice (T3) and is intentionally NOT performed here: ``request_plan``/``revise_plan``
therefore produce a ``draft`` Plan shell carrying correct identity/version/source/
status/created_at; population of ``task_graph``/etc. is deferred to T3.

Deferred (cross-boundary, deliberately NOT decided here):
- Whether a Goal may hold more than one ``active`` Plan at a time. §7 states no
  uniqueness invariant, and ``request_plan`` is additive by design (only ``revise_plan``
  supersedes). Which ``active`` Plan a consumer picks is RFC-0003 (Workflow) territory.
- Consumption itself (``active`` → discarded) is Workflow's read-only act (§7.1); no
  ``consume_plan`` operation exists here and none may be added by RFC-0002.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from anima.constitution.errors import ConstitutionViolation
from anima.constitution.state import PLAN_CONTRACT_VERSION, Plan, PlanSource, PlanStatus


def _now_iso() -> str:
    """Construction timestamp — assigned at construction time, never by storage (§8)."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# §7 lifecycle model — the single source of transition truth
# ---------------------------------------------------------------------------
#: States that END a Plan's life. A Plan is *discarded* — never archived, never restored
#: (RFC-0002 §7 table / RFC-0106 §5.4 V5) — so a terminal Plan has NO outgoing transition
#: and can never be re-superseded, re-invalidated, or re-activated.
_TERMINAL_STATES: frozenset[PlanStatus] = frozenset(
    {PlanStatus.SUPERSEDED, PlanStatus.DEPRECATED}
)

#: The complete RFC-0002 §7.1 transition table, encoded as data so the lifecycle
#: invariant is mechanically checkable instead of being scattered across operations.
#: Any ``(from -> to)`` pair absent here is illegal and is rejected by ``_transition``.
#: There is deliberately no ``archived`` / ``done`` / ``adopted`` target: Plan completion
#: is the Goal's lifecycle (RFC-0105) and RFC-0003's run-state, never the Plan's (§7).
_LEGAL_TRANSITIONS: dict[PlanStatus, frozenset[PlanStatus]] = {
    # draft --activate--> active | --revise--> superseded | --invalidate--> deprecated
    PlanStatus.DRAFT: frozenset(
        {PlanStatus.ACTIVE, PlanStatus.SUPERSEDED, PlanStatus.DEPRECATED}
    ),
    # active --revise--> superseded | --invalidate--> deprecated
    # (Workflow consumption is read-only: it discards, it does NOT transition — §7.1)
    PlanStatus.ACTIVE: frozenset({PlanStatus.SUPERSEDED, PlanStatus.DEPRECATED}),
    PlanStatus.SUPERSEDED: frozenset(),  # terminal
    PlanStatus.DEPRECATED: frozenset(),  # terminal
}


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
        self._planner = planner  # M1.6 construction impl (seam for T3); still unused at T2.2
        self._registry: dict[str, Plan] = {}  # in-process ONLY — never persisted (§8)

    # ---- single-writer enclosure (internal helpers) ----
    def _require(self, plan_id: str) -> Plan:
        plan = self._registry.get(plan_id)
        if plan is None:
            raise KeyError(f"Plan not found: {plan_id!r}")
        return plan

    def _transition(
        self,
        plan: Plan,
        target: PlanStatus,
        *,
        operation: str,
        permitted_from: tuple[PlanStatus, ...],
    ) -> Plan:
        """The **only** place a Plan's runtime state is written (§7.1 single owner / O1).

        Every lifecycle move funnels through here, so "who may transition a Plan" has
        exactly one answer in code as it does in the RFC. Two guards, applied in order:

        1. **Operation guard** — the source states this specific operation permits
           (§6/§7.1 per-operation rule, e.g. ``activate_plan`` only from ``draft``).
        2. **Table guard** — the global §7.1 transition table (defence in depth: a
           terminal Plan can never move, whatever an operation claims it wants).

        A rejected transition raises ``ConstitutionViolation`` and leaves the Plan
        untouched — an invalid transition is never a partial one.
        """
        current = plan.status
        if current not in permitted_from:
            allowed = "/".join(state.value.upper() for state in permitted_from)
            raise ConstitutionViolation(
                f"{operation} requires {allowed} state, got {current.value!r}"
            )
        if target not in _LEGAL_TRANSITIONS[current]:
            raise ConstitutionViolation(
                f"illegal Plan transition {current.value!r} -> {target.value!r} "
                f"via {operation}: not permitted by the RFC-0002 §7.1 transition table"
                + (" (terminal state)" if current in _TERMINAL_STATES else "")
            )
        plan.status = target
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
        """Move a Plan from ``draft`` to ``active`` — the single draft→active transition (§7.1).

        Permitted **only** from ``draft``: re-activating an ``active`` Plan, or reviving a
        ``superseded``/``deprecated`` one, is rejected (a discarded Plan is never restored —
        RFC-0106 V5). Workflow never self-activates; it consumes an already-``active`` Plan.
        """
        return self._transition(
            self._require(plan_id),
            PlanStatus.ACTIVE,
            operation="activate_plan",
            permitted_from=(PlanStatus.DRAFT,),
        )

    def read_plan(self, plan_id: str) -> Plan:
        """Return the current Plan structure (introspection / RFC-0003 consumption).

        Read-only with respect to the lifecycle: reading never transitions a Plan, and
        consumption by Workflow (§7.1 ``consume``) likewise leaves the state unchanged —
        the Plan ends by being discarded, not by a consumer-driven transition.
        """
        return self._require(plan_id)

    def revise_plan(self, goal_ref: str, reason: str) -> Plan:
        """Produce a NEW Plan for the same Goal; supersede prior Plans (§7.1).

        Lifecycle semantics (§7):

        - The replacement is constructed **first**; priors are superseded only once a
          successor actually exists, so a failed construction can never leave a Goal with
          all Plans superseded and no replacement (``superseded`` *implies* a replacement).
        - Only ``draft``/``active`` priors move. Already-terminal Plans are skipped, never
          re-transitioned — a ``deprecated`` Plan stays ``deprecated``; supersede and
          invalidate are distinct lifecycle events and must not overwrite each other.
        - The prior Plan is only *status*-transitioned and dropped; its content is never
          mutated and never archived (RFC-0106 V5). The successor is a fresh Plan with a
          new ``id`` — there is no ``Plan.restore()``.
        - No prior Plan is not an error: after a crash the registry is empty (transient,
          §8) and re-planning is always permitted (G4).

        ``reason`` is accepted per the §6 contract for audit but is not persisted (§8).
        """
        # Construct the successor before touching any prior state (see docstring).
        new_plan = Plan(
            id=uuid.uuid4().hex,
            goal_ref=goal_ref,
            contract_version=PLAN_CONTRACT_VERSION,
            source=PlanSource.PLANNER,
            status=PlanStatus.DRAFT,
            created_at=_now_iso(),
        )
        for plan in self._registry.values():
            if plan.goal_ref != goal_ref or plan.status in _TERMINAL_STATES:
                continue
            self._transition(
                plan,
                PlanStatus.SUPERSEDED,
                operation="revise_plan",
                permitted_from=(PlanStatus.DRAFT, PlanStatus.ACTIVE),
            )
        # Registered last: the successor can never supersede itself.
        self._registry[new_plan.id] = new_plan
        return new_plan

    def list_plans(self, goal_ref: str) -> list[Plan]:
        """List Plans (incl. superseded) for a Goal — live registry only (§8, in-process).

        Read-only: listing never transitions a Plan.
        """
        return [plan for plan in self._registry.values() if plan.goal_ref == goal_ref]

    def invalidate_plan(self, plan_id: str, reason: str) -> Plan:
        """Declare a Plan INVALID without replacement → ``deprecated`` (§7.1).

        Valid only from ``draft``/``active``; a ``superseded``/``deprecated`` Plan cannot be
        re-invalidated (terminal). ``deprecated`` implies **no** replacement — that is what
        distinguishes it from ``superseded``, which asserts a successor exists. Workflow
        (RFC-0003) MUST NOT consume a ``deprecated`` Plan; the Plan is discarded, not
        archived, and deprecating one does **not** block a later ``request_plan`` for the
        same Goal (§7 table, G4). ``reason`` is audit metadata, not persisted (§8).
        """
        return self._transition(
            self._require(plan_id),
            PlanStatus.DEPRECATED,
            operation="invalidate_plan",
            permitted_from=(PlanStatus.DRAFT, PlanStatus.ACTIVE),
        )
