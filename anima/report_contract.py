"""Offline Evidence -> Report Contract prototype.

Reports are immutable summaries of explicitly supplied synthetic observation
and evidence.  This module has no action executor, persistence, runtime
adapter, or production access.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping

from anima.observer_contract import (
    ObservationRecord,
    SyntheticEvidence,
    verify_observation,
)


class ReportContractError(ValueError):
    """Raised when an Evidence -> Report contract is invalid."""


class ReportStatus:
    OBSERVED = "observed"
    INCOMPLETE = "incomplete"


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReportContractError(f"{field_name} must be a non-empty string")
    return value


def _timestamp(value: Any) -> str:
    value = _text(value, "generated_at")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReportContractError("generated_at must be an ISO-8601 timestamp") from exc
    return value


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        _plain(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ReportRecord:
    report_id: str
    observation_digest: str
    subject: str
    generated_at: str
    evidence_refs: tuple[str, ...]
    findings: tuple[str, ...]
    status: str
    report_digest: str

    def __post_init__(self) -> None:
        for name in ("report_id", "observation_digest", "subject", "report_digest"):
            _text(getattr(self, name), name)
        _timestamp(self.generated_at)
        if self.status not in {ReportStatus.OBSERVED, ReportStatus.INCOMPLETE}:
            raise ReportContractError("unsupported report status")
        if not self.evidence_refs:
            raise ReportContractError("evidence_refs must not be empty")
        for value in (*self.evidence_refs, *self.findings):
            _text(value, "report reference/finding")
        for value in (self.observation_digest, self.report_digest):
            if len(value) != 64 or any(
                character not in "0123456789abcdef" for character in value
            ):
                raise ReportContractError("report digests must be SHA-256 hex digests")


@dataclass(frozen=True, slots=True)
class ReportVerification:
    valid: bool
    observation_valid: bool
    report_digest_match: bool
    evidence_complete: bool
    errors: tuple[str, ...] = ()


class OfflineReportBuilder:
    """Build an in-memory report from explicitly supplied synthetic inputs."""

    def build(
        self,
        *,
        report_id: str,
        observation: ObservationRecord,
        evidence: tuple[SyntheticEvidence, ...],
        findings: tuple[str, ...],
        generated_at: str,
    ) -> ReportRecord:
        copied_findings = tuple(copy.deepcopy(findings))
        observation_result = verify_observation(observation, evidence)
        status = ReportStatus.OBSERVED if observation_result.valid else ReportStatus.INCOMPLETE
        evidence_refs = observation.evidence_refs
        payload = {
            "report_id": report_id,
            "observation_digest": observation.digest,
            "subject": observation.subject,
            "generated_at": generated_at,
            "evidence_refs": list(evidence_refs),
            "findings": list(copied_findings),
            "status": status,
        }
        return ReportRecord(
            report_id=report_id,
            observation_digest=observation.digest,
            subject=observation.subject,
            generated_at=generated_at,
            evidence_refs=evidence_refs,
            findings=copied_findings,
            status=status,
            report_digest=_digest(payload),
        )


def verify_report(
    report: ReportRecord,
    observation: ObservationRecord,
    evidence: tuple[SyntheticEvidence, ...],
) -> ReportVerification:
    observation_result = verify_observation(observation, evidence)
    errors = list(observation_result.errors)
    if report.observation_digest != observation.digest:
        errors.append("report observation_digest does not match observation")
    if report.subject != observation.subject:
        errors.append("report subject does not match observation")
    if tuple(report.evidence_refs) != tuple(observation.evidence_refs):
        errors.append("report evidence_refs do not match observation")
    payload = {
        "report_id": report.report_id,
        "observation_digest": report.observation_digest,
        "subject": report.subject,
        "generated_at": report.generated_at,
        "evidence_refs": list(report.evidence_refs),
        "findings": list(report.findings),
        "status": report.status,
    }
    digest_match = _digest(payload) == report.report_digest
    if not digest_match:
        errors.append("report digest mismatch")
    return ReportVerification(
        valid=not errors,
        observation_valid=observation_result.valid,
        report_digest_match=digest_match,
        evidence_complete=observation_result.evidence_complete,
        errors=tuple(errors),
    )


__all__ = [
    "OfflineReportBuilder",
    "ReportContractError",
    "ReportRecord",
    "ReportStatus",
    "ReportVerification",
    "verify_report",
]
