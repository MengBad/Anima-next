"""Offline controlled staged activation prototype.

Staging is represented as an auditable record only.  It never activates a
runtime capability, invokes a provider, or changes production state.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from anima.capability_activation_contract import (
    CapabilityActivationPlan,
    SyntheticCapabilityProposal,
)
from anima.capability_activation_proof import (
    ActivationProof,
    ActivationRecord,
    verify_activation_proof,
)


class StagedActivationError(ValueError):
    """Raised when a staged activation contract is invalid."""


class StageStatus:
    STAGED = "staged"
    OBSERVING = "observing"
    FAILED = "failed"


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StagedActivationError(f"{field_name} must be a non-empty string")
    return value


def _timestamp(value: Any, field_name: str) -> datetime:
    value = _text(value, field_name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StagedActivationError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise StagedActivationError(f"{field_name} must include timezone information")
    return parsed


def _digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class StagedActivationRecord:
    stage_id: str
    plan_id: str
    activation_record_id: str
    proof_id: str
    capability_id: str
    scope: str
    stage: str
    runtime_activation: str
    gate_refs: tuple[str, ...]
    staged_at: str
    stage_digest: str

    def __post_init__(self) -> None:
        for name in (
            "stage_id",
            "plan_id",
            "activation_record_id",
            "proof_id",
            "capability_id",
            "scope",
            "stage",
            "runtime_activation",
            "stage_digest",
        ):
            _text(getattr(self, name), name)
        if self.stage not in {StageStatus.STAGED, StageStatus.OBSERVING, StageStatus.FAILED}:
            raise StagedActivationError("unsupported stage status")
        if self.runtime_activation != "not_activated":
            raise StagedActivationError("runtime_activation must remain not_activated")
        if not self.gate_refs:
            raise StagedActivationError("gate_refs must not be empty")
        for reference in self.gate_refs:
            _text(reference, "gate reference")
        _timestamp(self.staged_at, "staged_at")
        if len(self.stage_digest) != 64 or any(
            character not in "0123456789abcdef" for character in self.stage_digest
        ):
            raise StagedActivationError("stage_digest must be a SHA-256 hex digest")


@dataclass(frozen=True, slots=True)
class StagedActivationVerification:
    valid: bool
    proof_valid: bool
    gate_valid: bool
    no_runtime_activation: bool
    errors: tuple[str, ...] = ()


class OfflineStagedActivationPlanner:
    """Create a staged record only after a valid synthetic activation proof."""

    def stage(
        self,
        *,
        stage_id: str,
        plan: CapabilityActivationPlan,
        record: ActivationRecord,
        proof: ActivationProof,
        proposal: SyntheticCapabilityProposal,
        gate_refs: tuple[str, ...],
        staged_at: str,
    ) -> StagedActivationRecord:
        _timestamp(staged_at, "staged_at")
        verification = verify_activation_proof(
            plan=plan, record=record, proof=proof, proposal=proposal
        )
        if not verification.valid:
            raise StagedActivationError("activation proof verification failed")
        if not gate_refs:
            raise StagedActivationError("gate_refs must not be empty")
        payload = {
            "stage_id": stage_id,
            "plan_id": plan.plan_id,
            "activation_record_id": record.record_id,
            "proof_id": proof.proof_id,
            "capability_id": proposal.capability_id,
            "scope": proposal.scope,
            "stage": StageStatus.STAGED,
            "runtime_activation": "not_activated",
            "gate_refs": list(gate_refs),
            "staged_at": staged_at,
        }
        return StagedActivationRecord(
            stage_id=stage_id,
            plan_id=plan.plan_id,
            activation_record_id=record.record_id,
            proof_id=proof.proof_id,
            capability_id=proposal.capability_id,
            scope=proposal.scope,
            stage=StageStatus.STAGED,
            runtime_activation="not_activated",
            gate_refs=gate_refs,
            staged_at=staged_at,
            stage_digest=_digest(payload),
        )


def verify_staged_activation(
    *,
    staged: StagedActivationRecord,
    plan: CapabilityActivationPlan,
    record: ActivationRecord,
    proof: ActivationProof,
    proposal: SyntheticCapabilityProposal,
) -> StagedActivationVerification:
    proof_result = verify_activation_proof(
        plan=plan, record=record, proof=proof, proposal=proposal
    )
    errors = list(proof_result.errors)
    gate_valid = bool(staged.gate_refs)
    if staged.plan_id != plan.plan_id:
        errors.append("staged plan binding mismatch")
    if staged.activation_record_id != record.record_id:
        errors.append("staged activation record binding mismatch")
    if staged.proof_id != proof.proof_id:
        errors.append("staged proof binding mismatch")
    if staged.capability_id != proposal.capability_id:
        errors.append("staged capability binding mismatch")
    if staged.scope != proposal.scope or staged.scope != "synthetic-only":
        errors.append("staged scope exceeds synthetic-only boundary")
        gate_valid = False
    no_runtime_activation = staged.runtime_activation == "not_activated"
    if not no_runtime_activation:
        errors.append("runtime activation is forbidden")
    payload = {
        "stage_id": staged.stage_id,
        "plan_id": staged.plan_id,
        "activation_record_id": staged.activation_record_id,
        "proof_id": staged.proof_id,
        "capability_id": staged.capability_id,
        "scope": staged.scope,
        "stage": staged.stage,
        "runtime_activation": staged.runtime_activation,
        "gate_refs": list(staged.gate_refs),
        "staged_at": staged.staged_at,
    }
    if _digest(payload) != staged.stage_digest:
        errors.append("staged activation digest mismatch")
    return StagedActivationVerification(
        valid=not errors,
        proof_valid=proof_result.valid,
        gate_valid=gate_valid,
        no_runtime_activation=no_runtime_activation,
        errors=tuple(errors),
    )


__all__ = [
    "OfflineStagedActivationPlanner",
    "StageStatus",
    "StagedActivationError",
    "StagedActivationRecord",
    "StagedActivationVerification",
    "verify_staged_activation",
]
