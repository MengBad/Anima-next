"""P5.2 Runtime Consumption Bridge / Read-only Adapter.

This module is the second - and equally thin - bridge between the frozen P3/P4
governance evidence and a runtime consumer.  It is the mirror image of P5.1:

  * P5.1 observes governance evidence and produces an immutable, offline
    ``ObservedRuntimeState``.
  * P5.2 *consumes* that already-observed state into an immutable, digest-bound
    ``ConsumedRuntimeState`` - and proves the consumption stayed inside the
    read-only boundary.

This module is:

  * READ-ONLY        - it never writes, never stores, never repairs
  * CONSUMPTION-ONLY - it normalizes an observed state into a consumption view
  * NON-ACTIVATOR    - there is no activate / execute / apply / mutate entry point
  * OFFLINE-ONLY     - the observed state must come from a synthetic, fixture,
                       recorded, local or mock source

Single direction of information flow (never reversed)::

    Observed Runtime State  ->  Consumed Runtime State  ->  Consumption Verification
      (P5.1, offline)             (normalized, frozen)        (recomputed, fails closed)

What this module explicitly does NOT do
---------------------------------------
* It does not re-decide governance.  A governance claim is consumed as
  immutable evidence; it is never re-judged.
* It does not upgrade a claim.  ``not_activated`` is reported as
  ``not_activated``; it is never reported as ``active``, ``executed``,
  ``deployed`` or ``running``.
* It does not repair drift.  When the consumed state contradicts the observed
  state, the contradiction is *reported*; nothing is changed on either side.
* It never reaches production or any remote host.  There is no network, no
  process spawn, no filesystem mutation in this module.

Digest convention (identical to P3/P4/P5.1)::

    sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")))

The payload mirror below intentionally duplicates the upstream payload shape so
a digest can be recomputed without importing private helpers.  It is pinned by
``tests/test_capability_runtime_consumption.py`` - if the convention ever
drifts, the verification tests fail loudly rather than silently accepting a
stale mirror.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from anima.capability_runtime_observation import (
    NOT_ACTIVATED,
    NOT_APPLICABLE,
    NOT_EXECUTED,
    OBSERVATION_SCOPE,
    OFFLINE_SOURCE_PREFIXES,
    ObservedRuntimeState,
)

__all__ = [
    "ConsumedRuntimeState",
    "RuntimeConsumptionAdapter",
    "RuntimeConsumptionError",
    "RuntimeConsumptionReport",
    "RuntimeConsumptionVerification",
    "UNKNOWN",
]

UNKNOWN = "unknown"


class RuntimeConsumptionError(ValueError):
    """Raised when consumption input violates the read-only boundary."""


def _digest(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


_HEX = frozenset("0123456789abcdef")


def _is_hex64(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeConsumptionError(f"{field_name} must be a non-empty string")
    return value


# ---------------------------------------------------------------------------
# data model (all frozen + slots + digest-bound, fails closed on construction)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ConsumedRuntimeState:
    """An immutable, digest-bound, read-only view of one observed runtime state.

    The consumption reports *what the observed state says* - faithfully.  It
    never sanitizes, upgrades or downgrades a claim; verification is what fails
    closed.
    """

    consumption_id: str
    subject_id: str
    runtime_activation: str
    execution_status: str
    observed_at: str
    source: str
    consumed_at: str
    observation_scope: str
    consumption_digest: str

    def __post_init__(self) -> None:
        for field in (
            "consumption_id",
            "subject_id",
            "runtime_activation",
            "execution_status",
            "observed_at",
            "source",
            "consumed_at",
            "observation_scope",
            "consumption_digest",
        ):
            _text(getattr(self, field), field)
        if not self.source.startswith(OFFLINE_SOURCE_PREFIXES):
            raise RuntimeConsumptionError(
                "consumed runtime state must come from an offline source "
                f"({'|'.join(OFFLINE_SOURCE_PREFIXES)}); got {self.source!r}"
            )
        if not _is_hex64(self.consumption_digest):
            raise RuntimeConsumptionError(
                "consumption_digest must be a SHA-256 hex digest"
            )
        if self.observation_scope != OBSERVATION_SCOPE:
            raise RuntimeConsumptionError(
                f"observation_scope must remain {OBSERVATION_SCOPE!r}; "
                f"got {self.observation_scope!r}"
            )


@dataclass(frozen=True, slots=True)
class RuntimeConsumptionVerification:
    """Proof object returned by :meth:`RuntimeConsumptionAdapter.verify`.

    ``valid`` describes the *integrity of the consumption itself*.  Each flag is
    independently reported so a single breach is never silently absorbed.
    """

    valid: bool
    digest_intact: bool
    scope_intact: bool
    no_mutation: bool
    no_execution: bool
    source_offline: bool
    errors: tuple = ()


@dataclass(frozen=True, slots=True)
class RuntimeConsumptionReport:
    """A report - not a remediation - of consumption / observation disagreement."""

    consumption_id: str
    reconciled: bool
    contradictions: tuple
    consumed_claim: str
    observed_claim: str
    reported_at: str
    report_digest: str


# ---------------------------------------------------------------------------
# consumption payload mirror
# ---------------------------------------------------------------------------
def _consumed_payload(fields: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "consumption_id": fields["consumption_id"],
        "subject_id": fields["subject_id"],
        "runtime_activation": fields["runtime_activation"],
        "execution_status": fields["execution_status"],
        "observed_at": fields["observed_at"],
        "source": fields["source"],
        "consumed_at": fields["consumed_at"],
        "observation_scope": fields["observation_scope"],
    }


# ---------------------------------------------------------------------------
# the adapter (read-only surface only)
# ---------------------------------------------------------------------------
class RuntimeConsumptionAdapter:
    """Read-only bridge from an observed runtime state to a consumption view.

    The public surface is intentionally limited to four side-effect-free
    operations: ``consume``, ``inspect``, ``verify`` and ``reconcile``.  No
    operation writes, stores, dispatches, or reaches outside the process.
    """

    def consume(
        self,
        state: ObservedRuntimeState,
        *,
        consumption_id: str,
        consumed_at: str,
    ) -> ConsumedRuntimeState:
        """Normalize an observed runtime state into a frozen consumption view."""
        _text(consumption_id, "consumption_id")
        _text(consumed_at, "consumed_at")
        if not isinstance(state, ObservedRuntimeState):
            raise RuntimeConsumptionError(
                "consume accepts an ObservedRuntimeState only"
            )
        # re-validate the offline boundary (defense in depth, fails closed)
        if not state.source.startswith(OFFLINE_SOURCE_PREFIXES):
            raise RuntimeConsumptionError(
                "observed runtime state must come from an offline source"
            )
        fields = {
            "consumption_id": consumption_id,
            "subject_id": state.subject_id,
            "runtime_activation": state.runtime_activation,
            "execution_status": state.execution_status,
            "observed_at": state.observed_at,
            "source": state.source,
            "consumed_at": consumed_at,
            "observation_scope": OBSERVATION_SCOPE,
        }
        return ConsumedRuntimeState(
            consumption_digest=_digest(_consumed_payload(fields)), **fields
        )

    def inspect(self, consumed: ConsumedRuntimeState) -> Mapping[str, Any]:
        """Return an immutable mapping view of a consumed state."""
        if not isinstance(consumed, ConsumedRuntimeState):
            raise RuntimeConsumptionError(
                "inspect accepts a ConsumedRuntimeState only"
            )
        return MappingProxyType(
            {
                "consumption_id": consumed.consumption_id,
                "subject_id": consumed.subject_id,
                "runtime_activation": consumed.runtime_activation,
                "execution_status": consumed.execution_status,
                "observed_at": consumed.observed_at,
                "source": consumed.source,
                "consumed_at": consumed.consumed_at,
                "observation_scope": consumed.observation_scope,
                "consumption_digest": consumed.consumption_digest,
            }
        )

    def verify(
        self,
        consumed: ConsumedRuntimeState,
        original: ObservedRuntimeState,
    ) -> RuntimeConsumptionVerification:
        """Verify a consumption against its observed state (fails closed).

        Proves that the consumption is a faithful, unmodified, digest-bound view
        of the observed state, that it stays inside the synthetic-only scope,
        that consuming changed nothing, and that no execution is claimed.
        """
        errors: list[str] = []

        if not isinstance(consumed, ConsumedRuntimeState):
            raise RuntimeConsumptionError("verification requires a ConsumedRuntimeState")
        if not isinstance(original, ObservedRuntimeState):
            raise RuntimeConsumptionError("verification requires an ObservedRuntimeState")

        # --- digest integrity: the consumption still hashes to its own fields ---
        expected_digest = _digest(
            _consumed_payload(
                {
                    "consumption_id": consumed.consumption_id,
                    "subject_id": consumed.subject_id,
                    "runtime_activation": consumed.runtime_activation,
                    "execution_status": consumed.execution_status,
                    "observed_at": consumed.observed_at,
                    "source": consumed.source,
                    "consumed_at": consumed.consumed_at,
                    "observation_scope": consumed.observation_scope,
                }
            )
        )
        digest_intact = consumed.consumption_digest == expected_digest
        if not digest_intact:
            errors.append("consumption digest does not match its recomputed fields")

        # --- scope: synthetic-only, never widened by consumption ---
        scope_intact = consumed.observation_scope == OBSERVATION_SCOPE
        if not scope_intact:
            errors.append(
                f"consumption scope must remain {OBSERVATION_SCOPE!r}; "
                f"got {consumed.observation_scope!r}"
            )

        # --- no mutation: consumed fields equal the observed state at consume time ---
        no_mutation = (
            consumed.subject_id == original.subject_id
            and consumed.runtime_activation == original.runtime_activation
            and consumed.execution_status == original.execution_status
            and consumed.observed_at == original.observed_at
            and consumed.source == original.source
        )
        if not no_mutation:
            errors.append("consumed state diverges from the observed state")

        # --- no execution: the consumption never claims execution or activation ---
        no_execution = (
            consumed.execution_status in (NOT_EXECUTED, NOT_APPLICABLE)
            and consumed.runtime_activation in (NOT_ACTIVATED, NOT_APPLICABLE)
        )
        if not no_execution:
            errors.append(
                "consumption claims runtime activation or execution "
                f"(runtime_activation={consumed.runtime_activation!r}, "
                f"execution_status={consumed.execution_status!r})"
            )

        # --- source: still offline, never upgraded to a live/production source ---
        source_offline = consumed.source.startswith(OFFLINE_SOURCE_PREFIXES)
        if not source_offline:
            errors.append("consumed source is not an offline source")

        valid = (
            digest_intact
            and scope_intact
            and no_mutation
            and no_execution
            and source_offline
            and not errors
        )
        return RuntimeConsumptionVerification(
            valid=valid,
            digest_intact=digest_intact,
            scope_intact=scope_intact,
            no_mutation=no_mutation,
            no_execution=no_execution,
            source_offline=source_offline,
            errors=tuple(errors),
        )

    def reconcile(
        self,
        consumed: ConsumedRuntimeState,
        original: ObservedRuntimeState,
    ) -> RuntimeConsumptionReport:
        """Report - never repair - disagreement between consumed and observed.

        If the consumed state contradicts the observed state, the contradiction
        is recorded in the report.  Nothing is activated, revoked, corrected or
        written back.  Resolving a contradiction is a governance decision and is
        out of scope for the consumption layer.
        """
        if not isinstance(consumed, ConsumedRuntimeState):
            raise RuntimeConsumptionError("reconcile requires a ConsumedRuntimeState")
        if not isinstance(original, ObservedRuntimeState):
            raise RuntimeConsumptionError("reconcile requires an ObservedRuntimeState")

        contradictions: list[str] = []
        if consumed.subject_id != original.subject_id:
            contradictions.append(
                "subject mismatch: consumed="
                f"{consumed.subject_id!r}, observed={original.subject_id!r}"
            )
        if consumed.runtime_activation != original.runtime_activation:
            contradictions.append(
                "runtime activation contradicts observation: observed="
                f"{original.runtime_activation!r}, consumed="
                f"{consumed.runtime_activation!r}"
            )
        if consumed.execution_status != original.execution_status:
            contradictions.append(
                "execution status contradicts observation: observed="
                f"{original.execution_status!r}, consumed="
                f"{consumed.execution_status!r}"
            )

        reconciled = not contradictions
        payload = {
            "consumption_id": consumed.consumption_id,
            "reconciled": reconciled,
            "contradictions": list(contradictions),
            "consumed_claim": (
                f"{consumed.runtime_activation}/{consumed.execution_status}"
            ),
            "observed_claim": (
                f"{original.runtime_activation}/{original.execution_status}"
            ),
            "reported_at": consumed.consumed_at,
        }
        return RuntimeConsumptionReport(
            consumption_id=consumed.consumption_id,
            reconciled=reconciled,
            contradictions=tuple(contradictions),
            consumed_claim=payload["consumed_claim"],
            observed_claim=payload["observed_claim"],
            reported_at=consumed.consumed_at,
            report_digest=_digest(payload),
        )
