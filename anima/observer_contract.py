"""Offline, read-only Observer Contract prototype.

The module records synthetic observations only.  It has no runtime adapter,
writer, scheduler, action executor, or production connection.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping


class ObserverContractError(ValueError):
    """Raised when an observation contract is invalid."""


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ObserverContractError(f"{field_name} must be a non-empty string")
    return value


def _timestamp(value: Any) -> str:
    value = _text(value, "observed_at")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ObserverContractError("observed_at must be an ISO-8601 timestamp") from exc
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
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
class SyntheticEvidence:
    evidence_id: str
    evidence_type: str
    content: str
    observed_at: str

    def __post_init__(self) -> None:
        _text(self.evidence_id, "evidence_id")
        _text(self.evidence_type, "evidence_type")
        _text(self.content, "content")
        _timestamp(self.observed_at)

    def to_mapping(self) -> dict[str, str]:
        return {
            "evidence_id": self.evidence_id,
            "evidence_type": self.evidence_type,
            "content": self.content,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True, slots=True)
class ObservationRecord:
    observer_id: str
    observed_at: str
    subject: str
    evidence_refs: tuple[str, ...]
    runtime_state: Mapping[str, Any]
    digest: str

    def __post_init__(self) -> None:
        _text(self.observer_id, "observer_id")
        _timestamp(self.observed_at)
        _text(self.subject, "subject")
        _text(self.digest, "digest")
        if len(self.digest) != 64 or any(
            character not in "0123456789abcdef" for character in self.digest
        ):
            raise ObserverContractError("digest must be a SHA-256 hex digest")
        if not self.evidence_refs:
            raise ObserverContractError("evidence_refs must not be empty")
        for reference in self.evidence_refs:
            _text(reference, "evidence reference")
        if not isinstance(self.runtime_state, Mapping):
            raise ObserverContractError("runtime_state must be an object")

    def payload(self, evidence: tuple[SyntheticEvidence, ...]) -> dict[str, Any]:
        return {
            "observer_id": self.observer_id,
            "observed_at": self.observed_at,
            "subject": self.subject,
            "evidence_refs": list(self.evidence_refs),
            "runtime_state": _plain(self.runtime_state),
            "evidence": [item.to_mapping() for item in evidence],
        }


@dataclass(frozen=True, slots=True)
class ObservationVerification:
    valid: bool
    digest_match: bool
    evidence_complete: bool
    errors: tuple[str, ...] = ()


class OfflineObserver:
    """Create observations in memory without persistence or side effects."""

    def observe(
        self,
        *,
        observer_id: str,
        observed_at: str,
        subject: str,
        runtime_state: Mapping[str, Any],
        evidence: tuple[SyntheticEvidence, ...],
    ) -> ObservationRecord:
        copied_state = copy.deepcopy(dict(runtime_state))
        evidence_refs = tuple(item.evidence_id for item in evidence)
        payload = {
            "observer_id": observer_id,
            "observed_at": observed_at,
            "subject": subject,
            "evidence_refs": list(evidence_refs),
            "runtime_state": copied_state,
            "evidence": [item.to_mapping() for item in evidence],
        }
        record = ObservationRecord(
            observer_id=observer_id,
            observed_at=observed_at,
            subject=subject,
            evidence_refs=evidence_refs,
            runtime_state=_freeze(copied_state),
            digest=_digest(payload),
        )
        return record


def verify_observation(
    record: ObservationRecord,
    evidence: tuple[SyntheticEvidence, ...],
) -> ObservationVerification:
    evidence_by_id = {item.evidence_id: item for item in evidence}
    missing = tuple(
        reference for reference in record.evidence_refs if reference not in evidence_by_id
    )
    payload = record.payload(tuple(evidence_by_id[ref] for ref in record.evidence_refs if ref in evidence_by_id))
    computed_digest = _digest(payload)
    errors: list[str] = []
    if missing:
        errors.append(f"missing evidence_refs: {list(missing)}")
    digest_match = computed_digest == record.digest
    if not digest_match:
        errors.append("observation digest mismatch")
    return ObservationVerification(
        valid=not errors,
        digest_match=digest_match,
        evidence_complete=not missing,
        errors=tuple(errors),
    )


__all__ = [
    "ObservationRecord",
    "ObservationVerification",
    "ObserverContractError",
    "OfflineObserver",
    "SyntheticEvidence",
    "verify_observation",
]
