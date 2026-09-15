from __future__ import annotations

import pytest

from anima.capability.activation_contract import (
    CapabilityActivationReview,
    OfflineCapabilityActivationPlanner,
    ReviewDecision,
    SyntheticCapabilityProposal,
)
from anima.capability.activation_proof import (
    ActivationProofError,
    OfflineActivationProofBuilder,
    verify_activation_proof,
)


NOW = "2026-08-11T00:00:00Z"


def _chain():
    proposal = SyntheticCapabilityProposal(
        capability_id="cap-synthetic-1",
        version="0.1.0",
        provider_ref="synthetic://provider",
        contract_digest="a" * 64,
        risk_level="low",
        scope="synthetic-only",
        proposed_at=NOW,
    )
    review = CapabilityActivationReview(
        review_id="review-1",
        proposal_id=proposal.capability_id,
        decision=ReviewDecision.APPROVE,
        reviewer="operator-synthetic",
        evidence_refs=("review-evidence-1",),
        reviewed_at=NOW,
    )
    plan = OfflineCapabilityActivationPlanner().plan(
        plan_id="plan-1",
        proposal_id=proposal.capability_id,
        proposal=proposal,
        review=review,
        created_at=NOW,
    )
    builder = OfflineActivationProofBuilder()
    record = builder.record(
        record_id="record-1",
        plan=plan,
        proposal=proposal,
        review=review,
        requested_scope="synthetic-only",
        recorded_at=NOW,
    )
    proof = builder.proof(
        proof_id="proof-1",
        record=record,
        result="activation record verified; capability not activated",
        evidence_refs=("proof-evidence-1",),
        created_at=NOW,
    )
    return proposal, plan, record, proof


def test_activation_record_and_proof_bind_to_plan():
    proposal, plan, record, proof = _chain()

    result = verify_activation_proof(
        plan=plan, record=record, proof=proof, proposal=proposal
    )

    assert result.valid is True
    assert result.plan_binding is True
    assert result.record_binding is True
    assert result.scope_valid is True
    assert result.no_runtime_activation is True


def test_denied_plan_cannot_generate_activation_record():
    proposal, plan, _, _ = _chain()
    denied_plan = plan.__class__(
        plan_id=plan.plan_id,
        proposal_id=plan.proposal_id,
        review_id=plan.review_id,
        decision=ReviewDecision.REJECT,
        status="denied",
        created_at=plan.created_at,
        plan_digest=plan.plan_digest,
    )
    review = CapabilityActivationReview(
        review_id=plan.review_id,
        proposal_id=proposal.capability_id,
        decision=ReviewDecision.REJECT,
        reviewer="operator-synthetic",
        evidence_refs=("review-evidence-1",),
        reviewed_at=NOW,
    )

    with pytest.raises(ActivationProofError, match="denied plan"):
        OfflineActivationProofBuilder().record(
            record_id="record-denied",
            plan=denied_plan,
            proposal=proposal,
            review=review,
            requested_scope="synthetic-only",
            recorded_at=NOW,
        )


def test_scope_overreach_is_rejected():
    proposal, plan, _, _ = _chain()
    review = CapabilityActivationReview(
        review_id="review-1",
        proposal_id=proposal.capability_id,
        decision=ReviewDecision.APPROVE,
        reviewer="operator-synthetic",
        evidence_refs=("review-evidence-1",),
        reviewed_at=NOW,
    )

    with pytest.raises(ActivationProofError, match="scope"):
        OfflineActivationProofBuilder().record(
            record_id="record-overreach",
            plan=plan,
            proposal=proposal,
            review=review,
            requested_scope="production-capability",
            recorded_at=NOW,
        )


def test_record_digest_tampering_is_rejected():
    proposal, plan, record, proof = _chain()
    tampered = record.__class__(
        record_id=record.record_id,
        plan_id=record.plan_id,
        proposal_id=record.proposal_id,
        review_id=record.review_id,
        plan_digest=record.plan_digest,
        scope=record.scope,
        status=record.status,
        recorded_at=record.recorded_at,
        record_digest="0" * 64,
    )

    result = verify_activation_proof(
        plan=plan, record=tampered, proof=proof, proposal=proposal
    )
    assert result.valid is False
    assert "activation record digest mismatch" in result.errors


def test_proof_binding_and_digest_tampering_are_rejected():
    proposal, plan, record, proof = _chain()
    tampered = proof.__class__(
        proof_id=proof.proof_id,
        activation_record_id="other-record",
        plan_digest=proof.plan_digest,
        result=proof.result,
        evidence_refs=proof.evidence_refs,
        created_at=proof.created_at,
        proof_digest=proof.proof_digest,
    )

    result = verify_activation_proof(
        plan=plan, record=record, proof=tampered, proposal=proposal
    )
    assert result.valid is False
    assert "proof/activation record binding mismatch" in result.errors
    assert "activation proof digest mismatch" in result.errors


def test_proof_builder_has_no_activation_executor():
    builder = OfflineActivationProofBuilder()
    assert not hasattr(builder, "activate")
    assert not hasattr(builder, "execute")
