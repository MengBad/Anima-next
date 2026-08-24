"""RED-phase tests for P4.2 Evolution Gate / Recording Boundary Prototype.

These tests import ``anima.capability_evolution_contract`` symbols that do not
yet exist for the gate (``EvolutionGateRecord``, ``verify_evolution_gate``);
running them now must fail.  After the gate is implemented they must turn green.

The gate is a *recording boundary* only.  It records a decision (approved /
rejected / needs_revision) bound to a proposal, with reviewer provenance and a
full rationale on rejection.  It never executes evolution: approval is a record,
not an apply.  All constraints are offline / synthetic-only / read-only.
"""

import re
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from anima.capability_evolution_contract import (
    EvolutionProposalError,
    OfflineEvolutionPlanner,
    verify_evolution_gate,
)

NOW = datetime.now(timezone.utc).isoformat()
_VALID_64 = "a" * 64


def _build_proposal_and_review():
    planner = OfflineEvolutionPlanner()
    proposal = planner.propose(
        proposal_id="ev-1",
        validation_rfc_id="RFC-0004",
        observed_contradiction="Runtime Lifecycle claims a capability may self-promote.",
        inconsistency_eliminated="Self-promotion path removed.",
        change_class="contraction",
        forbids_expansion="true",
        proposed_at=NOW,
    )
    impact = planner.assess_impact(
        impact_id="im-1",
        proposal=proposal,
        affected_foundation_refs=("Foundation:Life/O1:Lifecycle",),
        assessed_at=NOW,
    )
    review = planner.review(
        review_id="rv-1",
        proposal=proposal,
        impact=impact,
        decision="approve",
        reviewed_at=NOW,
    )
    return planner, proposal, review


def test_approved_gate_passes_verification():
    planner, proposal, review = _build_proposal_and_review()
    gate = planner.record_gate(
        gate_id="gt-1",
        proposal=proposal,
        review=review,
        decision="approved",
        reviewer="maintainer@foundation",
        evidence_refs=("rv-1",),
        created_at=NOW,
        rationale="Approved: contraction only, no foundation expansion.",
    )
    verification = verify_evolution_gate(proposal=proposal, gate=gate)
    assert verification.valid, verification.errors
    assert verification.proposal_binding_intact
    assert verification.reject_has_rationale
    assert verification.reviewer_provenance
    assert verification.gate_not_executor
    assert verification.approval_not_apply
    assert verification.no_runtime_mutation


def test_rejected_gate_with_rationale_passes_but_marks_reject_has_rationale():
    planner, proposal, review = _build_proposal_and_review()
    gate = planner.record_gate(
        gate_id="gt-2",
        proposal=proposal,
        review=review,
        decision="rejected",
        reviewer="maintainer@foundation",
        evidence_refs=("rv-1",),
        created_at=NOW,
        rationale="Rejected: the contradiction is not observed, only hypothetical.",
    )
    verification = verify_evolution_gate(proposal=proposal, gate=gate)
    assert verification.valid, verification.errors
    assert verification.reject_has_rationale is True


def test_reject_requires_full_rationale():
    planner, proposal, review = _build_proposal_and_review()
    with pytest.raises(EvolutionProposalError):
        planner.record_gate(
            gate_id="gt-bad",
            proposal=proposal,
            review=review,
            decision="rejected",
            reviewer="maintainer@foundation",
            evidence_refs=(),
            created_at=NOW,
            rationale="",  # empty -> must be rejected
        )


def test_needs_revision_also_requires_rationale():
    planner, proposal, review = _build_proposal_and_review()
    with pytest.raises(EvolutionProposalError):
        planner.record_gate(
            gate_id="gt-bad2",
            proposal=proposal,
            review=review,
            decision="needs_revision",
            reviewer="maintainer@foundation",
            evidence_refs=(),
            created_at=NOW,
            rationale="",
        )


def test_reviewer_provenance_is_required_and_preserved():
    planner, proposal, review = _build_proposal_and_review()
    with pytest.raises(EvolutionProposalError):
        planner.record_gate(
            gate_id="gt-bad3",
            proposal=proposal,
            review=review,
            decision="approved",
            reviewer="",  # missing provenance
            evidence_refs=(),
            created_at=NOW,
            rationale="ok",
        )
    # When provided, it is preserved verbatim on the record.
    gate = planner.record_gate(
        gate_id="gt-3",
        proposal=proposal,
        review=review,
        decision="approved",
        reviewer="specific.maintainer@foundation",
        evidence_refs=("rv-1",),
        created_at=NOW,
        rationale="Approved with provenance.",
    )
    assert gate.reviewer == "specific.maintainer@foundation"
    verification = verify_evolution_gate(proposal=proposal, gate=gate)
    assert verification.reviewer_provenance is True


def test_reject_has_rationale_flag_false_when_rationale_missing_on_reject():
    # Direct construction with a rejected decision but empty rationale must
    # be rejected by the record's own invariants (defense in depth).
    from anima.capability_evolution_contract import EvolutionGateRecord

    with pytest.raises(EvolutionProposalError):
        EvolutionGateRecord(
            gate_id="gt-x",
            proposal_id="ev-1",
            proposal_digest=_VALID_64,
            review_id="rv-1",
            decision="rejected",
            reviewer="m@f",
            evidence_refs=(),
            created_at=NOW,
            rationale="",
            digest=_VALID_64,
        )


def test_invalid_decision_state_rejected():
    planner, proposal, review = _build_proposal_and_review()
    with pytest.raises(EvolutionProposalError):
        planner.record_gate(
            gate_id="gt-bad4",
            proposal=proposal,
            review=review,
            decision="applied",  # not a valid gate state
            reviewer="m@f",
            evidence_refs=(),
            created_at=NOW,
            rationale="n/a",
        )


def test_proposal_gate_digest_binding_tampering_rejected():
    planner, proposal, review = _build_proposal_and_review()
    gate = planner.record_gate(
        gate_id="gt-4",
        proposal=proposal,
        review=review,
        decision="approved",
        reviewer="m@f",
        evidence_refs=("rv-1",),
        created_at=NOW,
        rationale="Approved.",
    )
    tampered = replace(gate, proposal_digest="f" * 64)
    verification = verify_evolution_gate(proposal=proposal, gate=tampered)
    assert not verification.valid
    assert not verification.proposal_binding_intact
    assert any("digest" in e.lower() or "binding" in e.lower() for e in verification.errors)


def test_gate_can_not_execute_evolution():
    planner, proposal, review = _build_proposal_and_review()
    gate = planner.record_gate(
        gate_id="gt-5",
        proposal=proposal,
        review=review,
        decision="approved",
        reviewer="m@f",
        evidence_refs=("rv-1",),
        created_at=NOW,
        rationale="Approved.",
    )
    # A gate record exposes no executor entry points.
    assert not hasattr(gate, "apply")
    assert not hasattr(gate, "execute")
    assert not hasattr(gate, "mutate")
    # The planner exposes no apply/execute either.
    assert not hasattr(planner, "apply")
    assert not hasattr(planner, "execute")
    assert not hasattr(planner, "mutate")
    verification = verify_evolution_gate(proposal=proposal, gate=gate)
    assert verification.gate_not_executor is True
    assert verification.approval_not_apply is True
    assert verification.no_runtime_mutation is True


def test_approval_is_not_apply():
    planner, proposal, review = _build_proposal_and_review()
    gate = planner.record_gate(
        gate_id="gt-6",
        proposal=proposal,
        review=review,
        decision="approved",
        reviewer="m@f",
        evidence_refs=("rv-1",),
        created_at=NOW,
        rationale="Approved but NOT applied - recording boundary only.",
    )
    # Approving produces a record; it does not expose an apply() that would
    # change runtime state.
    assert not hasattr(gate, "apply")
    verification = verify_evolution_gate(proposal=proposal, gate=gate)
    assert verification.valid
    assert verification.approval_not_apply is True


def test_static_boundary_scan_gate_no_executor_or_runtime_calls():
    repo_root = Path(__file__).resolve().parent.parent
    ev = repo_root / "anima/capability/evolution_contract.py"
    if not ev.exists():
        ev = repo_root / "anima/capability_evolution_contract.py"
    source = ev.read_text(encoding="utf-8")
    patterns = [
        r"subprocess",
        r"\bssh\b",
        r"systemctl",
        r"docker",
        r"\brequests\b",
        r"httpx",
        r"aiohttp",
        r"urllib",
        r"http\.client",
        r"importlib",
        r"__import__",
        r"\.activate\(",
        r"\.enable\(",
        r"\.start\(",
        r"\.execute\(",
        r"\.apply\(",
        r"\.mutate\(",
        r"active\s*=\s*True",
        r"enabled\s*=\s*True",
    ]
    compiled = [re.compile(p) for p in patterns]
    hits = [p.pattern for p in compiled if p.search(source)]
    assert not hits, f"forbidden patterns found: {hits}"
