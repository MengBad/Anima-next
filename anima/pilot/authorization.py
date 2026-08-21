"""P8.12 — Final Authorization System (approval record + enable token +
expiry + scope + revoke).

The pilot's FINAL authorization gate: a live traffic pilot only operates under
an explicit, scoped, expiring, revocable authorization.

    * approval record - the immutable owner approval (who / when / why / scope)
    * enable token    - a secret token that must be presented to enable
    * expiry          - the authorization expires (wall-clock), after which
                        everything is denied (fail closed)
    * scope           - the authorized scope (groups, capabilities)
    * revoke          - explicit revoke kills the authorization immediately

This is a POLICY object: it records/validates authorization state; it never
executes, never mutates the system, never touches System B.

Scope limits (P8.12): no System B, no gateway, no capability activation, no
env bypass.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

__all__ = [
    "PilotAuthorization",
    "ApprovalRecord",
    "AuthorizationStore",
    "AuthorizationAuditLog",
    "new_enable_token",
    "token_digest",
]


def new_enable_token() -> str:
    """A fresh enable token (secret)."""
    return secrets.token_urlsafe(24)


def token_digest(token: str) -> str:
    """One-way digest of an enable token (store the digest, never the secret)."""
    return hashlib.sha256(f"pilot-enable:{token}".encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """Immutable owner approval (P8.12).

    Field names follow the authorization contract: operator / enabled_time /
    expire_time / scope.  ``owner`` and ``approved_at`` are compatibility
    aliases.
    """

    approval_id: str
    operator: str = "owner"
    enabled_time: float = 0.0
    expire_time: float = 0.0
    reason: str = ""
    scope_groups: tuple = ()
    scope_capabilities: tuple = ()
    # compatibility aliases
    owner: Optional[str] = None
    approved_at: Optional[float] = None

    def __post_init__(self) -> None:
        if self.owner is None:
            object.__setattr__(self, "owner", self.operator)
        if self.approved_at is None:
            object.__setattr__(self, "approved_at", self.enabled_time)

    def to_dict(self) -> dict:
        return {
            "approval_id": self.approval_id,
            "operator": self.operator,
            "enabled_time": self.enabled_time,
            "expire_time": self.expire_time,
            "reason": self.reason,
            "scope_groups": list(self.scope_groups),
            "scope_capabilities": list(self.scope_capabilities),
        }


class PilotAuthorization:
    """Scoped, expiring, revocable live-pilot authorization (fail closed).

    P8.12: explicit lifecycle — ``enable()`` activates (default OFF until
    called), ``disable()`` deactivates, ``revoke()`` kills permanently,
    ``expired()`` checks wall-clock expiry.  ``allows()`` is fail closed:
    enabled + token + scope + not expired + not revoked.
    """

    def __init__(
        self,
        *,
        approval: ApprovalRecord,
        enable_token: str,
        ttl_sec: float = 0.0,
        authz_id: Optional[str] = None,
        audit_log: Optional["AuthorizationAuditLog"] = None,
    ) -> None:
        self._approval = approval
        self._token_digest = token_digest(enable_token)
        self._ttl_sec = ttl_sec
        self._issued_at = time.time()
        self._authz_id = authz_id or f"authz-{secrets.token_hex(6)}"
        self._revoked = False
        self._enabled = False  # P8.12: default OFF until enable()
        self._audit_log = audit_log

    def _audit(self, event: str, **extra) -> None:
        if self._audit_log is None:
            return
        try:
            self._audit_log.record(
                event,
                authz_id=self._authz_id,
                approval_id=self._approval.approval_id,
                operator=self._approval.operator,
                reason=extra.pop("reason", ""),
                **extra,
            )
        except Exception:  # noqa: BLE001 - auditing must never break authz
            pass

    # -- facts ---------------------------------------------------------------

    @property
    def authz_id(self) -> str:
        return self._authz_id

    @property
    def approval(self) -> ApprovalRecord:
        return self._approval

    # -- lifecycle (P8.12) ----------------------------------------------------

    def enable(self) -> None:
        """Explicit enable (never derived from env/config)."""
        if self._revoked:
            raise RuntimeError("authorization revoked, cannot enable")
        self._enabled = True
        self._audit("enabled")

    def disable(self) -> None:
        """Explicit disable (deactivates without revoking)."""
        self._enabled = False
        self._audit("disabled")

    def is_enabled(self) -> bool:
        return self._enabled

    def expired(self, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.time()
        if self._ttl_sec > 0 and now - self._issued_at > self._ttl_sec:
            return True
        if self._approval.expire_time > 0 and now > self._approval.expire_time:
            return True
        return False

    def revoke(self) -> None:
        self._revoked = True
        self._enabled = False
        self._audit("revoked")

    def is_revoked(self) -> bool:
        return self._revoked

    # -- gate -------------------------------------------------------------------

    def allows(
        self,
        *,
        group_id: str,
        capability: str,
        token: Optional[str] = None,
        now: Optional[float] = None,
    ) -> tuple[bool, str]:
        """Fail-closed check: enabled + approval + token + scope + not expired
        + not revoked."""
        if self._revoked:
            self._audit("denied", reason="revoked", group_id=group_id,
                        capability=capability)
            return False, "revoked"
        if not self._enabled:
            self._audit("denied", reason="not_enabled", group_id=group_id,
                        capability=capability)
            return False, "not_enabled"
        if self.expired(now):
            self._audit("denied", reason="expired", group_id=group_id,
                        capability=capability)
            return False, "expired"
        if token is None:
            self._audit("denied", reason="token_required", group_id=group_id,
                        capability=capability)
            return False, "token_required"
        if not hmac.compare_digest(token_digest(token), self._token_digest):
            self._audit("denied", reason="bad_token", group_id=group_id,
                        capability=capability)
            return False, "bad_token"
        if group_id not in self._approval.scope_groups:
            self._audit("denied", reason="group_out_of_scope", group_id=group_id,
                        capability=capability)
            return False, "group_out_of_scope"
        if capability not in self._approval.scope_capabilities:
            self._audit("denied", reason="capability_out_of_scope",
                        group_id=group_id, capability=capability)
            return False, "capability_out_of_scope"
        return True, "allowed"


class AuthorizationStore:
    """Persist authorizations as approval records (JSON) for audit."""

    def __init__(self, data_dir: str) -> None:
        self._dir = Path(data_dir) / "authorizations"
        self._dir.mkdir(parents=True, exist_ok=True)

    def save(self, authz: PilotAuthorization) -> Path:
        path = self._dir / f"{authz.authz_id}.json"
        payload = {
            "authz_id": authz.authz_id,
            "approval": authz.approval.to_dict(),
            "issued_at": authz._issued_at,  # noqa: SLF001 - audit surface
            "ttl_sec": authz._ttl_sec,  # noqa: SLF001
            "token_digest": authz._token_digest,  # noqa: SLF001 - digest only
            "revoked": authz.is_revoked(),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        return path

    def list_records(self) -> list[dict]:
        out = []
        for p in sorted(self._dir.glob("*.json")):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                continue
        return out

    def clear(self) -> int:
        n = 0
        for p in self._dir.glob("*.json"):
            try:
                p.unlink()
                n += 1
            except OSError:
                continue
        return n


class AuthorizationAuditLog:
    """P9.4 append-only JSONL audit of the authorization lifecycle.

    Every event is a JSON line: {event, ts, authz_id?, approval_id?,
    operator?, reason?, scope?}.  The log is append-only (never truncated by
    a later writer); ``clear()`` removes the trail on operator demand
    (privacy: records are deletable, P8.4).
    """

    def __init__(self, data_dir: str) -> None:
        self._dir = Path(data_dir) / "logs"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "authorization_audit.jsonl"

    def record(self, event: str, **fields) -> None:
        """Append one audit event (never overwrites existing records)."""
        entry = {"event": event, "ts": time.time()}
        entry.update({k: v for k, v in fields.items() if v not in (None, "")})
        try:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass  # auditing must never raise into the authz path

    def read(self) -> list[dict]:
        """All audit records, oldest first."""
        if not self._path.exists():
            return []
        out: list[dict] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def clear(self) -> int:
        """Delete the audit trail; returns the number of records removed."""
        if not self._path.exists():
            return 0
        n = len(self.read())
        try:
            self._path.unlink()
        except OSError:
            return 0
        return n
