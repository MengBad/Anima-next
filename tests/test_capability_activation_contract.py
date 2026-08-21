from __future__ import annotations

import pytest

from anima.capability_activation_contract import (
    CapabilityActivationError,
    CapabilityActivationReview,
    OfflineCapabilityActivationPlanner,
    PlanStatus,
    ReviewDecision,
    SyntheticCapabilityProposal,
    verify_activation_plan,
)


NOW = "2026-08-11T00:00:00Z"


def _proposal():
    return SyntheticCapabilityProposal(
        capability_id="cap-synthetic-1",
        version="0.1.0",
        provider_ref="synthetic://provider",
        contract_digest="a" * 64,
        risk_level="low",
        scope="synthetic-only",
        proposed_at=NOW,
    )


def _review(decision=ReviewDecision.APPROVE, proposal_id="cap-synthetic-1"):
    return CapabilityActivationReview(
        review_id="review-1",
        proposal_id=proposal_id,
        decision=decision,
        reviewer="operator-synthetic",
        evidence_refs=("review-evidence-1",),
        reviewed_at=NOW,
    )


def test_approved_synthetic_proposal_creates_unactivated_plan():
    proposal = _proposal()
    review = _review()
    plan = OfflineCapabilityActivationPlanner().plan(
        plan_id="plan-1",
        proposal_id=proposal.capability_id,
        proposal=proposal,
        review=review,
        created_at=NOW,
    )

    result = verify_activation_plan(plan, proposal=proposal, review=review)

    assert plan.status == PlanStatus.NOT_ACTIVATED
    assert result.valid is True
    assert result.proposal_binding is True
    assert result.review_binding is True
    assert result.no_runtime_activation is True


@pytest.mark.parametrize("decision", [ReviewDecision.REJECT, ReviewDecision.REQUEST_REVIEW])
def test_non_approval_is_denied_without_activation(decision):
    proposal = _proposal()
    review = _review(decision)
    plan = OfflineCapabilityActivationPlanner().plan(
        plan_id="plan-1",
        proposal_id=proposal.capability_id,
        proposal=proposal,
        review=review,
        created_at=NOW,
    )

    assert plan.status == PlanStatus.DENIED
    assert verify_activation_plan(plan, proposal=proposal, review=review).no_runtime_activation


def test_missing_or_mismatched_inputs_fail_closed():
    proposal = _proposal()
    planner = OfflineCapabilityActivationPlanner()
    missing = planner.plan(
        plan_id="plan-1",
        proposal_id=proposal.capability_id,
        proposal=None,
        review=None,
        created_at=NOW,
    )
    assert missing.status == PlanStatus.DENIED

    mismatch = planner.plan(
        plan_id="plan-2",
        proposal_id="other-capability",
        proposal=proposal,
        review=_review(proposal_id="other-capability"),
        created_at=NOW,
    )
    assert mismatch.status == PlanStatus.DENIED


def test_tampered_plan_digest_is_rejected():
    proposal = _proposal()
    review = _review()
    plan = OfflineCapabilityActivationPlanner().plan(
        plan_id="plan-1",
        proposal_id=proposal.capability_id,
        proposal=proposal,
        review=review,
        created_at=NOW,
    )
    tampered = plan.__class__(
        plan_id=plan.plan_id,
        proposal_id=plan.proposal_id,
        review_id=plan.review_id,
        decision=plan.decision,
        status=plan.status,
        created_at=plan.created_at,
        plan_digest="0" * 64,
    )

    result = verify_activation_plan(tampered, proposal=proposal, review=review)
    assert result.valid is False
    assert "activation plan digest mismatch" in result.errors


def test_real_provider_and_active_status_are_rejected():
    with pytest.raises(CapabilityActivationError, match="synthetic"):
        SyntheticCapabilityProposal(
            capability_id="cap-1",
            version="0.1.0",
            provider_ref="real://provider",
            contract_digest="a" * 64,
            risk_level="low",
            scope="synthetic-only",
            proposed_at=NOW,
        )

    assert not hasattr(OfflineCapabilityActivationPlanner(), "activate")
    assert not hasattr(OfflineCapabilityActivationPlanner(), "execute")
