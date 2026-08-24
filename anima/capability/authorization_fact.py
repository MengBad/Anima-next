"""P6.10-A Authorization Fact Recording Slice.

Establishes an independent ``AuthorizationFact`` data structure and its
verification contract — the isolation layer between "authorization to
execute" (authorization fact) and "execution that happened" (execution fact /
HealthEvidence / ExecutionLedger).

Invariants held by construction and by test:

  * FACT ONLY         - AuthorizationFact records the *intent to authorize*; it
                        carries NO execution status, NO result, NO success.
                        approved != executed, authorized != running,
                        resolution != authorization, authorization fact !=
                        execution fact.
  * NO EXECUTION      - this slice never calls an executor, never invokes a
                        provider, never dispatches a capability, never touches
                        approve_and_execute, never writes an execution ledger.
  * IMMUTABLE         - AuthorizationFact is frozen + slots + digest-bound;
                        stored facts are never overwritten (replay-safe).
  * ATOMIC STORAGE    - facts persist under ``authorization_facts/<id>.json``
                        via tempfile + os.replace (stdlib only).
  * NO FROZEN IMPORT  - this module imports NO anima.runtime / anima.constitution /
                        anima.core / anima.tools / anima.capability.governance.
  * NON-ESCALATION    - an authorization fact never upgrades to executed /
                        deployed / running / production semantics; the payload
                        carries none of that vocabulary.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Optional

__all__ = [
    "AuthorizationFact",
    "AuthorizationFactReadError",
    "AuthorizationFactStore",
    "AuthorizationFactVerification",
    "verify_authorization_fact",
]

#: storage subdomain for authorization facts (separate from execution ledger).
_SUBDIR = "authorization_facts"

#: the offline-source prefix contract (local files are offline sources).
_SOURCE_PREFIX = "local://authorization_facts/"


class AuthorizationFactReadError(ValueError):
    """Raised when an authorization fact artifact is missing or corrupt (fail closed)."""


@dataclass(frozen=True, slots=True)
class AuthorizationFact:
    """An immutable, digest-bound record of one authorization intent.

    Deliberately carries NO execution semantics: no execution_status, no
    result, no success.  It answers "who authorized what capability, when" —
    never "what happened when it executed".
    """

    authz_id: str
    principal: str
    capability_id: str
    scope: str
    granted_at: str
    source: str
    authz_digest: str
    #: P6.12: optional upstream intent binding ("" = no intent link). The
    #: intent_id is a *link back* to the request intent; it is never an
    #: authorization identifier (intent_id != authz_id).
    intent_id: str = ""

    def __post_init__(self) -> None:
        for name in (
            "authz_id",
            "principal",
            "capability_id",
            "scope",
            "granted_at",
            "source",
            "authz_digest",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise AuthorizationFactReadError(f"{name} must be a non-empty string")
        if not isinstance(self.intent_id, str):
            raise AuthorizationFactReadError("intent_id must be a string")
        if self.intent_id and self.intent_id == self.authz_id:
            raise AuthorizationFactReadError(
                "intent_id must never equal authz_id (intent is not authorization)"
            )
        if not self.source.startswith(_SOURCE_PREFIX):
            raise AuthorizationFactReadError(
                "authorization fact source must stay inside the offline-prefix contract"
            )
        if not (
            len(self.authz_digest) == 64
            and set(self.authz_digest) <= frozenset("0123456789abcdef")
        ):
            raise AuthorizationFactReadError(
                "authz_digest must be a SHA-256 hex digest"
            )
        # non-escalation guard: the fact vocabulary must not carry execution terms
        payload = " ".join(
            (self.capability_id, self.scope, self.principal)
        ).lower()
        for banned in ("executed", "deployed", "authorized_running", "production"):
            if banned in payload:
                raise AuthorizationFactReadError(
                    f"authorization fact must not escalate to execution semantics "
                    f"(found {banned!r})"
                )


@dataclass(frozen=True, slots=True)
class AuthorizationFactVerification:
    """Integrity result of an authorization fact (fails closed)."""

    valid: bool
    digest_intact: bool
    non_escalation: bool
    errors: tuple = ()


def _digest(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _payload(fact: AuthorizationFact) -> dict[str, Any]:
    return {
        "authz_id": fact.authz_id,
        "principal": fact.principal,
        "capability_id": fact.capability_id,
        "scope": fact.scope,
        "granted_at": fact.granted_at,
        "source": fact.source,
        "intent_id": fact.intent_id,
    }


def _validate(data: Any, path: Path) -> None:
    """Fail closed unless the artifact matches the authorization_fact schema."""
    if not isinstance(data, dict):
        raise AuthorizationFactReadError(
            f"authorization fact corrupt: root is not an object: {path}"
        )
    for field in (
        "authz_id",
        "principal",
        "capability_id",
        "scope",
        "granted_at",
        "source",
        "authz_digest",
    ):
        if not isinstance(data.get(field), str) or not data.get(field):
            raise AuthorizationFactReadError(
                f"authorization fact corrupt: {field!r} must be a non-empty string: {path}"
            )
    if data.get("intent_id") is not None and not isinstance(data["intent_id"], str):
        raise AuthorizationFactReadError(
            f"authorization fact corrupt: 'intent_id' must be a string: {path}"
        )
    if not (
        len(data["authz_digest"]) == 64
        and set(data["authz_digest"]) <= frozenset("0123456789abcdef")
    ):
        raise AuthorizationFactReadError(
            f"authorization fact corrupt: authz_digest invalid: {path}"
        )
    if not data["source"].startswith(_SOURCE_PREFIX):
        raise AuthorizationFactReadError(
            f"authorization fact corrupt: source escapes offline contract: {path}"
        )


class AuthorizationFactStore:
    """Read-only-plus-record store for authorization facts (replay-safe).

    Public surface (exactly three operations):

    * ``record`` - persist a NEW authorization fact; a duplicate authz_id is a
                   no-op (the stored fact is never overwritten)
    * ``load``   - read one fact by id (None if absent; raises on corrupt)
    * ``has``    - idempotency probe
    """

    def __init__(self, data_dir: str) -> None:
        self._dir = Path(data_dir)
        self._facts_dir = self._dir / _SUBDIR

    def _path(self, authz_id: str) -> Path:
        return self._facts_dir / f"{authz_id}.json"

    def record(
        self,
        *,
        authz_id: str,
        principal: str,
        capability_id: str,
        scope: str,
        granted_at: str,
        source: Optional[str] = None,
        intent_id: Optional[str] = None,
    ) -> AuthorizationFact:
        """Record an authorization intent fact (replay-safe, atomic write)."""
        source = source or f"{_SOURCE_PREFIX}{authz_id}.json"
        intent_id = intent_id or ""
        fact = AuthorizationFact(
            authz_id=authz_id,
            principal=principal,
            capability_id=capability_id,
            scope=scope,
            granted_at=granted_at,
            source=source,
            intent_id=intent_id,
            authz_digest=_digest(
                {
                    "authz_id": authz_id,
                    "principal": principal,
                    "capability_id": capability_id,
                    "scope": scope,
                    "granted_at": granted_at,
                    "source": source,
                    "intent_id": intent_id,
                }
            ),
        )
        path = self._path(authz_id)
        if path.is_file():
            # replay-safe: a duplicate authz_id is a no-op, never overwritten
            return fact
        self._facts_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "authz_id": fact.authz_id,
                "principal": fact.principal,
                "capability_id": fact.capability_id,
                "scope": fact.scope,
                "granted_at": fact.granted_at,
                "source": fact.source,
                "authz_digest": fact.authz_digest,
                "intent_id": fact.intent_id,
            },
            ensure_ascii=False,
        )
        # atomic write: tempfile in the same directory + os.replace
        fd, tmp = tempfile.mkstemp(dir=str(self._facts_dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return fact

    def load(self, authz_id: str) -> Optional[AuthorizationFact]:
        """Read one authorization fact (None if absent; fail closed on corrupt)."""
        path = self._path(authz_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AuthorizationFactReadError(
                f"authorization fact corrupt (invalid JSON): {path}: {exc}"
            ) from exc
        _validate(data, path)
        return AuthorizationFact(
            authz_id=data["authz_id"],
            principal=data["principal"],
            capability_id=data["capability_id"],
            scope=data["scope"],
            granted_at=data["granted_at"],
            source=data["source"],
            authz_digest=data["authz_digest"],
            intent_id=data.get("intent_id", "") or "",
        )

    def has(self, authz_id: str) -> bool:
        """Whether an authorization fact is already recorded (idempotency probe)."""
        return self._path(authz_id).is_file()


def verify_authorization_fact(
    fact: AuthorizationFact,
) -> AuthorizationFactVerification:
    """Verify an authorization fact (fails closed; recompute, never repair)."""
    errors: list[str] = []
    if not isinstance(fact, AuthorizationFact):
        raise AuthorizationFactReadError(
            "verification requires an AuthorizationFact"
        )
    expected = _digest(_payload(fact))
    digest_intact = fact.authz_digest == expected
    if not digest_intact:
        errors.append("authz_digest does not match the fact fields")
    # non-escalation: the fact's semantic vocabulary must stay authorization-only
    payload = " ".join(
        (fact.capability_id, fact.scope, fact.principal)
    ).lower()
    non_escalation = all(
        banned not in payload
        for banned in ("executed", "deployed", "authorized_running", "production")
    )
    if not non_escalation:
        errors.append("authorization fact escalates to execution semantics")
    valid = digest_intact and non_escalation and not errors
    return AuthorizationFactVerification(
        valid=valid,
        digest_intact=digest_intact,
        non_escalation=non_escalation,
        errors=tuple(errors),
    )
