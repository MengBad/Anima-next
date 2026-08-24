"""RED-phase tests for P4.1 Evolution Proposal Contract Prototype.

These tests import ``anima.capability_evolution_contract`` which does not yet
exist; running them now must fail with ModuleNotFoundError.  After the module is
implemented they must turn green.

The contract establishes *boundary* artifacts only (proposal / impact / review)
for the Foundation Evolution protocol (F1-F4).  It must never execute, activate
a runtime, or mutate the Foundation.  All constraints are offline / synthetic-only
/ read-only.
"""

import re
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from anima.capability_evolution_contract import (
    EvolutionProposalError,
    OfflineEvolutionPlanner,
    verify_evolution_chain,
)

NOW = datetime.now(timezone.utc).isoformat()
_SYNTHETIC = "synthetic-only"
_VALID_64 = "a" * 64


def _valid_proposal(**overrides) -> "dict":
    data = dict(
        proposal_id="ev-1",
        validation_rfc_id="RFC-0004",
        observed_contradiction="Runtime Lifecycle claims a capability may self-promote, "
        "but Observation shows no upstream authority permits it.",
        inconsistency_eliminated="Self-promotion path removed; only Validation RFC may "
        "propose foundation change.",
        basis="observed_contradiction",
        change_class="contraction",
        forbids_expansion="true",
        scope=_SYNTHETIC,
        status="proposed",
        proposed_at=NOW,
        proposal_digest=_VALID_64,
    )
    data.update(overrides)
    return data


def test_valid_evolution_chain_passes_verification():
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
    verification = verify_evolution_chain(proposal=proposal, impact=impact, review=review)
    assert verification.valid, verification.errors
    assert verification.f1_valid
    assert verification.f2_valid
    assert verification.f3_valid
    assert verification.f4_valid
    assert verification.impact_read_only
    assert verification.no_runtime_mutation


def test_f1_proposal_requires_validation_rfc_id():
    with pytest.raises(EvolutionProposalError):
        from anima.capability_evolution_contract import EvolutionProposal

        EvolutionProposal(**_valid_proposal(validation_rfc_id=""))


def test_f2_proposal_must_be_observed_contradiction_not_improvement():
    with pytest.raises(EvolutionProposalError):
        from anima.capability_evolution_contract import EvolutionProposal

        EvolutionProposal(**_valid_proposal(basis="improvement"))


def test_f3_proposal_requires_inconsistency_eliminated():
    with pytest.raises(EvolutionProposalError):
        from anima.capability_evolution_contract import EvolutionProposal

        EvolutionProposal(**_valid_proposal(inconsistency_eliminated=""))


def test_f4_proposal_must_not_expand_foundation():
    with pytest.raises(EvolutionProposalError):
        from anima.capability_evolution_contract import EvolutionProposal

        EvolutionProposal(**_valid_proposal(change_class="expansion", forbids_expansion="false"))


def test_digest_binding_tampering_rejected():
    planner = OfflineEvolutionPlanner()
    proposal = planner.propose(
        proposal_id="ev-1",
        validation_rfc_id="RFC-0004",
        observed_contradiction="Observed contradiction text.",
        inconsistency_eliminated="Inconsistency eliminated text.",
        change_class="contraction",
        forbids_expansion="true",
        proposed_at=NOW,
    )
    impact = planner.assess_impact(
        impact_id="im-1",
        proposal=proposal,
        affected_foundation_refs=("Foundation:Life/O1",),
        assessed_at=NOW,
    )
    review = planner.review(
        review_id="rv-1",
        proposal=proposal,
        impact=impact,
        decision="approve",
        reviewed_at=NOW,
    )
    # Tamper the impact's binding to the proposal digest.
    tampered_impact = replace(impact, proposal_digest="f" * 64)
    verification = verify_evolution_chain(
        proposal=proposal, impact=tampered_impact, review=review
    )
    assert not verification.valid
    assert not verification.digest_intact
    assert any("digest" in e.lower() for e in verification.errors)


def test_scope_must_be_synthetic_only():
    planner = OfflineEvolutionPlanner()
    with pytest.raises(EvolutionProposalError):
        planner.propose(
            proposal_id="ev-1",
            validation_rfc_id="RFC-0004",
            observed_contradiction="Observed contradiction text.",
            inconsistency_eliminated="Inconsistency eliminated text.",
            change_class="contraction",
            forbids_expansion="true",
            proposed_at=NOW,
            requested_scope="production",
        )


def test_impact_must_be_read_only_analysis():
    planner = OfflineEvolutionPlanner()
    proposal = planner.propose(
        proposal_id="ev-1",
        validation_rfc_id="RFC-0004",
        observed_contradiction="Observed contradiction text.",
        inconsistency_eliminated="Inconsistency eliminated text.",
        change_class="contraction",
        forbids_expansion="true",
        proposed_at=NOW,
    )
    with pytest.raises(EvolutionProposalError):
        planner.assess_impact(
            impact_id="im-1",
            proposal=proposal,
            affected_foundation_refs=("Foundation:Life/O1",),
            mutation_class="modify",
            assessed_at=NOW,
        )


def test_review_decision_is_gate_not_executor():
    planner = OfflineEvolutionPlanner()
    proposal = planner.propose(
        proposal_id="ev-1",
        validation_rfc_id="RFC-0004",
        observed_contradiction="Observed contradiction text.",
        inconsistency_eliminated="Inconsistency eliminated text.",
        change_class="contraction",
        forbids_expansion="true",
        proposed_at=NOW,
    )
    impact = planner.assess_impact(
        impact_id="im-1",
        proposal=proposal,
        affected_foundation_refs=("Foundation:Life/O1",),
        assessed_at=NOW,
    )
    review = planner.review(
        review_id="rv-1",
        proposal=proposal,
        impact=impact,
        decision="approve",
        reviewed_at=NOW,
    )
    # An approved review is a gate record; it must not expose any executor.
    assert not hasattr(review, "apply")
    assert not hasattr(review, "execute")
    assert not hasattr(review, "mutate")
    assert not hasattr(planner, "apply")
    assert not hasattr(planner, "execute")
    verification = verify_evolution_chain(proposal=proposal, impact=impact, review=review)
    assert verification.no_runtime_mutation is True
    assert verification.impact_read_only is True


def test_static_boundary_scan_no_executor_or_runtime_calls():
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
