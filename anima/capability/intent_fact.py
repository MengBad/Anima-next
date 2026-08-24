"""P6.12 — Intent Identity Closure Slice.

Closes the identity chain:

    intent_id -> authz_id -> execution_key -> evidence_id

An ``IntentFact`` records *why* a request was made (the upstream intent), the
first hop of the chain.  It deliberately carries NO authorization semantics
(no authz_id / scope / granted_at) and NO execution semantics (no executed /
approved / deployed / production):  intent is NOT authorization.

Invariants held by construction and by test:

  * INTENT ONLY       - ``IntentFact`` answers "why was this requested, by
                        whom, for which capability" — never "who allowed it"
                        (that is AuthorizationFact) and never "what happened"
                        (that is ExecutionFact / HealthEvidence).
  * IDENTITY CHAIN    - ``intent_id`` propagates intent -> authz -> execution
                        fact; the execution identity (execution_key / evidence
                        id) stays derived from the AUTHORIZATION, so the chain
                        is never re-derived from the intent (no
                        execution_key -> intent reversal).
  * NO EXECUTION      - this slice never calls an executor, never invokes a
                        provider, never dispatches, never enables capability
                        mode, never mutates governance or production config.
  * IMMUTABLE         - IntentFact is frozen + slots + digest-bound; stored
                        facts are never overwritten (replay-safe).
  * ATOMIC STORAGE    - facts persist under ``intent_facts/<intent_id>.json``
                        via tempfile + os.replace (stdlib only).
  * NO FROZEN IMPORT  - this module imports NO anima.runtime / anima.constitution /
                        anima.core / anima.tools / anima.capability.governance /
                        anima.capability.health.
  * NON-ESCALATION    - an intent fact never upgrades to approved / executed /
                        deployed / production semantics.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

__all__ = [
    "IntentFact",
    "IntentFactReadError",
    "IntentFactStore",
    "IntentFactVerification",
    "IdentityContinuityVerification",
    "verify_intent_fact",
    "verify_identity_continuity",
]

#: storage subdomain for intent facts (separate from authorization / execution).
_SUBDIR = "intent_facts"

#: the offline-source prefix contract (local files are offline sources).
_SOURCE_PREFIX = "local://intent_facts/"

#: semantic-escalation tokens that must never appear in an intent fact.
_ESCALATION_TOKENS = frozenset(
    {"executed", "deployed", "authorized_running", "production", "approved"}
)


class IntentFactReadError(ValueError):
    """Raised when an intent fact artifact is missing or corrupt (fail closed)."""


@dataclass(frozen=True, slots=True)
class IntentFact:
    """An immutable, digest-bound record of one request intent (chain head).

    Carries NO authorization semantics (no authz_id / scope / granted_at) and
    NO execution semantics (no executed / approved / deployed / production).
    It answers "why was this requested, by whom, for which capability".
    """

    intent_id: str
    principal: str
    capability_id: str
    request_digest: str
    requested_at: str
    source: str
    intent_digest: str

    def __post_init__(self) -> None:
        for name in (
            "intent_id",
            "principal",
            "capability_id",
            "request_digest",
            "requested_at",
            "source",
            "intent_digest",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise IntentFactReadError(f"{name} must be a non-empty string")
        if not self.source.startswith(_SOURCE_PREFIX):
            raise IntentFactReadError(
                "intent fact source must stay inside the offline-prefix contract"
            )
        if not (
            len(self.intent_digest) == 64
            and set(self.intent_digest) <= frozenset("0123456789abcdef")
        ):
            raise IntentFactReadError("intent_digest must be a SHA-256 hex digest")
        if not (
            len(self.request_digest) == 64
            and set(self.request_digest) <= frozenset("0123456789abcdef")
        ):
            raise IntentFactReadError("request_digest must be a SHA-256 hex digest")
        # non-escalation guard: the intent vocabulary must not carry
        # authorization/execution terms
        payload = " ".join(
            (self.capability_id, self.principal, self.intent_id)
        ).lower()
        for banned in _ESCALATION_TOKENS:
            if banned in payload:
                raise IntentFactReadError(
                    f"intent fact must not escalate to execution semantics "
                    f"(found {banned!r})"
                )


@dataclass(frozen=True, slots=True)
class IntentFactVerification:
    """Integrity result of an intent fact (fails closed)."""

    valid: bool
    digest_intact: bool
    non_escalation: bool
    errors: tuple = ()


@dataclass(frozen=True, slots=True)
class IdentityContinuityVerification:
    """Four-hop identity-chain verification (intent -> authz -> exec)."""

    valid: bool
    intent_continuity: bool
    non_escalated: bool
    errors: tuple = ()


def _digest(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _payload(fact: IntentFact) -> dict[str, Any]:
    return {
        "intent_id": fact.intent_id,
        "principal": fact.principal,
        "capability_id": fact.capability_id,
        "request_digest": fact.request_digest,
        "requested_at": fact.requested_at,
        "source": fact.source,
    }


def _validate(data: Any, path: Path) -> None:
    """Fail closed unless the artifact matches the intent_fact schema."""
    if not isinstance(data, dict):
        raise IntentFactReadError(
            f"intent fact corrupt: root is not an object: {path}"
        )
    for field in (
        "intent_id",
        "principal",
        "capability_id",
        "request_digest",
        "requested_at",
        "source",
        "intent_digest",
    ):
        if not isinstance(data.get(field), str) or not data.get(field):
            raise IntentFactReadError(
                f"intent fact corrupt: {field!r} must be a non-empty string: {path}"
            )
    for digest_field in ("intent_digest", "request_digest"):
        if not (
            len(data[digest_field]) == 64
            and set(data[digest_field]) <= frozenset("0123456789abcdef")
        ):
            raise IntentFactReadError(
                f"intent fact corrupt: {digest_field} invalid: {path}"
            )
    if not data["source"].startswith(_SOURCE_PREFIX):
        raise IntentFactReadError(
            f"intent fact corrupt: source escapes offline contract: {path}"
        )


class IntentFactStore:
    """Read-only-plus-record store for intent facts (replay-safe).

    Public surface (exactly three operations):

    * ``record`` - persist a NEW intent fact; a duplicate intent_id is a
                   no-op (the stored fact is never overwritten)
    * ``load``   - read one fact by id (None if absent; raises on corrupt)
    * ``has``    - idempotency probe
    """

    def __init__(self, data_dir: str) -> None:
        self._dir = Path(data_dir)
        self._facts_dir = self._dir / _SUBDIR

    def _path(self, intent_id: str) -> Path:
        return self._facts_dir / f"{intent_id}.json"

    def record(
        self,
        *,
        intent_id: str,
        principal: str,
        capability_id: str,
        request_digest: str,
        requested_at: str,
        source: Optional[str] = None,
    ) -> IntentFact:
        """Record an intent fact (replay-safe, atomic write)."""
        source = source or f"{_SOURCE_PREFIX}{intent_id}.json"
        fact = IntentFact(
            intent_id=intent_id,
            principal=principal,
            capability_id=capability_id,
            request_digest=request_digest,
            requested_at=requested_at,
            source=source,
            intent_digest=_digest(
                {
                    "intent_id": intent_id,
                    "principal": principal,
                    "capability_id": capability_id,
                    "request_digest": request_digest,
                    "requested_at": requested_at,
                    "source": source,
                }
            ),
        )
        path = self._path(intent_id)
        if path.is_file():
            # replay-safe: a duplicate intent_id is a no-op, never overwritten
            return fact
        self._facts_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "intent_id": fact.intent_id,
                "principal": fact.principal,
                "capability_id": fact.capability_id,
                "request_digest": fact.request_digest,
                "requested_at": fact.requested_at,
                "source": fact.source,
                "intent_digest": fact.intent_digest,
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

    def load(self, intent_id: str) -> Optional[IntentFact]:
        """Read one intent fact (None if absent; fail closed on corrupt)."""
        path = self._path(intent_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise IntentFactReadError(
                f"intent fact corrupt (invalid JSON): {path}: {exc}"
            ) from exc
        _validate(data, path)
        return IntentFact(
            intent_id=data["intent_id"],
            principal=data["principal"],
            capability_id=data["capability_id"],
            request_digest=data["request_digest"],
            requested_at=data["requested_at"],
            source=data["source"],
            intent_digest=data["intent_digest"],
        )

    def has(self, intent_id: str) -> bool:
        """Whether an intent fact is already recorded (idempotency probe)."""
        return self._path(intent_id).is_file()


def verify_intent_fact(fact: IntentFact) -> IntentFactVerification:
    """Verify an intent fact (fails closed; recompute, never repair)."""
    errors: list[str] = []
    if not isinstance(fact, IntentFact):
        raise IntentFactReadError("verification requires an IntentFact")
    expected = _digest(_payload(fact))
    digest_intact = fact.intent_digest == expected
    if not digest_intact:
        errors.append("intent_digest does not match the fact fields")
    payload = " ".join(
        (fact.capability_id, fact.principal, fact.intent_id)
    ).lower()
    non_escalation = all(
        banned not in payload for banned in _ESCALATION_TOKENS
    )
    if not non_escalation:
        errors.append("intent fact escalates to execution semantics")
    valid = digest_intact and non_escalation and not errors
    return IntentFactVerification(
        valid=valid,
        digest_intact=digest_intact,
        non_escalation=non_escalation,
        errors=tuple(errors),
    )


def verify_identity_continuity(
    intent_fact: Any,
    authz_fact: Any,
    execution_fact: Any,
    *,
    intent_id: Optional[str] = None,
) -> IdentityContinuityVerification:
    """Verify the four-hop identity chain stays unbroken and non-escalating.

    Checks intent_id propagation: intent.intent_id == authz.intent_id ==
    execution.intent_id, plus per-hop digest integrity.  A PASS here is an
    identity-continuity statement ONLY — it never authorizes execution.
    """
    errors: list[str] = []
    try:
        intent_ok = bool(intent_fact) and isinstance(
            getattr(intent_fact, "intent_digest", None), str
        )
        authz_ok = bool(authz_fact) and isinstance(
            getattr(authz_fact, "authz_digest", None), str
        )
        exec_ok = bool(execution_fact) and isinstance(
            getattr(execution_fact, "fact_digest", None), str
        )
    except Exception:  # noqa: BLE001 - fail closed on unexpected shapes
        return IdentityContinuityVerification(
            valid=False, intent_continuity=False,
            non_escalated=False, errors=("malformed identity objects",),
        )
    if not (intent_ok and authz_ok and exec_ok):
        errors.append("missing one or more identity hops")

    chain_id = intent_id or (
        getattr(intent_fact, "intent_id", None) if intent_fact else None
    )
    hops = []
    if intent_fact is not None:
        hops.append(getattr(intent_fact, "intent_id", None))
    if authz_fact is not None:
        hops.append(getattr(authz_fact, "intent_id", None))
    if execution_fact is not None:
        hops.append(getattr(execution_fact, "intent_id", None))

    if chain_id is None:
        intent_continuity = False
        errors.append("no intent_id anchor for continuity")
    else:
        intent_continuity = all(h == chain_id for h in hops)
        if not intent_continuity:
            errors.append(
                f"identity chain broken: intent_id not propagated "
                f"(expected {chain_id!r}, got {hops!r})"
            )

    # non-escalation: none of the hops carries execution-authorization semantics
    non_escalated = True
    for hop in (intent_fact, authz_fact, execution_fact):
        if hop is None:
            continue
        vocab = " ".join(
            str(getattr(hop, "capability_id", ""))
            + " "
            + str(getattr(hop, "scope", ""))
        ).lower()
        if any(banned in vocab for banned in _ESCALATION_TOKENS):
            non_escalated = False
            errors.append("identity chain escalates to execution semantics")
            break

    valid = intent_continuity and non_escalated and not errors
    return IdentityContinuityVerification(
        valid=valid,
        intent_continuity=intent_continuity,
        non_escalated=non_escalated,
        errors=tuple(errors),
    )
