"""Offline Observer -> Operator workflow integrity prototype.

The workflow records a verified chain of references.  It deliberately has no
executor, persistence, runtime adapter, scheduler, or production access.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from anima.observer_contract import ObservationRecord
from anima.operator_action_contract import (
    ActionStatus,
    OperatorActionRecord,
    SyntheticAuthorization,
)
from anima.report_contract import ReportRecord, ReportVerification


class WorkflowContractError(ValueError):
    """Raised when a workflow contract is invalid."""


class WorkflowStatus:
    CREATED = "created"
    VALIDATED = "validated"
    AUTHORIZED = "authorized"
    RECORDED = "recorded"
    VERIFIED = "verified"
    FAILED = "failed"


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowContractError(f"{field_name} must be a non-empty string")
    return value


def _timestamp(value: Any, field_name: str) -> datetime:
    value = _text(value, field_name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkflowContractError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise WorkflowContractError(f"{field_name} must include timezone information")
    return parsed


def _digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    verification_id: str
    workflow_id: str
    action_id: str
    result: str
    observed_at: str
    digest: str

    def __post_init__(self) -> None:
        for name in (
            "verification_id",
            "workflow_id",
            "action_id",
            "result",
            "digest",
        ):
            _text(getattr(self, name), name)
        _timestamp(self.observed_at, "observed_at")
        if len(self.digest) != 64 or any(
            character not in "0123456789abcdef" for character in self.digest
        ):
            raise WorkflowContractError("verification digest must be a SHA-256 hex digest")

    @classmethod
    def create(
        cls,
        *,
        verification_id: str,
        workflow_id: str,
        action_id: str,
        result: str,
        observed_at: str,
    ) -> "VerificationEvidence":
        payload = {
            "verification_id": verification_id,
            "workflow_id": workflow_id,
            "action_id": action_id,
            "result": result,
            "observed_at": observed_at,
        }
        return cls(**payload, digest=_digest(payload))


@dataclass(frozen=True, slots=True)
class WorkflowRecord:
    workflow_id: str
    observation_refs: tuple[str, ...]
    report_ref: str
    authorization_ref: str
    action_ref: str
    verification_refs: tuple[str, ...]
    status: str
    workflow_digest: str
    errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.workflow_id, "workflow_id")
        for name, values in (
            ("observation_refs", self.observation_refs),
            ("verification_refs", self.verification_refs),
        ):
            if not values:
                raise WorkflowContractError(f"{name} must not be empty")
            for value in values:
                _text(value, name[:-1])
        for name in ("report_ref", "authorization_ref", "action_ref", "workflow_digest"):
            _text(getattr(self, name), name)
        if self.status not in {
            WorkflowStatus.CREATED,
            WorkflowStatus.VALIDATED,
            WorkflowStatus.AUTHORIZED,
            WorkflowStatus.RECORDED,
            WorkflowStatus.VERIFIED,
            WorkflowStatus.FAILED,
        }:
            raise WorkflowContractError("unsupported workflow status")
        if len(self.workflow_digest) != 64 or any(
            character not in "0123456789abcdef" for character in self.workflow_digest
        ):
            raise WorkflowContractError("workflow_digest must be a SHA-256 hex digest")
        for error in self.errors:
            _text(error, "workflow error")


@dataclass(frozen=True, slots=True)
class WorkflowVerification:
    workflow_valid: bool
    reference_binding: bool
    integrity_valid: bool
    verification_chain_valid: bool
    errors: tuple[str, ...] = ()


class OfflineWorkflowBuilder:
    """Build a workflow record without executing or persisting anything."""

    def build(
        self,
        *,
        workflow_id: str,
        observations: tuple[ObservationRecord, ...],
        report: ReportRecord | None,
        report_verification: ReportVerification | None,
        authorization: SyntheticAuthorization | None,
        action: OperatorActionRecord | None,
        verification_evidence: tuple[VerificationEvidence, ...],
        created_at: str,
        now: str,
    ) -> WorkflowRecord:
        _timestamp(created_at, "created_at")
        errors = _collect_errors(
            workflow_id=workflow_id,
            observations=observations,
            report=report,
            report_verification=report_verification,
            authorization=authorization,
            action=action,
            verification_evidence=verification_evidence,
            now=now,
        )
        observation_refs = tuple(item.digest for item in observations)
        report_ref = report.report_digest if report else "missing"
        authorization_ref = authorization.authorization_id if authorization else "missing"
        action_ref = action.action_id if action else "missing"
        verification_refs = tuple(item.verification_id for item in verification_evidence)
        status = WorkflowStatus.VERIFIED if not errors else WorkflowStatus.FAILED
        payload = {
            "workflow_id": workflow_id,
            "observation_refs": list(observation_refs),
            "report_ref": report_ref,
            "authorization_ref": authorization_ref,
            "action_ref": action_ref,
            "verification_refs": list(verification_refs),
            "status": status,
            "errors": list(errors),
        }
        return WorkflowRecord(
            workflow_id=workflow_id,
            observation_refs=observation_refs or ("missing",),
            report_ref=report_ref,
            authorization_ref=authorization_ref,
            action_ref=action_ref,
            verification_refs=verification_refs or ("missing",),
            status=status,
            workflow_digest=_digest(payload),
            errors=tuple(errors),
        )


def verify_workflow(
    workflow: WorkflowRecord,
    *,
    observations: tuple[ObservationRecord, ...],
    report: ReportRecord | None,
    authorization: SyntheticAuthorization | None,
    action: OperatorActionRecord | None,
    verification_evidence: tuple[VerificationEvidence, ...],
    now: str,
) -> WorkflowVerification:
    errors = list(
        _collect_errors(
            workflow_id=workflow.workflow_id,
            observations=observations,
            report=report,
            report_verification=ReportVerification(True, True, True, True),
            authorization=authorization,
            action=action,
            verification_evidence=verification_evidence,
            now=now,
        )
    )
    observation_refs = tuple(item.digest for item in observations)
    report_ref = report.report_digest if report else "missing"
    authorization_ref = authorization.authorization_id if authorization else "missing"
    action_ref = action.action_id if action else "missing"
    verification_refs = tuple(item.verification_id for item in verification_evidence)
    if workflow.observation_refs != (observation_refs or ("missing",)):
        errors.append("observation references do not match workflow")
    if workflow.report_ref != report_ref:
        errors.append("report reference does not match workflow")
    if workflow.authorization_ref != authorization_ref:
        errors.append("authorization reference does not match workflow")
    if workflow.action_ref != action_ref:
        errors.append("action reference does not match workflow")
    if workflow.verification_refs != (verification_refs or ("missing",)):
        errors.append("verification references do not match workflow")
    payload = {
        "workflow_id": workflow.workflow_id,
        "observation_refs": list(workflow.observation_refs if workflow.observation_refs != ("missing",) else ()),
        "report_ref": workflow.report_ref,
        "authorization_ref": workflow.authorization_ref,
        "action_ref": workflow.action_ref,
        "verification_refs": list(workflow.verification_refs if workflow.verification_refs != ("missing",) else ()),
        "status": workflow.status,
        "errors": list(workflow.errors),
    }
    if _digest(payload) != workflow.workflow_digest:
        errors.append("workflow digest mismatch")
    verification_chain_valid = bool(verification_evidence) and all(
        item.workflow_id == workflow.workflow_id
        and item.action_id == workflow.action_ref
        and _verify_evidence_digest(item)
        for item in verification_evidence
    )
    if not verification_chain_valid:
        errors.append("verification evidence does not bind to workflow")
    return WorkflowVerification(
        workflow_valid=not errors,
        reference_binding=not any("reference" in error for error in errors),
        integrity_valid=not any("digest" in error for error in errors),
        verification_chain_valid=verification_chain_valid,
        errors=tuple(errors),
    )


def _collect_errors(
    *,
    workflow_id: str,
    observations: tuple[ObservationRecord, ...],
    report: ReportRecord | None,
    report_verification: ReportVerification | None,
    authorization: SyntheticAuthorization | None,
    action: OperatorActionRecord | None,
    verification_evidence: tuple[VerificationEvidence, ...],
    now: str,
) -> tuple[str, ...]:
    errors: list[str] = []
    if not observations:
        errors.append("missing observation")
    if report is None:
        errors.append("missing report")
    if report_verification is None or not report_verification.valid:
        errors.append("report verification did not pass")
    if authorization is None:
        errors.append("missing authorization")
    if action is None:
        errors.append("missing action record")
    if not verification_evidence:
        errors.append("missing verification evidence")
    if report is not None and observations and report.observation_digest not in {
        item.digest for item in observations
    }:
        errors.append("report mismatch")
    if report is not None and authorization is not None:
        if authorization.report_digest != report.report_digest:
            errors.append("authorization report mismatch")
        if _timestamp(now, "now") >= _timestamp(authorization.expires_at, "expires_at"):
            errors.append("expired authorization")
    if authorization is not None and action is not None:
        if action.authorization_id != authorization.authorization_id:
            errors.append("action authorization mismatch")
        if action.status != ActionStatus.NOT_EXECUTED:
            errors.append("action is not a not_executed record")
    for item in verification_evidence:
        if item.workflow_id != workflow_id:
            errors.append("verification workflow mismatch")
        if not _verify_evidence_digest(item):
            errors.append("verification evidence digest mismatch")
    return tuple(errors)


def _verify_evidence_digest(item: VerificationEvidence) -> bool:
    payload = {
        "verification_id": item.verification_id,
        "workflow_id": item.workflow_id,
        "action_id": item.action_id,
        "result": item.result,
        "observed_at": item.observed_at,
    }
    return _digest(payload) == item.digest


__all__ = [
    "OfflineWorkflowBuilder",
    "VerificationEvidence",
    "WorkflowContractError",
    "WorkflowRecord",
    "WorkflowStatus",
    "WorkflowVerification",
    "verify_workflow",
]
