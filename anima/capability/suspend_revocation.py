"""Offline Suspend / Revocation Proof prototype (P3.4).

This module records governance suspend / revoke *decisions* for an unactivated
synthetic capability only.  It never executes a provider, activates a runtime
capability, or changes production state.

Chain of artifacts:

    ActivationRecord            (from capability_activation_proof)
            |  bound by activation_record_digest
            v
    SuspensionRecord           (the suspend request, as an auditable record)
            |  bound by suspension_digest
            v
    RevocationRecord           (the revocation, as an auditable record)
            |  verified by
            v
    SuspendRevocationVerification   (the verification proof)

Every record is immutable (frozen dataclass) and carries SHA-256 digests that
bind it to the upstream record, so any tampering breaks the chain.  The
verification proof fails closed: any missing authorization, broken digest
binding, scope escape, unrelated-capability revocation, or runtime-activation
claim makes it invalid.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from anima.capability.activation_contract import PlanStatus
from anima.capability.activation_proof import ActivationRecord

_NOT_ACTIVATED = PlanStatus.NOT_ACTIVATED
_SYNTHETIC_ONLY = "synthetic-only"


class SuspendRevocationError(ValueError):
    """Raised when a suspend / revocation contract is invalid."""


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SuspendRevocationError(f"{field_name} must be a non-empty string")
    return value


def _timestamp(value: Any, field_name: str) -> datetime:
    value = _text(value, field_name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SuspendRevocationError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise SuspendRevocationError(f"{field_name} must include timezone information")
    return parsed


def _digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _check_digest(value: str, field_name: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise SuspendRevocationError(f"{field_name} must be a SHA-256 hex digest")
    return value


@dataclass(frozen=True, slots=True)
class SuspensionRecord:
    suspension_id: str
    activation_record_id: str
    capability_id: str
    scope: str
    authorization: str
    status: str
    runtime_activation: str
    activation_record_digest: str
    suspended_at: str
    suspension_digest: str

    def __post_init__(self) -> None:
        for name in (
            "suspension_id",
            "activation_record_id",
            "capability_id",
            "scope",
            "authorization",
            "status",
            "runtime_activation",
            "activation_record_digest",
            "suspension_digest",
        ):
            _text(getattr(self, name), name)
        _timestamp(self.suspended_at, "suspended_at")
        if self.runtime_activation != _NOT_ACTIVATED:
            raise SuspendRevocationError("runtime_activation must remain not_activated")
        if self.status != "suspended":
            raise SuspendRevocationError("suspension status must be suspended")
        _check_digest(self.activation_record_digest, "activation_record_digest")
        _check_digest(self.suspension_digest, "suspension_digest")


@dataclass(frozen=True, slots=True)
class RevocationRecord:
    revocation_id: str
    activation_record_id: str
    suspension_id: str
    capability_id: str
    scope: str
    authorization: str
    status: str
    runtime_activation: str
    activation_record_digest: str
    suspension_digest: str
    revoked_at: str
    revocation_digest: str

    def __post_init__(self) -> None:
        for name in (
            "revocation_id",
            "activation_record_id",
            "suspension_id",
            "capability_id",
            "scope",
            "authorization",
            "status",
            "runtime_activation",
            "activation_record_digest",
            "suspension_digest",
            "revocation_digest",
        ):
            _text(getattr(self, name), name)
        _timestamp(self.revoked_at, "revoked_at")
        if self.runtime_activation != _NOT_ACTIVATED:
            raise SuspendRevocationError("runtime_activation must remain not_activated")
        if self.status != "revoked":
            raise SuspendRevocationError("revocation status must be revoked")
        _check_digest(self.activation_record_digest, "activation_record_digest")
        _check_digest(self.suspension_digest, "suspension_digest")
        _check_digest(self.revocation_digest, "revocation_digest")


@dataclass(frozen=True, slots=True)
class SuspendRevocationVerification:
    valid: bool
    authorized: bool
    activation_binding: bool
    suspension_binding: bool
    scope_valid: bool
    no_unrelated_revocation: bool
    digest_intact: bool
    no_runtime_activation: bool
    no_runtime_mutation: bool
    errors: tuple[str, ...] = ()


class OfflineSuspendRevocationPlanner:
    """Record governance suspend / revoke decisions from synthetic inputs only."""

    def record_suspension(
        self,
        *,
        suspension_id: str,
        activation_record: ActivationRecord,
        authorization: str,
        suspended_at: str,
    ) -> SuspensionRecord:
        _timestamp(suspended_at, "suspended_at")
        if not isinstance(activation_record, ActivationRecord):
            raise SuspendRevocationError("suspension requires an ActivationRecord")
        if activation_record.status != _NOT_ACTIVATED:
            raise SuspendRevocationError("cannot suspend an activated record")
        _text(authorization, "authorization")
        if activation_record.scope != _SYNTHETIC_ONLY:
            raise SuspendRevocationError("suspension scope exceeds synthetic-only boundary")
        payload = {
            "suspension_id": suspension_id,
            "activation_record_id": activation_record.record_id,
            "capability_id": activation_record.proposal_id,
            "scope": activation_record.scope,
            "authorization": authorization,
            "status": "suspended",
            "runtime_activation": _NOT_ACTIVATED,
            "activation_record_digest": activation_record.record_digest,
            "suspended_at": suspended_at,
        }
        return SuspensionRecord(
            suspension_id=suspension_id,
            activation_record_id=activation_record.record_id,
            capability_id=activation_record.proposal_id,
            scope=activation_record.scope,
            authorization=authorization,
            status="suspended",
            runtime_activation=_NOT_ACTIVATED,
            activation_record_digest=activation_record.record_digest,
            suspended_at=suspended_at,
            suspension_digest=_digest(payload),
        )

    def record_revocation(
        self,
        *,
        revocation_id: str,
        activation_record: ActivationRecord,
        suspension: SuspensionRecord,
        authorization: str,
        revoked_at: str,
    ) -> RevocationRecord:
        _timestamp(revoked_at, "revoked_at")
        if not isinstance(activation_record, ActivationRecord):
            raise SuspendRevocationError("revocation requires an ActivationRecord")
        if not isinstance(suspension, SuspensionRecord):
            raise SuspendRevocationError("revocation requires a SuspensionRecord")
        if activation_record.status != _NOT_ACTIVATED:
            raise SuspendRevocationError("cannot revoke an activated record")
        if suspension.activation_record_id != activation_record.record_id:
            raise SuspendRevocationError("suspension does not belong to this activation record")
        if suspension.capability_id != activation_record.proposal_id:
            raise SuspendRevocationError("suspension targets a different capability")
        _text(authorization, "authorization")
        if activation_record.scope != _SYNTHETIC_ONLY or suspension.scope != _SYNTHETIC_ONLY:
            raise SuspendRevocationError("revocation scope exceeds synthetic-only boundary")
        payload = {
            "revocation_id": revocation_id,
            "activation_record_id": activation_record.record_id,
            "suspension_id": suspension.suspension_id,
            "capability_id": activation_record.proposal_id,
            "scope": activation_record.scope,
            "authorization": authorization,
            "status": "revoked",
            "runtime_activation": _NOT_ACTIVATED,
            "activation_record_digest": activation_record.record_digest,
            "suspension_digest": suspension.suspension_digest,
            "revoked_at": revoked_at,
        }
        return RevocationRecord(
            revocation_id=revocation_id,
            activation_record_id=activation_record.record_id,
            suspension_id=suspension.suspension_id,
            capability_id=activation_record.proposal_id,
            scope=activation_record.scope,
            authorization=authorization,
            status="revoked",
            runtime_activation=_NOT_ACTIVATED,
            activation_record_digest=activation_record.record_digest,
            suspension_digest=suspension.suspension_digest,
            revoked_at=revoked_at,
            revocation_digest=_digest(payload),
        )


def verify_suspend_revocation(
    *,
    activation_record: ActivationRecord,
    suspension: SuspensionRecord,
    revocation: RevocationRecord,
) -> SuspendRevocationVerification:
    errors: list[str] = []

    authorized = bool(suspension.authorization) and bool(revocation.authorization)

    activation_binding = (
        suspension.activation_record_id == activation_record.record_id
        and revocation.activation_record_id == activation_record.record_id
        and suspension.capability_id == activation_record.proposal_id
    )
    suspension_binding = revocation.suspension_id == suspension.suspension_id

    scope_valid = (
        activation_record.scope == _SYNTHETIC_ONLY
        and suspension.scope == _SYNTHETIC_ONLY
        and revocation.scope == _SYNTHETIC_ONLY
    )

    no_unrelated_revocation = (
        revocation.capability_id == activation_record.proposal_id
        and revocation.capability_id == suspension.capability_id
        and suspension.capability_id == activation_record.proposal_id
    )

    no_runtime_activation = (
        activation_record.status == _NOT_ACTIVATED
        and suspension.runtime_activation == _NOT_ACTIVATED
        and revocation.runtime_activation == _NOT_ACTIVATED
    )

    # Digest binding: each record must re-digest to its own stored digest, and
    # each downstream record must carry the upstream digests unchanged.
    susp_payload = {
        "suspension_id": suspension.suspension_id,
        "activation_record_id": suspension.activation_record_id,
        "capability_id": suspension.capability_id,
        "scope": suspension.scope,
        "authorization": suspension.authorization,
        "status": suspension.status,
        "runtime_activation": suspension.runtime_activation,
        "activation_record_digest": suspension.activation_record_digest,
        "suspended_at": suspension.suspended_at,
    }
    if _digest(susp_payload) != suspension.suspension_digest:
        errors.append("suspension digest mismatch or tampering")

    rev_payload = {
        "revocation_id": revocation.revocation_id,
        "activation_record_id": revocation.activation_record_id,
        "suspension_id": revocation.suspension_id,
        "capability_id": revocation.capability_id,
        "scope": revocation.scope,
        "authorization": revocation.authorization,
        "status": revocation.status,
        "runtime_activation": revocation.runtime_activation,
        "activation_record_digest": revocation.activation_record_digest,
        "suspension_digest": revocation.suspension_digest,
        "revoked_at": revocation.revoked_at,
    }
    if _digest(rev_payload) != revocation.revocation_digest:
        errors.append("revocation digest mismatch or tampering")

    if revocation.activation_record_digest != activation_record.record_digest:
        errors.append("revocation activation digest binding mismatch")
    if revocation.suspension_digest != suspension.suspension_digest:
        errors.append("revocation suspension digest binding mismatch")
    if suspension.activation_record_digest != activation_record.record_digest:
        errors.append("suspension activation digest binding mismatch")

    if not authorized:
        errors.append("suspend/revoke requires authorization")
    if not activation_binding:
        errors.append("activation binding mismatch")
    if not suspension_binding:
        errors.append("suspension binding mismatch")
    if not scope_valid:
        errors.append("scope exceeds synthetic-only boundary")
    if not no_unrelated_revocation:
        errors.append("revocation targets an unrelated capability")
    if not no_runtime_activation:
        errors.append("runtime activation is forbidden")

    digest_intact = (
        _digest(susp_payload) == suspension.suspension_digest
        and _digest(rev_payload) == revocation.revocation_digest
        and revocation.activation_record_digest == activation_record.record_digest
        and revocation.suspension_digest == suspension.suspension_digest
        and suspension.activation_record_digest == activation_record.record_digest
    )
    no_runtime_mutation = no_runtime_activation

    return SuspendRevocationVerification(
        valid=not errors,
        authorized=authorized,
        activation_binding=activation_binding,
        suspension_binding=suspension_binding,
        scope_valid=scope_valid,
        no_unrelated_revocation=no_unrelated_revocation,
        digest_intact=digest_intact,
        no_runtime_activation=no_runtime_activation,
        no_runtime_mutation=no_runtime_mutation,
        errors=tuple(errors),
    )


__all__ = [
    "OfflineSuspendRevocationPlanner",
    "RevocationRecord",
    "SuspendRevocationError",
    "SuspendRevocationVerification",
    "SuspensionRecord",
    "verify_suspend_revocation",
]
