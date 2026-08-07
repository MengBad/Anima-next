"""RFC-0002 §6 / §7 — Planning Interface (T2.1) smoke test.

Validates the Planning Authority's stable API surface: the six operations, the
runtime lifecycle transitions, the single-writer enclosure, and the transient
(in-process, never-persisted) registry boundary. Failures here mean the interface
drifted from RFC-0002 §6/§7 — fix the code, not the contract.
"""
from anima.constitution.planning_interface import PlanningInterface
from anima.constitution.state import PLAN_CONTRACT_VERSION, Plan, PlanSource, PlanStatus


def _new_interface() -> PlanningInterface:
    return PlanningInterface()


def test_request_plan_creates_draft_shell():
    iface = _new_interface()
    plan = iface.request_plan("goal-1", "ctx-a")

    assert plan.id  # assigned at construction
    assert plan.goal_ref == "goal-1"
    assert plan.context_ref == "ctx-a"
    assert plan.contract_version == PLAN_CONTRACT_VERSION
    assert plan.source is PlanSource.PLANNER
    assert plan.status is PlanStatus.DRAFT
    assert plan.created_at  # timestamp assigned
    # Transient shell: task_graph not yet populated (population is T3 wiring).
    assert plan.task_graph == []


def test_activate_plan_draft_to_active():
    iface = _new_interface()
    plan = iface.request_plan("goal-1", "ctx-a")

    activated = iface.activate_plan(plan.id)
    assert activated.status is PlanStatus.ACTIVE
    # read_plan reflects the transition (single registry).
    assert iface.read_plan(plan.id).status is PlanStatus.ACTIVE


def test_activate_plan_rejects_non_draft():
    iface = _new_interface()
    plan = iface.request_plan("goal-1", "ctx-a")
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
    plan = iface.request_plan("goal-1", "ctx-a")
    assert iface.read_plan(plan.id) is plan  # same object from the live registry


def test_revise_plan_supersedes_prior_and_emits_new_draft():
    iface = _new_interface()
    p1 = iface.request_plan("goal-1", "ctx-a")
    iface.activate_plan(p1.id)
    p2 = iface.revise_plan("goal-1", reason="context change")

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
    plan = iface.request_plan("goal-1", "ctx-a")

    deprecated = iface.invalidate_plan(plan.id, reason="goal cancelled")
    assert deprecated.status is PlanStatus.DEPRECATED
    assert iface.read_plan(plan.id).status is PlanStatus.DEPRECATED


def test_invalidate_plan_rejects_already_superseded_or_deprecated():
    iface = _new_interface()
    p1 = iface.request_plan("goal-1", "ctx-a")
    iface.revise_plan("goal-1", reason="x")  # p1 -> SUPERSEDED

    raised = False
    try:
        iface.invalidate_plan(p1.id, reason="y")
    except Exception as e:  # noqa: BLE001
        raised = True
        assert "DRAFT/ACTIVE" in str(e)
    assert raised, "invalidate_plan must reject already-SUPERSEDED/DEPRECATED Plans"


def test_registry_is_in_process_only_no_persistence():
    iface = _new_interface()
    a = iface.request_plan("goal-a", "ctx-a")
    b = iface.request_plan("goal-a", "ctx-b")
    c = iface.request_plan("goal-b", "ctx-c")

    # Single in-process registry (no DB / json / archive).
    assert isinstance(iface._registry, dict)
    assert len(iface._registry) == 3
    # list_plans filters by goal_ref over the live registry only.
    assert [p.id for p in iface.list_plans("goal-a")] == [a.id, b.id]
    assert [p.id for p in iface.list_plans("goal-b")] == [c.id]
    # Unique ids across constructions.
    assert len({a.id, b.id, c.id}) == 3


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
