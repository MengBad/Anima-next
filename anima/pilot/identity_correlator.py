"""P8.3 — Identity Correlation (QQ identity -> F5 execution identity).

Real QQ identity binds to the execution chain WITHOUT ever merging:

    qq_group_id
        |
    qq_user_id
        |
    request_id          (fresh per admitted message, seeded by the Entry Gate)
        |
    intent_id           (derived: one intent per admitted request)
        |
    authz_id            (pilot-scope authorization binding, NEVER the qq id)
        |
    execution_key       (F5 triple (run_id, task_id, attempt) — F5-domain)
        |
    evidence_id         (deterministic sha256 of the execution_key)

Hard rules (P8.0):
    * qq identity != execution identity — qq_user_id is NEVER used as authz_id
      or as an execution_key component
    * authz_id is a pilot-scope binding (pilot_enablement_id-based), NOT the
      QQ user id; execution stays F5-autonomous
    * the correlation is a READ-ONLY map: it records the chain for evidence
      and audit, it never grants anything by itself

Scope limits (P8.0): no System B, no gateway, no capability activation, no
key merge, no authz injection into F5 keys, no env bypass.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from typing import Optional

__all__ = ["IdentityCorrelator", "IdentityChain"]


def _hash_qq_user(user_id: str) -> str:
    """Salt-less one-way digest of a QQ user id (evidence/audit, not security).

    Used ONLY where a stable per-user tag is needed without storing the raw
    QQ id in conversation logs (P8.4 privacy).  Not a credential.
    """
    return hashlib.sha256(f"qq-user:{user_id}".encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class IdentityChain:
    """The full correlation chain for ONE admitted request (read-only fact)."""

    qq_group_id: str
    qq_user_id: str
    qq_user_hash: str
    request_id: str
    intent_id: str
    authz_id: str
    execution_key: tuple
    evidence_id: str

    def to_dict(self) -> dict:
        return {
            "qq_group_id": self.qq_group_id,
            "qq_user_hash": self.qq_user_hash,   # privacy: hashed, never raw
            "request_id": self.request_id,
            "intent_id": self.intent_id,
            "authz_id": self.authz_id,
            "execution_key": list(self.execution_key),
            "evidence_id": self.evidence_id,
        }


class IdentityCorrelator:
    """Build the QQ -> F5 identity chain for one admitted request.

    ``authz_id`` derives from the pilot enablement scope (the approval that
    authorized this pilot), never from the QQ user id.  ``execution_key`` is
    the F5 triple (run_id, task_id, attempt); the run_id embeds the request_id
    so evidence stays traceable, but the key shape stays F5-domain.
    """

    def __init__(self, pilot_enablement_id: str = "pilot-live-001") -> None:
        self._pe_id = pilot_enablement_id

    def bind(
        self,
        *,
        group_id: str,
        user_id: str,
        request_id: str,
        task_id: Optional[str] = None,
        attempt: int = 0,
    ) -> IdentityChain:
        intent_id = f"intent-{request_id}"
        # authz = pilot-scope binding, NEVER the qq user id
        authz_id = hashlib.sha256(
            f"{self._pe_id}:{group_id}".encode("utf-8")
        ).hexdigest()[:24]
        run_id = f"run-{request_id}"
        tid = task_id or f"task-{request_id}"
        execution_key = (run_id, tid, attempt)
        evidence_id = hashlib.sha256(
            repr(execution_key).encode("utf-8")
        ).hexdigest()
        return IdentityChain(
            qq_group_id=str(group_id),
            qq_user_id=str(user_id),
            qq_user_hash=_hash_qq_user(str(user_id)),
            request_id=request_id,
            intent_id=intent_id,
            authz_id=authz_id,
            execution_key=execution_key,
            evidence_id=evidence_id,
        )


def new_request_id() -> str:
    """A fresh request id (used when the gate did not seed one)."""
    return f"req-{secrets.token_hex(8)}"
