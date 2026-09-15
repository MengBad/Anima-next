"""Offline Activation Record and Proof prototype.

This module records proof of an unactivated synthetic plan only.  It never
executes a provider or activates a runtime capability.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from anima.capability.activation_contract import (
    CapabilityActivationPlan,
    CapabilityActivationReview,
    PlanStatus,
    ReviewDecision,
    SyntheticCapabilityProposal,
)


class ActivationProofError(ValueError):
    """Raised when an activation record/proof contract is invalid."""


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ActivationProofError(f"{field_name} must be a non-empty string")
    return value


def _timestamp(value: Any, field_name: str) -> datetime:
    value = _text(value, field_name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ActivationProofError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ActivationProofError(f"{field_name} must include timezone information")
    return parsed


def _digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ActivationRecord:
    record_id: str
    plan_id: str
    proposal_id: str
    review_id: str
    plan_digest: str
    scope: str
    status: str
    recorded_at: str
    record_digest: str

    def __post_init__(self) -> None:
        for name in (
            "record_id",
            "plan_id",
            "proposal_id",
            "review_id",
            "plan_digest",
            "scope",
            "status",
            "record_digest",
        ):
            _text(getattr(self, name), name)
        _timestamp(self.recorded_at, "recorded_at")
        if self.status != PlanStatus.NOT_ACTIVATED:
            raise ActivationProofError("activation record must remain not_activated")
        for value in (self.plan_digest, self.record_digest):
            if len(value) != 64 or any(
                character not in "0123456789abcdef" for character in value
            ):
                raise ActivationProofError("activation digests must be SHA-256 hex digests")


@dataclass(frozen=True, slots=True)
class ActivationProof:
    proof_id: str
    activation_record_id: str
    plan_digest: str
    result: str
    evidence_refs: tuple[str, ...]
    created_at: str
    proof_digest: str

    def __post_init__(self) -> None:
        for name in (
            "proof_id",
            "activation_record_id",
            "plan_digest",
            "result",
            "proof_digest",
        ):
            _text(getattr(self, name), name)
        _timestamp(self.created_at, "created_at")
        if not self.evidence_refs:
            raise ActivationProofError("proof requires evidence_refs")
        for reference in self.evidence_refs:
            _text(reference, "proof evidence reference")
        for value in (self.plan_digest, self.proof_digest):
            if len(value) != 64 or any(
                character not in "0123456789abcdef" for character in value
            ):
                raise ActivationProofError("proof digests must be SHA-256 hex digests")


@dataclass(frozen=True, slots=True)
class ActivationProofVerification:
    valid: bool
    plan_binding: bool
    record_binding: bool
    scope_valid: bool
    no_runtime_activation: bool
    errors: tuple[str, ...] = ()


class OfflineActivationProofBuilder:
    """Create an unactivated record and proof from synthetic inputs."""

    def record(
        self,
        *,
        record_id: str,
        plan: CapabilityActivationPlan,
        proposal: SyntheticCapabilityProposal,
        review: CapabilityActivationReview,
        requested_scope: str,
        recorded_at: str,
    ) -> ActivationRecord:
        _timestamp(recorded_at, "recorded_at")
        if plan.status != PlanStatus.NOT_ACTIVATED:
            raise ActivationProofError("denied plan cannot generate activation record")
        if plan.proposal_id != proposal.capability_id:
            raise ActivationProofError("activation plan/proposal mismatch")
        if plan.review_id != review.review_id or review.proposal_id != plan.proposal_id:
            raise ActivationProofError("activation plan/review mismatch")
        if review.decision != ReviewDecision.APPROVE:
            raise ActivationProofError("only approved plan may generate activation record")
        if requested_scope != proposal.scope or requested_scope != "synthetic-only":
            raise ActivationProofError("activation scope exceeds synthetic-only boundary")
        payload = {
            "record_id": record_id,
            "plan_id": plan.plan_id,
            "proposal_id": proposal.capability_id,
            "review_id": review.review_id,
            "plan_digest": plan.plan_digest,
            "scope": requested_scope,
            "status": PlanStatus.NOT_ACTIVATED,
            "recorded_at": recorded_at,
        }
        return ActivationRecord(
            record_id=record_id,
            plan_id=plan.plan_id,
            proposal_id=proposal.capability_id,
            review_id=review.review_id,
            plan_digest=plan.plan_digest,
            scope=requested_scope,
            status=PlanStatus.NOT_ACTIVATED,
            recorded_at=recorded_at,
            record_digest=_digest(payload),
        )

    def proof(
        self,
        *,
        proof_id: str,
        record: ActivationRecord,
        result: str,
        evidence_refs: tuple[str, ...],
        created_at: str,
    ) -> ActivationProof:
        _timestamp(created_at, "created_at")
        if record.status != PlanStatus.NOT_ACTIVATED:
            raise ActivationProofError("proof cannot be created for an activated record")
        if not evidence_refs:
            raise ActivationProofError("proof requires evidence_refs")
        payload = {
            "proof_id": proof_id,
            "activation_record_id": record.record_id,
            "plan_digest": record.plan_digest,
            "result": result,
            "evidence_refs": list(evidence_refs),
            "created_at": created_at,
        }
        return ActivationProof(
            proof_id=proof_id,
            activation_record_id=record.record_id,
            plan_digest=record.plan_digest,
            result=result,
            evidence_refs=evidence_refs,
            created_at=created_at,
            proof_digest=_digest(payload),
        )


def verify_activation_proof(
    *,
    plan: CapabilityActivationPlan,
    record: ActivationRecord,
    proof: ActivationProof,
    proposal: SyntheticCapabilityProposal,
) -> ActivationProofVerification:
    errors: list[str] = []
    plan_binding = (
        record.plan_id == plan.plan_id
        and record.plan_digest == plan.plan_digest
        and record.proposal_id == proposal.capability_id
    )
    record_binding = proof.activation_record_id == record.record_id
    scope_valid = record.scope == proposal.scope == "synthetic-only"
    no_runtime_activation = (
        plan.status == PlanStatus.NOT_ACTIVATED
        and record.status == PlanStatus.NOT_ACTIVATED
    )
    if not plan_binding:
        errors.append("activation record/plan binding mismatch")
    if not record_binding:
        errors.append("proof/activation record binding mismatch")
    if proof.plan_digest != record.plan_digest:
        errors.append("proof plan digest mismatch")
    if not scope_valid:
        errors.append("activation scope exceeds synthetic-only boundary")
    if not no_runtime_activation:
        errors.append("runtime activation status is forbidden")
    payload = {
        "record_id": record.record_id,
        "plan_id": record.plan_id,
        "proposal_id": record.proposal_id,
        "review_id": record.review_id,
        "plan_digest": record.plan_digest,
        "scope": record.scope,
        "status": record.status,
        "recorded_at": record.recorded_at,
    }
    if _digest(payload) != record.record_digest:
        errors.append("activation record digest mismatch")
    proof_payload = {
        "proof_id": proof.proof_id,
        "activation_record_id": proof.activation_record_id,
        "plan_digest": proof.plan_digest,
        "result": proof.result,
        "evidence_refs": list(proof.evidence_refs),
        "created_at": proof.created_at,
    }
    if _digest(proof_payload) != proof.proof_digest:
        errors.append("activation proof digest mismatch")
    return ActivationProofVerification(
        valid=not errors,
        plan_binding=plan_binding,
        record_binding=record_binding,
        scope_valid=scope_valid,
        no_runtime_activation=no_runtime_activation,
        errors=tuple(errors),
    )


__all__ = [
    "ActivationProof",
    "ActivationProofError",
    "ActivationProofVerification",
    "ActivationRecord",
    "OfflineActivationProofBuilder",
    "verify_activation_proof",
]
