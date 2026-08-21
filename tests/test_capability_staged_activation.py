from __future__ import annotations

import re
from pathlib import Path

import pytest

from anima.capability_activation_contract import (
    CapabilityActivationError,
    CapabilityActivationReview,
    OfflineCapabilityActivationPlanner,
    ReviewDecision,
    SyntheticCapabilityProposal,
)
from anima.capability_activation_proof import OfflineActivationProofBuilder
from anima.capability_staged_activation import (
    OfflineStagedActivationPlanner,
    StageStatus,
    StagedActivationError,
    verify_staged_activation,
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
        result="not activated",
        evidence_refs=("proof-evidence-1",),
        created_at=NOW,
    )
    staged = OfflineStagedActivationPlanner().stage(
        stage_id="stage-1",
        plan=plan,
        record=record,
        proof=proof,
        proposal=proposal,
        gate_refs=("gate-proof-valid", "gate-scope-synthetic"),
        staged_at=NOW,
    )
    return proposal, plan, record, proof, staged


def test_valid_proof_creates_staged_not_activated_record():
    proposal, plan, record, proof, staged = _chain()

    result = verify_staged_activation(
        staged=staged,
        plan=plan,
        record=record,
        proof=proof,
        proposal=proposal,
    )

    assert staged.stage == StageStatus.STAGED
    assert staged.runtime_activation == "not_activated"
    assert result.valid is True
    assert result.proof_valid is True
    assert result.gate_valid is True
    assert result.no_runtime_activation is True


def test_invalid_proof_cannot_be_staged():
    proposal, plan, record, proof, _ = _chain()
    tampered_proof = proof.__class__(
        proof_id=proof.proof_id,
        activation_record_id=proof.activation_record_id,
        plan_digest=proof.plan_digest,
        result=proof.result,
        evidence_refs=proof.evidence_refs,
        created_at=proof.created_at,
        proof_digest="0" * 64,
    )

    with pytest.raises(StagedActivationError, match="proof verification"):
        OfflineStagedActivationPlanner().stage(
            stage_id="stage-invalid",
            plan=plan,
            record=record,
            proof=tampered_proof,
            proposal=proposal,
            gate_refs=("gate",),
            staged_at=NOW,
        )


def test_scope_overreach_and_missing_gate_are_rejected():
    proposal, plan, record, proof, _ = _chain()
    planner = OfflineStagedActivationPlanner()
    with pytest.raises(StagedActivationError, match="gate_refs"):
        planner.stage(
            stage_id="stage-no-gate",
            plan=plan,
            record=record,
            proof=proof,
            proposal=proposal,
            gate_refs=(),
            staged_at=NOW,
        )

    overreach = proposal.__class__(
        capability_id=proposal.capability_id,
        version=proposal.version,
        provider_ref=proposal.provider_ref,
        contract_digest=proposal.contract_digest,
        risk_level=proposal.risk_level,
        scope="production-capability",
        proposed_at=proposal.proposed_at,
    )
    with pytest.raises(StagedActivationError, match="proof verification"):
        planner.stage(
            stage_id="stage-overreach",
            plan=plan,
            record=record,
            proof=proof,
            proposal=overreach,
            gate_refs=("gate",),
            staged_at=NOW,
        )


def test_stage_digest_tampering_is_rejected():
    proposal, plan, record, proof, staged = _chain()
    tampered = staged.__class__(
        stage_id=staged.stage_id,
        plan_id=staged.plan_id,
        activation_record_id=staged.activation_record_id,
        proof_id=staged.proof_id,
        capability_id=staged.capability_id,
        scope=staged.scope,
        stage=staged.stage,
        runtime_activation=staged.runtime_activation,
        gate_refs=staged.gate_refs,
        staged_at=staged.staged_at,
        stage_digest="0" * 64,
    )

    result = verify_staged_activation(
        staged=tampered,
        plan=plan,
        record=record,
        proof=proof,
        proposal=proposal,
    )
    assert result.valid is False
    assert "staged activation digest mismatch" in result.errors


def test_staged_planner_has_no_activation_executor():
    planner = OfflineStagedActivationPlanner()
    assert not hasattr(planner, "activate")
    assert not hasattr(planner, "execute")


def test_fake_provider_is_rejected_only_synthetic_allowed():
    # Deliverable 3: a non-synthetic provider reference must be rejected.
    with pytest.raises(CapabilityActivationError, match="synthetic"):
        SyntheticCapabilityProposal(
            capability_id="cap-fake-1",
            version="0.1.0",
            provider_ref="https://real-api.example.com/exec",
            contract_digest="a" * 64,
            risk_level="low",
            scope="synthetic-only",
            proposed_at=NOW,
        )


# Static boundary scan (Deliverable 4): the staged-activation capability chain
# must contain no executor, no runtime activation primitives, and no outbound
# system/runtime calls. This is a fails-closed guard against accidental
# introduction of an executor into the offline-only, synthetic-only prototype.
_FORBIDDEN_SOURCE_PATTERNS = [
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
    r"active\s*=\s*True",
    r"enabled\s*=\s*True",
]

_STAGED_ACTIVATION_CHAIN_MODULES = [
    "anima/capability_activation_contract.py",
    "anima/capability_activation_proof.py",
    "anima/capability_staged_activation.py",
]


def test_staged_activation_chain_source_has_no_executor_or_runtime_calls():
    repo_root = Path(__file__).resolve().parent.parent
    compiled = [re.compile(pattern) for pattern in _FORBIDDEN_SOURCE_PATTERNS]
    for relative in _STAGED_ACTIVATION_CHAIN_MODULES:
        source = (repo_root / relative).read_text(encoding="utf-8")
        for pattern in compiled:
            assert not pattern.search(
                source
            ), f"{relative} contains forbidden pattern {pattern.pattern!r}"
