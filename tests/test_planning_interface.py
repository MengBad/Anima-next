"""RFC-0002 §6 / §7 — Planning Interface + Plan lifecycle (T2.1 / T2.2 / T3.2) smoke test.

Validates the Planning Authority's stable API surface: the six operations, the
runtime lifecycle transitions, the single-writer enclosure, and the transient
(in-process, never-persisted) registry boundary. Failures here mean the interface
drifted from RFC-0002 §6/§7 — fix the code, not the contract.
"""
import pathlib
import re

from anima.constitution.planning_interface import (
    _LEGAL_TRANSITIONS,
    _TERMINAL_STATES,
    PlanningInterface,
)
from anima.constitution.state import (
    PLAN_CONTRACT_VERSION,
    Drive,
    Goal,
    Plan,
    PlanSource,
    PlanStatus,
    PlannerContext,
    Task,
)


def _new_interface() -> PlanningInterface:
    return PlanningInterface()


def _derive_inputs() -> tuple[Goal, Drive, PlannerContext]:
    """Canonical, deterministic derivation inputs shared by the request/revise helpers."""
    return (
        Goal(target="ship feature", success_criterion="tests pass", horizon="weeks"),
        Drive(orientation={"approach": 0.8}),
        PlannerContext(capabilities=["knowledge"], environment={"safe_to_retry": True}),
    )


def _request(iface: PlanningInterface, goal_ref: str) -> Plan:
    """Thin wrapper: request_plan with the canonical deterministic inputs."""
    goal, drive, context = _derive_inputs()
    return iface.request_plan(goal_ref, goal=goal, drive=drive, context=context)


def _revise(iface: PlanningInterface, goal_ref: str, reason: str) -> Plan:
    """Thin wrapper: revise_plan with the canonical deterministic inputs."""
    goal, drive, context = _derive_inputs()
    return iface.revise_plan(
        goal_ref, goal=goal, drive=drive, context=context, reason=reason
    )


# ---------------------------------------------------------------------------
# T3.2 — creation path wired to derive_plan (no longer an empty shell)
# ---------------------------------------------------------------------------
def test_request_plan_creates_draft_populated_by_derive_plan():
    iface = _new_interface()
    plan = _request(iface, "goal-1")

    assert plan.id  # assigned at construction
    assert plan.goal_ref == "goal-1"
    assert plan.contract_version == PLAN_CONTRACT_VERSION
    assert plan.source is PlanSource.PLANNER
    assert plan.status is PlanStatus.DRAFT
    assert plan.created_at  # timestamp assigned
    # T3.2: task_graph is now populated by derive_plan (canonical RFC-0002 §5 Tasks),
    # not an empty shell.
    assert plan.task_graph, "request_plan must populate task_graph via derive_plan"
    assert all(isinstance(t, Task) for t in plan.task_graph)
    assert [t.id for t in plan.task_graph] == ["t1", "t2", "t3"]
    assert plan.ordering == ["t1", "t2", "t3"]
    # context_ref is derived deterministically from the PlannerContext (audit only).
    assert plan.context_ref


def test_request_plan_content_matches_derive_plan():
    """The interface must attach identity fields only — never alter derive_plan's content."""
    from anima.constitution.planner import derive_plan

    goal, drive, context = _derive_inputs()
    expected = derive_plan(goal, drive, context)
    iface = _new_interface()
    plan = iface.request_plan("goal-1", goal=goal, drive=drive, context=context)

    assert plan.task_graph == expected.task_graph
    assert plan.ordering == expected.ordering
    assert plan.goal_target == expected.goal_target
    assert plan.context_ref == expected.context_ref
    assert plan.emphasis == expected.emphasis
    assert plan.branching == expected.branching
    assert plan.rollback == expected.rollback


def test_request_plan_task_graph_is_canonical_and_deterministic():
    """derive_plan stays pure: same inputs ⇒ same content; only key + timestamp vary."""
    iface = _new_interface()
    p1 = _request(iface, "goal-1")
    p2 = _request(iface, "goal-1")

    assert p1.id != p2.id  # construction-boundary uuid, never part of deterministic content
    assert p1.task_graph == p2.task_graph
    assert p1.ordering == p2.ordering
    assert p1.goal_target == p2.goal_target
    assert p1.context_ref == p2.context_ref
    assert p1.emphasis == p2.emphasis


def test_revise_plan_successor_is_populated_by_derive_plan():
    """The revised (superseding) Plan is also fully populated, not an empty shell."""
    iface = _new_interface()
    _request(iface, "goal-1")
    successor = _revise(iface, "goal-1", reason="context change")

    assert successor.task_graph, "revise_plan successor must be populated by derive_plan"
    assert all(isinstance(t, Task) for t in successor.task_graph)
    assert [t.id for t in successor.task_graph] == ["t1", "t2", "t3"]


def test_activate_plan_draft_to_active():
    iface = _new_interface()
    plan = _request(iface, "goal-1")

    activated = iface.activate_plan(plan.id)
    assert activated.status is PlanStatus.ACTIVE
    # read_plan reflects the transition (single registry).
    assert iface.read_plan(plan.id).status is PlanStatus.ACTIVE


def test_activate_plan_rejects_non_draft():
    iface = _new_interface()
    plan = _request(iface, "goal-1")
    iface.activate_plan(plan.id)

    raised = False
    try:
        iface.activate_plan(plan.id)  # already ACTIVE, not DRAFT
    except Exception as e:  # noqa: BLE001 - asserting the guard fires
        raised = True
        assert "DRAFT" in str(e)
    assert raised, "activate_plan must reject non-DRAFT states (single transition)"


def test_read_plan_returns_current_structure():
    iface = _new_interface()
    plan = _request(iface, "goal-1")
    assert iface.read_plan(plan.id) is plan  # same object from the live registry


def test_revise_plan_supersedes_prior_and_emits_new_draft():
    iface = _new_interface()
    p1 = _request(iface, "goal-1")
    iface.activate_plan(p1.id)
    p2 = _revise(iface, "goal-1", reason="context change")

    # Prior becomes SUPERSEDED (never mutated, never archived).
    assert iface.read_plan(p1.id).status is PlanStatus.SUPERSEDED
    # New plan is a fresh DRAFT with a distinct id for the same goal.
    assert p2.id != p1.id
    assert p2.goal_ref == "goal-1"
    assert p2.status is PlanStatus.DRAFT
    # Both are listed for the goal (incl. superseded) — live registry only.
    listed = iface.list_plans("goal-1")
    assert {p.id for p in listed} == {p1.id, p2.id}


def test_invalidate_plan_draft_to_deprecated():
    iface = _new_interface()
    plan = _request(iface, "goal-1")

    deprecated = iface.invalidate_plan(plan.id, reason="goal cancelled")
    assert deprecated.status is PlanStatus.DEPRECATED
    assert iface.read_plan(plan.id).status is PlanStatus.DEPRECATED


def test_invalidate_plan_rejects_already_superseded_or_deprecated():
    iface = _new_interface()
    p1 = _request(iface, "goal-1")
    _revise(iface, "goal-1", reason="x")  # p1 -> SUPERSEDED

    raised = False
    try:
        iface.invalidate_plan(p1.id, reason="y")
    except Exception as e:  # noqa: BLE001
        raised = True
        assert "DRAFT/ACTIVE" in str(e)
    assert raised, "invalidate_plan must reject already-SUPERSEDED/DEPRECATED Plans"


def test_registry_is_in_process_only_no_persistence():
    iface = _new_interface()
    a = _request(iface, "goal-a")
    b = _request(iface, "goal-a")
    c = _request(iface, "goal-b")

    # Single in-process registry (no DB / json / archive).
    assert isinstance(iface._registry, dict)
    assert len(iface._registry) == 3
    # list_plans filters by goal_ref over the live registry only.
    assert [p.id for p in iface.list_plans("goal-a")] == [a.id, b.id]
    assert [p.id for p in iface.list_plans("goal-b")] == [c.id]
    # Unique ids across constructions.
    assert len({a.id, b.id, c.id}) == 3


# ---------------------------------------------------------------------------
# T2.2 — §7 lifecycle semantics
# ---------------------------------------------------------------------------
def test_transition_table_matches_rfc_0002_section_7_1():
    """The encoded table IS the §7.1 table — no extra edge, no missing edge."""
    assert _LEGAL_TRANSITIONS == {
        PlanStatus.DRAFT: frozenset(
            {PlanStatus.ACTIVE, PlanStatus.SUPERSEDED, PlanStatus.DEPRECATED}
        ),
        PlanStatus.ACTIVE: frozenset({PlanStatus.SUPERSEDED, PlanStatus.DEPRECATED}),
        PlanStatus.SUPERSEDED: frozenset(),
        PlanStatus.DEPRECATED: frozenset(),
    }
    # Every PlanStatus is covered — a new state cannot silently bypass the guard.
    assert set(_LEGAL_TRANSITIONS) == set(PlanStatus)
    # No `archived` / `done` / `adopted` target exists (§7: Plan is discarded, not archived).
    assert {PlanStatus.SUPERSEDED, PlanStatus.DEPRECATED} == _TERMINAL_STATES


def test_terminal_states_have_no_outgoing_transition():
    for terminal in _TERMINAL_STATES:
        assert _LEGAL_TRANSITIONS[terminal] == frozenset(), f"{terminal} must be terminal"


def test_activate_plan_rejects_superseded_and_deprecated():
    """A discarded Plan is never revived (RFC-0106 V5 — recomputed, not restored)."""
    iface = _new_interface()
    superseded = _request(iface, "goal-1")
    _revise(iface, "goal-1", reason="re-plan")  # -> SUPERSEDED
    deprecated = _request(iface, "goal-2")
    iface.invalidate_plan(deprecated.id, reason="goal cancelled")  # -> DEPRECATED

    for plan in (superseded, deprecated):
        raised = False
        try:
            iface.activate_plan(plan.id)
        except Exception as e:  # noqa: BLE001 - asserting the guard fires
            raised = True
            assert "DRAFT" in str(e)
        assert raised, f"activate_plan must reject {plan.status}"


def test_revise_plan_supersedes_draft_prior_too():
    """§7 diagram: revise from `draft` supersedes it as well, not only from `active`."""
    iface = _new_interface()
    p1 = _request(iface, "goal-1")  # stays DRAFT
    p2 = _revise(iface, "goal-1", reason="context change")

    assert iface.read_plan(p1.id).status is PlanStatus.SUPERSEDED
    assert p2.status is PlanStatus.DRAFT


def test_revise_plan_does_not_retransition_terminal_plans():
    """`superseded` and `deprecated` are distinct events — neither overwrites the other."""
    iface = _new_interface()
    deprecated = _request(iface, "goal-1")
    iface.invalidate_plan(deprecated.id, reason="context stale")
    already_superseded = _request(iface, "goal-1")
    _revise(iface, "goal-1", reason="first re-plan")  # supersedes `already_superseded`

    _revise(iface, "goal-1", reason="second re-plan")

    # The deprecated Plan was NOT flipped to superseded by a later revise.
    assert iface.read_plan(deprecated.id).status is PlanStatus.DEPRECATED
    assert iface.read_plan(already_superseded.id).status is PlanStatus.SUPERSEDED


def test_revise_plan_never_supersedes_itself_or_other_goals():
    iface = _new_interface()
    other = _request(iface, "goal-2")
    new_plan = _revise(iface, "goal-1", reason="no prior exists")

    # Fresh successor stays DRAFT; a foreign Goal's Plan is untouched.
    assert new_plan.status is PlanStatus.DRAFT
    assert iface.read_plan(other.id).status is PlanStatus.DRAFT


def test_revise_plan_without_prior_is_permitted():
    """Post-crash the registry is empty (§8, transient) — re-planning is always allowed (G4)."""
    iface = _new_interface()
    plan = _revise(iface, "goal-never-seen", reason="crash recovery")
    assert plan.status is PlanStatus.DRAFT
    assert iface.list_plans("goal-never-seen") == [plan]


def test_deprecated_plan_does_not_block_new_request_plan():
    """§7 table / G4: invalidation implies no replacement, not a ban on re-planning."""
    iface = _new_interface()
    old = _request(iface, "goal-1")
    iface.invalidate_plan(old.id, reason="goal cancelled")

    fresh = _request(iface, "goal-1")
    assert fresh.status is PlanStatus.DRAFT
    assert iface.activate_plan(fresh.id).status is PlanStatus.ACTIVE
    assert iface.read_plan(old.id).status is PlanStatus.DEPRECATED  # unchanged


def test_request_plan_does_not_transition_existing_plans():
    """Only `revise_plan` supersedes (§7); `request_plan` is additive by design."""
    iface = _new_interface()
    first = _request(iface, "goal-1")
    iface.activate_plan(first.id)

    _request(iface, "goal-1")
    assert iface.read_plan(first.id).status is PlanStatus.ACTIVE


def test_rejected_transition_leaves_plan_untouched():
    """An invalid transition is never a partial one."""
    iface = _new_interface()
    plan = _request(iface, "goal-1")
    iface.activate_plan(plan.id)
    before = plan.status

    try:
        iface.activate_plan(plan.id)
    except Exception:  # noqa: BLE001
        pass
    assert plan.status is before


def test_read_and_list_are_read_only():
    """Consumption (§7.1) never transitions a Plan — Workflow discards, it does not move state."""
    iface = _new_interface()
    plan = _request(iface, "goal-1")
    iface.activate_plan(plan.id)

    for _ in range(3):
        iface.read_plan(plan.id)
        iface.list_plans("goal-1")
    assert iface.read_plan(plan.id).status is PlanStatus.ACTIVE


def test_planning_interface_is_the_only_plan_lifecycle_writer():
    """Single-writer rule (O1) enforced mechanically over the source tree.

    No module other than ``planning_interface`` may assign a Plan runtime state, and
    inside it every write must go through the single ``_transition`` method.
    """
    root = pathlib.Path(__file__).resolve().parents[1] / "anima"
    owner = root / "constitution" / "planning_interface.py"
    external_writer = re.compile(r"\.status\s*=\s*PlanStatus\.")

    offenders = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if path != owner and external_writer.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"Plan lifecycle written outside the Planning Interface: {offenders}"

    # Inside the owner, exactly ONE state-assignment site exists: the `_transition` funnel.
    source = owner.read_text(encoding="utf-8")
    assert len(re.findall(r"\.status\s*=(?!=)", source)) == 1
    assert "plan.status = target" in source


def test_public_surface_is_exactly_the_six_rfc_operations():
    """T2.2 completes lifecycle behaviour — it must not grow the §6 interface."""
    public = {
        name
        for name in vars(PlanningInterface)
        if not name.startswith("_") and callable(getattr(PlanningInterface, name))
    }
    assert public == {
        "request_plan",
        "activate_plan",
        "read_plan",
        "revise_plan",
        "list_plans",
        "invalidate_plan",
    }


def test_existing_planner_untouched_compatibility():
    # The legacy M1.6 Planner remains importable and functional (compatibility).
    from anima.constitution.planner import Planner, derive_plan

    goal = __import__(
        "anima.constitution.state", fromlist=["Goal"]
    ).Goal(target="t", success_criterion="s", horizon="weeks")
    drive = __import__(
        "anima.constitution.state", fromlist=["Drive"]
    ).Drive(orientation={"approach": 0.8})
    ctx = __import__(
        "anima.constitution.state", fromlist=["PlannerContext"]
    ).PlannerContext(capabilities=["knowledge"], environment={"safe_to_retry": True})

    assert isinstance(derive_plan(goal, drive, ctx), Plan)
    planner = Planner()
    planner.adopt_goal(goal)  # M1.6 requires an adopted active Goal before building
    assert planner.build_plan(drive, ctx) is not None
