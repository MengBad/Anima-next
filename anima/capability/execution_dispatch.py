"""P6.10-B Authorization Dispatch Separation Slice.

Implements the three-way fact flow split (approve / dispatch / record) with
AuthorizationFact consumption and ExecutionFact recording — WITHOUT any
executor / provider invocation:

    approve()   -> AuthorizationFact          (intent to authorize; P6.10-A model)
    dispatch()  -> ExecutionFactDraft         (consume authz, construct execution_key)
    record()    -> ExecutionFact              (execution fact contract, immutable)

The flow is fact-flow only: dispatch() NEVER invokes an executor or provider;
it constructs the execution identity and evidence carrier.  Real execution
remains a future, separately-authorized slice.

Invariants held by construction and by test (P6.9 acceptance A-F):

  * A AUTHORIZATION ISOLATION - approve() produces an authorization fact ONLY;
                                no execution_facts domain is created
  * B DISPATCH FACT            - dispatch() produces execution evidence
                                (execution_key + evidence_id + result slot)
  * C EVIDENCE IMMUTABILITY    - recorded facts are frozen + digest-bound and
                                never overwritten
  * D REPLAY SAFETY            - the same authz_id is idempotent: one dispatch,
                                one fact, repeated verification
  * E CROSS-SINK CONSISTENCY   - authz fact carries no execution fields; the
                                execution fact links back via authz_id
  * F NON-ESCALATION           - neither fact ever upgrades to executed /
                                deployed / authorized_running / production

Boundary: NO executor / provider / workflow invocation, NO production wiring,
NO capability expansion, NO frozen/governance import (stdlib only).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from anima.capability.authorization_fact import (
    AuthorizationFact,
    AuthorizationFactReadError,
    AuthorizationFactStore,
)

__all__ = [
    "DispatchFlow",
    "ExecutionFact",
    "ExecutionFactDraft",
    "ExecutionFactReadError",
    "ExecutionFactVerification",
    "verify_execution_fact",
]

#: storage subdomain for execution facts (separate from authorization_facts).
_SUBDIR = "execution_facts"

#: the offline-source prefix contract.
_SOURCE_PREFIX = "local://execution_facts/"

#: System B execution_key shape: (request_id, capability_id, attempt).
_ATTEMPT = 0


class ExecutionFactReadError(ValueError):
    """Raised when an execution fact artifact is missing or corrupt (fail closed)."""


@dataclass(frozen=True, slots=True)
class ExecutionFactDraft:
    """Dispatch-time carrier: execution identity constructed WITHOUT executing.

    ``result`` is a slot only — it stays empty until a real executor (future
    slice) fills it.  The draft proves the dispatch contract, never the
    execution outcome.
    """

    execution_key: tuple
    evidence_id: str
    authz_id: str
    capability_id: str
    dispatched_at: str
    result: dict
    intent_id: str = ""


@dataclass(frozen=True, slots=True)
class ExecutionFact:
    """An immutable, digest-bound execution fact (recorded at dispatch time).

    Carries the execution identity (execution_key / evidence_id) and links back
    to its authorization via ``authz_id``.  ``result`` is the execution-outcome
    slot — empty until a real executor fills it; this slice never does.
    ``intent_id`` (P6.12) links back one more hop to the request intent; it is
    never an authorization or execution identifier.
    """

    execution_key: tuple
    evidence_id: str
    authz_id: str
    capability_id: str
    dispatched_at: str
    result: dict
    source: str
    fact_digest: str
    intent_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.execution_key, tuple) or len(self.execution_key) != 3:
            raise ExecutionFactReadError(
                "execution_key must be a (request_id, capability_id, attempt) tuple"
            )
        for name in (
            "evidence_id",
            "authz_id",
            "capability_id",
            "dispatched_at",
            "source",
            "fact_digest",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ExecutionFactReadError(f"{name} must be a non-empty string")
        if not isinstance(self.intent_id, str):
            raise ExecutionFactReadError("intent_id must be a string")
        if self.intent_id and self.intent_id == self.authz_id:
            raise ExecutionFactReadError(
                "intent_id must never equal authz_id (intent is not authorization)"
            )
        if not isinstance(self.result, dict):
            raise ExecutionFactReadError("result must be an object")
        if not self.source.startswith(_SOURCE_PREFIX):
            raise ExecutionFactReadError(
                "execution fact source must stay inside the offline-prefix contract"
            )
        if not (
            len(self.fact_digest) == 64
            and set(self.fact_digest) <= frozenset("0123456789abcdef")
        ):
            raise ExecutionFactReadError("fact_digest must be a SHA-256 hex digest")


@dataclass(frozen=True, slots=True)
class ExecutionFactVerification:
    """Integrity result of an execution fact (fails closed)."""

    valid: bool
    digest_intact: bool
    authz_bound: bool
    non_escalation: bool
    errors: tuple = ()


def _digest(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _payload(
    execution_key: tuple,
    evidence_id: str,
    authz_id: str,
    capability_id: str,
    dispatched_at: str,
    result: dict,
    source: str,
    intent_id: str = "",
) -> dict[str, Any]:
    return {
        "execution_key": list(execution_key),
        "evidence_id": evidence_id,
        "authz_id": authz_id,
        "capability_id": capability_id,
        "dispatched_at": dispatched_at,
        "result": result,
        "source": source,
        "intent_id": intent_id,
    }


def _validate(data: Any, path: Path) -> None:
    """Fail closed unless the artifact matches the execution_fact schema."""
    if not isinstance(data, dict):
        raise ExecutionFactReadError(
            f"execution fact corrupt: root is not an object: {path}"
        )
    if (
        not isinstance(data.get("execution_key"), list)
        or len(data["execution_key"]) != 3
        or not all(isinstance(v, str) for v in data["execution_key"][:2])
        or not isinstance(data["execution_key"][2], int)
    ):
        raise ExecutionFactReadError(
            f"execution fact corrupt: execution_key invalid: {path}"
        )
    for field in (
        "evidence_id",
        "authz_id",
        "capability_id",
        "dispatched_at",
        "source",
        "fact_digest",
    ):
        if not isinstance(data.get(field), str) or not data.get(field):
            raise ExecutionFactReadError(
                f"execution fact corrupt: {field!r} must be a non-empty string: {path}"
            )
    if data.get("intent_id") is not None and not isinstance(data["intent_id"], str):
        raise ExecutionFactReadError(
            f"execution fact corrupt: 'intent_id' must be a string: {path}"
        )
    if not isinstance(data.get("result"), dict):
        raise ExecutionFactReadError(
            f"execution fact corrupt: result must be an object: {path}"
        )
    if not data["source"].startswith(_SOURCE_PREFIX):
        raise ExecutionFactReadError(
            f"execution fact corrupt: source escapes offline contract: {path}"
        )


def verify_execution_fact(fact: ExecutionFact) -> ExecutionFactVerification:
    """Verify an execution fact (fails closed; recompute, never repair)."""
    errors: list[str] = []
    if not isinstance(fact, ExecutionFact):
        raise ExecutionFactReadError("verification requires an ExecutionFact")
    expected = _digest(
        _payload(
            execution_key=fact.execution_key,
            evidence_id=fact.evidence_id,
            authz_id=fact.authz_id,
            capability_id=fact.capability_id,
            dispatched_at=fact.dispatched_at,
            result=fact.result,
            source=fact.source,
        )
    )
    digest_intact = fact.fact_digest == expected
    if not digest_intact:
        errors.append("fact_digest does not match the fact fields")
    authz_bound = bool(fact.authz_id)
    if not authz_bound:
        errors.append("execution fact is not bound to an authorization")
    payload = " ".join(
        (fact.capability_id, fact.authz_id, fact.source)
    ).lower()
    non_escalation = all(
        banned not in payload
        for banned in ("executed", "deployed", "authorized_running", "production")
    )
    if not non_escalation:
        errors.append("execution fact escalates to execution vocabulary")
    valid = digest_intact and authz_bound and non_escalation and not errors
    return ExecutionFactVerification(
        valid=valid,
        digest_intact=digest_intact,
        authz_bound=authz_bound,
        non_escalation=non_escalation,
        errors=tuple(errors),
    )


class DispatchFlow:
    """Three-way fact flow: approve -> dispatch -> record (no execution).

    Public surface (all fact-flow only):

    * ``approve``          - record an authorization intent (AuthorizationFact)
    * ``dispatch``         - consume an authorization, construct the execution
                             identity (ExecutionFactDraft); idempotent per authz
    * ``record``           - persist the execution fact (immutable, replay-safe)
    * ``load_execution_fact`` / ``has_execution_fact`` - read / probe
    """

    def __init__(self, data_dir: str) -> None:
        self._dir = Path(data_dir)
        self._exec_dir = self._dir / _SUBDIR
        self._authz_store = AuthorizationFactStore(data_dir)

    # ---- approve: authorization intent only -----------------------------
    def approve(self, **kwargs) -> AuthorizationFact:
        """Record an authorization intent (P6.10-A model)."""
        return self._authz_store.record(**kwargs)

    # ---- dispatch: consume authorization, construct execution identity --
    def dispatch(self, authz_id: str) -> ExecutionFactDraft:
        """Consume an authorization and construct the execution identity.

        NEVER invokes an executor or provider.  Raises if the authorization
        does not exist (fail closed: no authorization -> no dispatch).
        """
        authz = self._authz_store.load(authz_id)
        if authz is None:
            raise AuthorizationFactReadError(
                f"dispatch requires an existing authorization: {authz_id!r}"
            )
        # execution_key: (request_id, capability_id, attempt) — System B shape,
        # request_id derived from the authorization id (identity continuity)
        execution_key = (authz_id, authz.capability_id, _ATTEMPT)
        # evidence_id is DETERMINISTICALLY derived from the dispatch identity:
        # the same authz_id dispatches to the same evidence (replay-safe), and
        # a second dispatch of the same authorization never creates a new fact.
        evidence_id = hashlib.sha256(
            f"exec-fact:{authz_id}:{authz.capability_id}:{_ATTEMPT}".encode("utf-8")
        ).hexdigest()[:32]
        return ExecutionFactDraft(
            execution_key=execution_key,
            evidence_id=evidence_id,
            authz_id=authz_id,
            capability_id=authz.capability_id,
            dispatched_at=authz.granted_at,
            result={},
            intent_id=authz.intent_id,
        )

    # ---- record: persist the execution fact (immutable, replay-safe) ----
    def record(self, draft: ExecutionFactDraft) -> ExecutionFact:
        """Persist the execution fact (atomic write; never overwritten)."""
        if not isinstance(draft, ExecutionFactDraft):
            raise ExecutionFactReadError("record requires an ExecutionFactDraft")
        source = f"{_SOURCE_PREFIX}{draft.evidence_id}.json"
        fact = ExecutionFact(
            execution_key=draft.execution_key,
            evidence_id=draft.evidence_id,
            authz_id=draft.authz_id,
            capability_id=draft.capability_id,
            dispatched_at=draft.dispatched_at,
            result=draft.result,
            source=source,
            intent_id=draft.intent_id,
            fact_digest=_digest(
                _payload(
                    execution_key=draft.execution_key,
                    evidence_id=draft.evidence_id,
                    authz_id=draft.authz_id,
                    capability_id=draft.capability_id,
                    dispatched_at=draft.dispatched_at,
                    result=draft.result,
                    source=source,
                    intent_id=draft.intent_id,
                )
            ),
        )
        path = self._exec_dir / f"{fact.evidence_id}.json"
        if path.is_file():
            return fact  # replay-safe: never overwritten
        self._exec_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "execution_key": list(fact.execution_key),
                "evidence_id": fact.evidence_id,
                "authz_id": fact.authz_id,
                "capability_id": fact.capability_id,
                "dispatched_at": fact.dispatched_at,
                "result": fact.result,
                "source": fact.source,
                "fact_digest": fact.fact_digest,
                "intent_id": fact.intent_id,
            },
            ensure_ascii=False,
        )
        fd, tmp = tempfile.mkstemp(dir=str(self._exec_dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return fact

    # ---- read / probe ---------------------------------------------------
    def load_execution_fact(self, evidence_id: str) -> Optional[ExecutionFact]:
        """Read one execution fact (None if absent; fail closed on corrupt)."""
        path = self._exec_dir / f"{evidence_id}.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ExecutionFactReadError(
                f"execution fact corrupt (invalid JSON): {path}: {exc}"
            ) from exc
        _validate(data, path)
        return ExecutionFact(
            execution_key=tuple(data["execution_key"]),
            evidence_id=data["evidence_id"],
            authz_id=data["authz_id"],
            capability_id=data["capability_id"],
            dispatched_at=data["dispatched_at"],
            result=data["result"],
            source=data["source"],
            fact_digest=data["fact_digest"],
            intent_id=data.get("intent_id", "") or "",
        )

    def has_execution_fact(self, evidence_id: str) -> bool:
        """Whether an execution fact is already recorded (idempotency probe)."""
        return (self._exec_dir / f"{evidence_id}.json").is_file()
