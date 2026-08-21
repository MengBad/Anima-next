"""Offline, deny-by-default Operator Action Contract prototype.

This module plans an action record only.  It never executes, persists, or
dispatches an action and has no runtime or production adapter.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from anima.report_contract import ReportRecord, ReportVerification


class ActionContractError(ValueError):
    """Raised when an action contract is invalid."""


class ActionStatus:
    NOT_EXECUTED = "not_executed"
    DENIED = "denied"


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ActionContractError(f"{field_name} must be a non-empty string")
    return value


def _timestamp(value: Any, field_name: str) -> datetime:
    value = _text(value, field_name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ActionContractError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ActionContractError(f"{field_name} must include timezone information")
    return parsed


def _digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SyntheticAuthorization:
    authorization_id: str
    operator_id: str
    role: str
    action_type: str
    subject: str
    report_digest: str
    issued_at: str
    expires_at: str
    status: str = "authorized"

    def __post_init__(self) -> None:
        for name in (
            "authorization_id",
            "operator_id",
            "role",
            "action_type",
            "subject",
            "report_digest",
        ):
            _text(getattr(self, name), name)
        issued = _timestamp(self.issued_at, "issued_at")
        expires = _timestamp(self.expires_at, "expires_at")
        if expires <= issued:
            raise ActionContractError("expires_at must be after issued_at")
        if len(self.report_digest) != 64 or any(
            character not in "0123456789abcdef" for character in self.report_digest
        ):
            raise ActionContractError("report_digest must be a SHA-256 hex digest")
        if self.status != "authorized":
            raise ActionContractError("only authorized synthetic records may be planned")


@dataclass(frozen=True, slots=True)
class OperatorActionRecord:
    action_id: str
    authorization_id: str
    operator_id: str
    action_type: str
    subject: str
    report_digest: str
    status: str
    created_at: str
    action_digest: str

    def __post_init__(self) -> None:
        for name in (
            "action_id",
            "authorization_id",
            "operator_id",
            "action_type",
            "subject",
            "report_digest",
            "action_digest",
        ):
            _text(getattr(self, name), name)
        _timestamp(self.created_at, "created_at")
        if self.status != ActionStatus.NOT_EXECUTED:
            raise ActionContractError("planned action must be not_executed")
        for value in (self.report_digest, self.action_digest):
            if len(value) != 64 or any(
                character not in "0123456789abcdef" for character in value
            ):
                raise ActionContractError("action digests must be SHA-256 hex digests")


@dataclass(frozen=True, slots=True)
class ActionPlanResult:
    allowed: bool
    action: OperatorActionRecord | None
    errors: tuple[str, ...] = ()


class OfflineOperatorActionPlanner:
    """Validate authorization and create an unexecuted action record only."""

    def plan(
        self,
        *,
        action_id: str,
        report: ReportRecord,
        report_verification: ReportVerification,
        authorization: SyntheticAuthorization | None,
        created_at: str,
        now: str,
    ) -> ActionPlanResult:
        errors: list[str] = []
        if not report_verification.valid:
            errors.append("report verification did not pass")
        if authorization is None:
            errors.append("authorization is required")
        else:
            current = _timestamp(now, "now")
            expires = _timestamp(authorization.expires_at, "expires_at")
            if current >= expires:
                errors.append("authorization is expired")
            if authorization.report_digest != report.report_digest:
                errors.append("authorization report_digest does not match report")
            if authorization.subject != report.subject:
                errors.append("authorization subject does not match report")
        if errors:
            return ActionPlanResult(False, None, tuple(errors))

        assert authorization is not None
        payload = {
            "action_id": action_id,
            "authorization_id": authorization.authorization_id,
            "operator_id": authorization.operator_id,
            "action_type": authorization.action_type,
            "subject": authorization.subject,
            "report_digest": report.report_digest,
            "status": ActionStatus.NOT_EXECUTED,
            "created_at": created_at,
        }
        action = OperatorActionRecord(
            action_id=action_id,
            authorization_id=authorization.authorization_id,
            operator_id=authorization.operator_id,
            action_type=authorization.action_type,
            subject=authorization.subject,
            report_digest=report.report_digest,
            status=ActionStatus.NOT_EXECUTED,
            created_at=created_at,
            action_digest=_digest(payload),
        )
        return ActionPlanResult(True, action)


__all__ = [
    "ActionContractError",
    "ActionPlanResult",
    "ActionStatus",
    "OfflineOperatorActionPlanner",
    "OperatorActionRecord",
    "SyntheticAuthorization",
]
