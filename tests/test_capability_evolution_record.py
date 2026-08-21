"""RED/GREEN verification for P4.3 Evolution Record Lifecycle Prototype.

The route already forms a closed loop: propose change -> review change -> record
decision.  P4.3 adds *lifecycle completeness* of the Evolution Record: the record
is created from the gate decision, advances through a fixed ordered lifecycle, and
reaches a terminal (closed) state - all without ever executing the Evolution.

These tests import :mod:`anima.capability_evolution_contract` symbols that do not
yet exist; running them before implementation proves the gap (RED).
"""

from dataclasses import replace

from anima.capability_evolution_contract import (
    EvolutionProposalError,
    OfflineEvolutionPlanner,
    EvolutionRecord,
    verify_evolution_record,
    EVOLUTION_RECORD_LIFECYCLE,
    EVOLUTION_RECORD_TERMINAL,
)

NOW = "2026-08-11T04:00:00+08:00"


def _build_chain(decision="approved"):
    """Build a complete proposal->impact->review->gate->record chain."""
    planner = OfflineEvolutionPlanner()
    proposal = planner.propose(
        "prop-1",
        "RFC-0001",
        "observed contradiction: O1 states X but runtime does Y",
        "eliminated the inconsistency between O1 and runtime",
        "contraction",
        "true",
        "2026-08-11T01:00:00+08:00",
    )
    impact = planner.assess_impact(
        "imp-1", proposal, ("Foundation:O1",), "2026-08-11T01:05:00+08:00"
    )
    review = planner.review(
        "rev-1", proposal, impact, "approved", "2026-08-11T01:10:00+08:00"
    )
    rationale = "" if decision == "approved" else "rejected: insufficient evidence"
    gate = planner.record_gate(
        "gate-1",
        proposal,
        review,
        decision,
        "reviewer@agent",
        ("evidence:1",),
        "2026-08-11T01:15:00+08:00",
        rationale=rationale,
    )
    record = planner.record_evolution(
        "rec-1", proposal, review, gate, "recorder@agent", "2026-08-11T01:20:00+08:00"
    )
    return planner, proposal, impact, review, gate, record


def test_record_evolution_creates_not_executed_record():
    planner, proposal, impact, review, gate, record = _build_chain()
    assert isinstance(record, EvolutionRecord)
    # The Evolution is *recorded*, never executed.
    assert record.execution_status == "not_executed"
    # Lifecycle starts at its initial state.
    assert record.lifecycle_state == EVOLUTION_RECORD_LIFECYCLE[0]
    assert record.decision == "approved"


def test_record_binds_proposal_review_and_gate():
    planner, proposal, impact, review, gate, record = _build_chain()
    v = verify_evolution_record(record, proposal, review, gate)
    assert v.proposal_binding_intact is True
    assert v.review_binding_intact is True
    assert v.gate_binding_intact is True
    assert v.valid is True


def test_lifecycle_advances_only_one_step_forward():
    planner, proposal, impact, review, gate, record = _build_chain()
    # recorded -> open is a legal single-step advance.
    advanced = planner.advance_lifecycle(record, "open", "2026-08-11T02:00:00+08:00")
    assert advanced.lifecycle_state == "open"
    # Skipping a state (recorded -> closed) must be rejected.
    try:
        planner.advance_lifecycle(record, "closed", "2026-08-11T02:00:00+08:00")
        assert False, "skipping a lifecycle state should raise"
    except EvolutionProposalError:
        pass


def test_terminal_state_is_irreversible():
    planner, proposal, impact, review, gate, record = _build_chain()
    opened = planner.advance_lifecycle(record, "open", "2026-08-11T02:00:00+08:00")
    closed = planner.advance_lifecycle(opened, "closed", "2026-08-11T02:15:00+08:00")
    assert closed.lifecycle_state == "closed"
    # No transition is possible out of the terminal state.
    try:
        planner.advance_lifecycle(closed, "open", "2026-08-11T02:30:00+08:00")
        assert False, "terminal state must be irreversible"
    except EvolutionProposalError:
        pass


def test_lifecycle_completeness_only_when_terminal():
    planner, proposal, impact, review, gate, record = _build_chain()
    v_initial = verify_evolution_record(record, proposal, review, gate)
    # recorded (initial) is NOT complete.
    assert v_initial.lifecycle_complete is False

    opened = planner.advance_lifecycle(record, "open", "2026-08-11T02:00:00+08:00")
    v_open = verify_evolution_record(opened, proposal, review, gate)
    assert v_open.lifecycle_complete is False

    closed = planner.advance_lifecycle(opened, "closed", "2026-08-11T02:30:00+08:00")
    v_closed = verify_evolution_record(closed, proposal, review, gate)
    # closed (terminal) IS complete.
    assert v_closed.lifecycle_complete is True
    assert v_closed.valid is True


def test_execution_status_never_changes_through_lifecycle():
    planner, proposal, impact, review, gate, record = _build_chain()
    assert record.execution_status == "not_executed"
    opened = planner.advance_lifecycle(record, "open", "2026-08-11T02:00:00+08:00")
    assert opened.execution_status == "not_executed"
    closed = planner.advance_lifecycle(opened, "closed", "2026-08-11T02:30:00+08:00")
    # Advancing the lifecycle never executes the Evolution.
    assert closed.execution_status == "not_executed"
    v = verify_evolution_record(closed, proposal, review, gate)
    assert v.no_execution is True


def test_record_has_no_executor_entrypoint():
    planner, proposal, impact, review, gate, record = _build_chain()
    # The record is a record, not an executor.
    for forbidden in ("apply", "execute", "mutate", "run"):
        assert not hasattr(record, forbidden), f"record must not expose {forbidden}()"
        assert not hasattr(planner, forbidden), f"planner must not expose {forbidden}()"


def test_rejected_gate_still_completes_lifecycle():
    planner, proposal, impact, review, gate, record = _build_chain(decision="rejected")
    assert record.decision == "rejected"
    assert record.execution_status == "not_executed"
    # A rejected decision still records and closes its lifecycle (not pursued).
    opened = planner.advance_lifecycle(record, "open", "2026-08-11T02:00:00+08:00")
    closed = planner.advance_lifecycle(opened, "closed", "2026-08-11T02:30:00+08:00")
    v = verify_evolution_record(closed, proposal, review, gate)
    assert v.valid is True
    assert v.lifecycle_complete is True
    assert v.no_execution is True


def test_record_digest_tampering_detected():
    planner, proposal, impact, review, gate, record = _build_chain()
    tampered = replace(record, record_digest="0" * 64)
    v = verify_evolution_record(tampered, proposal, review, gate)
    assert v.digest_intact is False
    assert v.valid is False


def test_gate_digest_binding_tampering_detected():
    planner, proposal, impact, review, gate, record = _build_chain()
    # The record carries the gate digest; tampering it breaks the binding.
    tampered = replace(record, gate_digest="f" * 64)
    v = verify_evolution_record(tampered, proposal, review, gate)
    assert v.gate_binding_intact is False
    assert v.valid is False


def test_no_runtime_mutation_proven_structurally():
    planner, proposal, impact, review, gate, record = _build_chain()
    v = verify_evolution_record(record, proposal, review, gate)
    # The record never mutates runtime / Lifecycle / Foundation / CapabilityContract.
    assert v.no_runtime_mutation is True
    # The record's decision is aligned with the gate decision.
    assert record.decision == gate.decision
