"""Offline, synthetic-only Capability Activation Contract prototype.

This module validates an activation proposal and explicit review without
activating a runtime capability or connecting to a provider.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping


class CapabilityActivationError(ValueError):
    """Raised when the synthetic activation contract is invalid."""


class ReviewDecision:
    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_REVIEW = "request_review"


class PlanStatus:
    NOT_ACTIVATED = "not_activated"
    DENIED = "denied"


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CapabilityActivationError(f"{field_name} must be a non-empty string")
    return value


def _timestamp(value: Any, field_name: str) -> datetime:
    value = _text(value, field_name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CapabilityActivationError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise CapabilityActivationError(f"{field_name} must include timezone information")
    return parsed


def _digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SyntheticCapabilityProposal:
    capability_id: str
    version: str
    provider_ref: str
    contract_digest: str
    risk_level: str
    scope: str
    proposed_at: str
    status: str = "proposed"

    def __post_init__(self) -> None:
        for name in (
            "capability_id",
            "version",
            "provider_ref",
            "contract_digest",
            "risk_level",
            "scope",
        ):
            _text(getattr(self, name), name)
        if not self.provider_ref.startswith("synthetic://"):
            raise CapabilityActivationError("provider_ref must be synthetic")
        _timestamp(self.proposed_at, "proposed_at")
        if self.status != "proposed":
            raise CapabilityActivationError("proposal status must be proposed")
        if len(self.contract_digest) != 64 or any(
            character not in "0123456789abcdef" for character in self.contract_digest
        ):
            raise CapabilityActivationError("contract_digest must be a SHA-256 hex digest")


@dataclass(frozen=True, slots=True)
class CapabilityActivationReview:
    review_id: str
    proposal_id: str
    decision: str
    reviewer: str
    evidence_refs: tuple[str, ...]
    reviewed_at: str

    def __post_init__(self) -> None:
        for name in ("review_id", "proposal_id", "reviewer"):
            _text(getattr(self, name), name)
        if self.decision not in {
            ReviewDecision.APPROVE,
            ReviewDecision.REJECT,
            ReviewDecision.REQUEST_REVIEW,
        }:
            raise CapabilityActivationError("unsupported review decision")
        if not self.evidence_refs:
            raise CapabilityActivationError("review requires evidence_refs")
        for reference in self.evidence_refs:
            _text(reference, "review evidence reference")
        _timestamp(self.reviewed_at, "reviewed_at")


@dataclass(frozen=True, slots=True)
class CapabilityActivationPlan:
    plan_id: str
    proposal_id: str
    review_id: str
    decision: str
    status: str
    created_at: str
    plan_digest: str

    def __post_init__(self) -> None:
        for name in ("plan_id", "proposal_id", "review_id", "decision", "plan_digest"):
            _text(getattr(self, name), name)
        if self.status not in {PlanStatus.NOT_ACTIVATED, PlanStatus.DENIED}:
            raise CapabilityActivationError("unsupported activation plan status")
        if self.decision not in {
            ReviewDecision.APPROVE,
            ReviewDecision.REJECT,
            ReviewDecision.REQUEST_REVIEW,
        }:
            raise CapabilityActivationError("unsupported activation plan decision")
        _timestamp(self.created_at, "created_at")
        if len(self.plan_digest) != 64 or any(
            character not in "0123456789abcdef" for character in self.plan_digest
        ):
            raise CapabilityActivationError("plan_digest must be a SHA-256 hex digest")


@dataclass(frozen=True, slots=True)
class ActivationVerification:
    valid: bool
    proposal_binding: bool
    review_binding: bool
    no_runtime_activation: bool
    errors: tuple[str, ...] = ()


class OfflineCapabilityActivationPlanner:
    """Create an unactivated plan from a synthetic proposal and review."""

    def plan(
        self,
        *,
        plan_id: str,
        proposal_id: str,
        proposal: SyntheticCapabilityProposal | None,
        review: CapabilityActivationReview | None,
        created_at: str,
    ) -> CapabilityActivationPlan:
        _timestamp(created_at, "created_at")
        errors: list[str] = []
        if proposal is None:
            errors.append("missing capability proposal")
        if review is None:
            errors.append("missing activation review")
        if proposal is not None and proposal.capability_id != proposal_id:
            errors.append("proposal ID mismatch")
        if review is not None and review.proposal_id != proposal_id:
            errors.append("review proposal ID mismatch")
        decision = review.decision if review else ReviewDecision.REQUEST_REVIEW
        status = PlanStatus.NOT_ACTIVATED if not errors and decision == ReviewDecision.APPROVE else PlanStatus.DENIED
        payload = {
            "plan_id": plan_id,
            "proposal_id": proposal_id,
            "review_id": review.review_id if review else "missing",
            "decision": decision,
            "status": status,
            "created_at": created_at,
        }
        return CapabilityActivationPlan(
            plan_id=plan_id,
            proposal_id=proposal_id,
            review_id=review.review_id if review else "missing",
            decision=decision,
            status=status,
            created_at=created_at,
            plan_digest=_digest(payload),
        )


def verify_activation_plan(
    plan: CapabilityActivationPlan,
    *,
    proposal: SyntheticCapabilityProposal | None,
    review: CapabilityActivationReview | None,
) -> ActivationVerification:
    errors: list[str] = []
    proposal_binding = proposal is not None and plan.proposal_id == proposal.capability_id
    review_binding = review is not None and plan.review_id == review.review_id and review.proposal_id == plan.proposal_id
    if not proposal_binding:
        errors.append("proposal binding mismatch")
    if not review_binding:
        errors.append("review binding mismatch")
    if plan.status not in {PlanStatus.NOT_ACTIVATED, PlanStatus.DENIED}:
        errors.append("runtime activation status is forbidden")
    payload = {
        "plan_id": plan.plan_id,
        "proposal_id": plan.proposal_id,
        "review_id": plan.review_id,
        "decision": plan.decision,
        "status": plan.status,
        "created_at": plan.created_at,
    }
    if _digest(payload) != plan.plan_digest:
        errors.append("activation plan digest mismatch")
    return ActivationVerification(
        valid=not errors,
        proposal_binding=proposal_binding,
        review_binding=review_binding,
        no_runtime_activation=plan.status in {PlanStatus.NOT_ACTIVATED, PlanStatus.DENIED},
        errors=tuple(errors),
    )


__all__ = [
    "ActivationVerification",
    "CapabilityActivationError",
    "CapabilityActivationPlan",
    "CapabilityActivationReview",
    "OfflineCapabilityActivationPlanner",
    "PlanStatus",
    "ReviewDecision",
    "SyntheticCapabilityProposal",
    "verify_activation_plan",
]
