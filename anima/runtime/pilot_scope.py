"""P7.14 — Controlled Pilot Implementation (P7.13 control model).

Implements the auditable, retractable, never-accidentally-rolled-out pilot
authorization model:

    PilotScope            - enabled / mode=CONTROLLED / goal_prefixes /
                            plan_prefixes / allowlist / operator_approval_ref
                            (+ additive scope_digest); wildcards are REJECTED
                            at construction (no global enable, no "*").
    CohortMatcher         - goal/plan PREFIX match + EXPLICIT allowlist match;
                            fail closed (empty prefixes / empty allowlist deny).
    PilotEnablementRecord - the approval record: pilot_enablement_id / operator /
                            scope_digest / approved_at.  This is a PILOT
                            authorization, NEVER an execution authorization —
                            it carries no intent_id / authz_id / execution_key /
                            evidence_id and never pollutes the F5 identity chain.
    pilot_gate()          - FAILED CLOSED: scope match AND approval binding AND
                            kill switch ENABLED AND provider available = allowed;
                            anything else is denied.

Scope limits (P7.14 authorization): no user traffic, no production rollout,
no capability activation, no System B / gateway / env override.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Optional

__all__ = [
    "PilotScope",
    "CohortMatcher",
    "PilotEnablementRecord",
    "pilot_gate",
    "PILOT_MODE_CONTROLLED",
]

#: the only pilot mode this model admits (never "PRODUCTION").
PILOT_MODE_CONTROLLED = "CONTROLLED"

#: wildcard token that must never appear (global enable is forbidden).
_WILDCARD = "*"


class PilotScopeError(ValueError):
    """Raised when a pilot scope is malformed (fail closed at construction)."""


def _scope_digest(
    scope_id: str,
    enabled: bool,
    mode: str,
    goal_prefixes: Iterable[str],
    plan_prefixes: Iterable[str],
    allowlist: Iterable[str],
    operator_approval_ref: str,
) -> str:
    payload = json.dumps(
        {
            "scope_id": scope_id,
            "enabled": enabled,
            "mode": mode,
            "goal_prefixes": sorted(goal_prefixes),
            "plan_prefixes": sorted(plan_prefixes),
            "allowlist": sorted(allowlist),
            "operator_approval_ref": operator_approval_ref,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PilotScope:
    """The auditable pilot scope (P7.13 C1). Wildcards rejected at construction.

    ``scope_digest`` is additive: when left empty it is computed from the scope
    fields; when provided it must match (a tampered scope fails closed).
    """

    scope_id: str
    enabled: bool
    mode: str
    goal_prefixes: tuple[str, ...]
    plan_prefixes: tuple[str, ...]
    allowlist: tuple[str, ...]
    operator_approval_ref: str
    scope_digest: str = ""

    def __post_init__(self) -> None:
        for name in ("scope_id", "mode", "operator_approval_ref"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise PilotScopeError(f"{name} must be a non-empty string")
        if self.mode != PILOT_MODE_CONTROLLED:
            raise PilotScopeError(
                f"pilot mode must be {PILOT_MODE_CONTROLLED!r} "
                f"(never PRODUCTION in this model)"
            )
        for field in ("goal_prefixes", "plan_prefixes", "allowlist"):
            value = getattr(self, field)
            if not isinstance(value, tuple):
                raise PilotScopeError(f"{field} must be a tuple")
            for item in value:
                if not isinstance(item, str) or not item:
                    raise PilotScopeError(f"{field} entries must be non-empty strings")
                if _WILDCARD in item:
                    raise PilotScopeError(
                        f"{field} must not contain the wildcard {_WILDCARD!r} "
                        f"(global enable is forbidden)"
                    )
        expected = _scope_digest(
            self.scope_id,
            self.enabled,
            self.mode,
            self.goal_prefixes,
            self.plan_prefixes,
            self.allowlist,
            self.operator_approval_ref,
        )
        if not self.scope_digest:
            object.__setattr__(self, "scope_digest", expected)
        elif self.scope_digest != expected:
            raise PilotScopeError("scope_digest does not match the scope fields")


class CohortMatcher:
    """PREFIX matching on goal/plan + EXPLICIT allowlist membership (fail closed).

    ``*`` is impossible by construction (PilotScope rejects it).  Empty prefixes
    or an empty allowlist deny everything — there is no default-open path.
    """

    def __init__(self, scope: PilotScope) -> None:
        self._scope = scope

    def match(self, goal_id: str, plan_id: str) -> bool:
        if not self._scope.enabled:
            return False
        if not self._scope.goal_prefixes or not self._scope.plan_prefixes:
            return False  # no prefix declared -> nothing can match
        if not self._scope.allowlist:
            return False  # empty allowlist -> deny all (fail closed)
        goal_ok = any(goal_id.startswith(p) for p in self._scope.goal_prefixes)
        plan_ok = any(plan_id.startswith(p) for p in self._scope.plan_prefixes)
        allow_ok = plan_id in self._scope.allowlist
        return goal_ok and plan_ok and allow_ok


@dataclass(frozen=True, slots=True)
class PilotEnablementRecord:
    """The approval record (P7.13 C5) — a PILOT authorization, never execution.

    Binds an operator approval to a concrete scope via ``scope_digest``.  It
    deliberately carries no intent_id / authz_id / execution_key / evidence_id —
    the F5 identity chain is never polluted by pilot enablement.
    """

    pilot_enablement_id: str
    operator: str
    scope_digest: str
    approved_at: str

    def __post_init__(self) -> None:
        for name in ("pilot_enablement_id", "operator", "scope_digest", "approved_at"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise PilotScopeError(f"{name} must be a non-empty string")
        if not (
            len(self.scope_digest) == 64
            and set(self.scope_digest) <= frozenset("0123456789abcdef")
        ):
            raise PilotScopeError("scope_digest must be a SHA-256 hex digest")


def pilot_gate(
    scope: Optional[PilotScope],
    record: Optional[PilotEnablementRecord],
    switch: Any,
    *,
    goal_id: str,
    plan_id: str,
    provider_available: bool = True,
) -> bool:
    """FAILED CLOSED pilot admission check.

    Allowed ONLY when: scope exists + enabled + cohort matches (goal/plan
    prefixes + allowlist) AND approval record bound (scope_digest matches) AND
    kill switch ENABLED AND provider available.  Anything else is denied.
    """
    if scope is None or record is None:
        return False
    if not scope.enabled:
        return False
    if record.scope_digest != scope.scope_digest:
        return False  # approval not bound to this scope
    if not CohortMatcher(scope).match(goal_id, plan_id):
        return False
    if not provider_available:
        return False
    try:
        switch.check()
    except Exception:  # noqa: BLE001 - any switch failure denies (fail closed)
        return False
    return True
