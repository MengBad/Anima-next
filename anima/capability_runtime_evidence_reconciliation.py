"""P5.5 Runtime Evidence Reconciliation / Governance Isolation.

The reconciliation layer over P5.4's real-store observations.  P5.5 answers
one question only:

    "Is the runtime evidence I read internally consistent, verifiable, and
     how does it differ from previously read evidence?"

It is NOT a runtime fixer, NOT an activation path, NOT a governance authority.

Properties held by construction and by test:

  * READ ONLY        - reconcile()/verify()/inspect() perform no file write,
                       no DB write, no timestamp write-back, no process spawn
  * FAIL CLOSED      - schema or integrity failure is reported as such; the
                       reconciliation is never silently "repaired"
  * NO WAKE-UP       - inputs are in-memory values produced by P5.4; no
                       transport, no runtime initialization
  * NO EXECUTION     - there is no apply / execute / activate / mutate /
                       repair / dispatch / start / stop / reload surface
  * OBSERVATION FACT - ``running=true`` read from the real store is preserved
                       as an *observation fact*; it is never upgraded to
                       authorized / approved / activated / executed / deployed
  * GOVERNANCE ISOLATED - the reconciliation never re-decides or rewrites
                       proposal / review / gate / record evidence
  * ADDITIVE DIGEST  - new evidence digests are computed and appended; no
                       upstream digest (proposal/review/gate/record or P5.1
                       observation) is ever rewritten

Direction of information flow (single direction, never reversed)::

    Real Runtime Store (data/health_history.json)
        -> P5.4 RealObservationStoreAdapter.read()
        -> P5.5 ReconciliationEngine.reconcile()
        -> RuntimeEvidenceReconciliation        (frozen, slots, digest-bound)
        -> Evidence only

Drift classification vocabulary (module constants, used verbatim by the
reconciliation_status field):

    NO_DRIFT            - current evidence matches the previous read
    EXPECTED_DRIFT      - only timestamps / sample count changed (a live
                          heartbeat store advances by design)
    UNEXPECTED_DRIFT    - runtime activation / execution status changed
    INTEGRITY_FAILURE   - store-level integrity check failed (fail closed)
    SCHEMA_FAILURE      - store schema check failed (fail closed)

Digest convention (identical to P3/P4/P5.1/P5.2/P5.4)::

    sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")))
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from anima.capability_runtime_observation import (
    OFFLINE_SOURCE_PREFIXES,
    ObservedRuntimeState,
)
from anima.capability_runtime_observation_store import StoreReadVerification

__all__ = [
    "EXPECTED_DRIFT",
    "INTEGRITY_FAILURE",
    "NO_DRIFT",
    "SCHEMA_FAILURE",
    "UNEXPECTED_DRIFT",
    "ReconciliationEngine",
    "ReconciliationVerification",
    "RuntimeEvidenceReconciliation",
    "RuntimeEvidenceReconciliationError",
]


NO_DRIFT = "NO_DRIFT"
EXPECTED_DRIFT = "EXPECTED_DRIFT"
UNEXPECTED_DRIFT = "UNEXPECTED_DRIFT"
INTEGRITY_FAILURE = "INTEGRITY_FAILURE"
SCHEMA_FAILURE = "SCHEMA_FAILURE"

_OBSERVED_FIELDS = (
    "subject_id",
    "runtime_activation",
    "execution_status",
    "observed_at",
    "source",
)


class RuntimeEvidenceReconciliationError(ValueError):
    """Raised when reconciliation input violates the read-only boundary."""


def _digest(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeEvidenceReconciliationError(
            f"{field_name} must be a non-empty string"
        )
    return value


def _is_hex64(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and set(value) <= frozenset("0123456789abcdef")
    )


def _observed_payload(state: ObservedRuntimeState) -> dict[str, Any]:
    """Read-only serialization of one observed runtime state."""
    if not isinstance(state, ObservedRuntimeState):
        raise RuntimeEvidenceReconciliationError(
            "reconcile accepts ObservedRuntimeState values only"
        )
    return {
        "subject_id": state.subject_id,
        "runtime_activation": state.runtime_activation,
        "execution_status": state.execution_status,
        "observed_at": state.observed_at,
        "source": state.source,
    }


def _observations_digest(observations: tuple[ObservedRuntimeState, ...]) -> str:
    payload = {"observations": [_observed_payload(state) for state in observations]}
    return _digest(payload)


def _check_offline(states: Iterable[ObservedRuntimeState]) -> None:
    """Fail closed if any state escapes the P5.1 offline-source contract."""
    for state in states:
        if not state.source.startswith(OFFLINE_SOURCE_PREFIXES):
            raise RuntimeEvidenceReconciliationError(
                "observed runtime state must come from an offline source; "
                f"got {state.source!r}"
            )


def _changed_fields(
    current: tuple[ObservedRuntimeState, ...],
    previous: tuple[ObservedRuntimeState, ...],
) -> frozenset[str]:
    """Fields that differ between two ordered observation sets (read-only)."""
    changed: set[str] = set()
    for cur, prev in zip(current, previous):
        for field in _OBSERVED_FIELDS:
            if getattr(cur, field) != getattr(prev, field):
                changed.add(field)
    if len(current) != len(previous):
        changed.add("sample_count")
    return frozenset(changed)


def _classify_status(
    schema_valid: bool,
    integrity_valid: bool,
    previous_available: bool,
    changed: frozenset[str],
) -> str:
    """Classify the reconciliation outcome (report only, never fixes)."""
    if not schema_valid:
        return SCHEMA_FAILURE
    if not integrity_valid:
        return INTEGRITY_FAILURE
    if not previous_available or not changed:
        return NO_DRIFT
    # a live heartbeat store advances timestamps and grows samples by design
    if changed <= {"observed_at", "sample_count"}:
        return EXPECTED_DRIFT
    return UNEXPECTED_DRIFT


@dataclass(frozen=True, slots=True)
class RuntimeEvidenceReconciliation:
    """An immutable, digest-bound reconciliation of real runtime evidence.

    ``runtime_state`` is an immutable snapshot of the observed runtime states
    exactly as P5.4 read them - including a ``running`` activation verbatim.
    The reconciliation never upgrades or downgrades an observation fact.
    """

    reconciliation_id: str
    reconciled_at: str
    observation_digest: str
    previous_observation_digest: str
    previous_available: bool
    store_digest: str
    schema_valid: bool
    integrity_valid: bool
    runtime_state: tuple
    drift_detected: bool
    drift_fields: tuple
    reconciliation_status: str
    evidence_digest: str

    def __post_init__(self) -> None:
        for field in (
            "reconciliation_id",
            "reconciled_at",
            "reconciliation_status",
        ):
            _text(getattr(self, field), field)
        # absence markers: previous_observation_digest / store_digest are "" when
        # no previous read / no store verification was provided
        for name in ("previous_observation_digest", "store_digest"):
            value = getattr(self, name)
            if value and not _is_hex64(value):
                raise RuntimeEvidenceReconciliationError(
                    f"{name} must be a SHA-256 hex digest when present"
                )
        if not isinstance(self.runtime_state, tuple):
            raise RuntimeEvidenceReconciliationError(
                "runtime_state must be a tuple of ObservedRuntimeState"
            )
        if not isinstance(self.drift_fields, tuple):
            raise RuntimeEvidenceReconciliationError("drift_fields must be a tuple")
        for name in ("observation_digest", "evidence_digest"):
            if not _is_hex64(getattr(self, name)):
                raise RuntimeEvidenceReconciliationError(
                    f"{name} must be a SHA-256 hex digest"
                )
        if self.reconciliation_status not in (
            NO_DRIFT,
            EXPECTED_DRIFT,
            UNEXPECTED_DRIFT,
            INTEGRITY_FAILURE,
            SCHEMA_FAILURE,
        ):
            raise RuntimeEvidenceReconciliationError(
                f"unknown reconciliation_status: {self.reconciliation_status!r}"
            )


@dataclass(frozen=True, slots=True)
class ReconciliationVerification:
    """Proof object returned by :meth:`ReconciliationEngine.verify`.

    ``valid`` is the aggregate of the independent integrity checks.  A single
    breach is never silently absorbed.
    """

    valid: bool
    observation_digest_intact: bool
    evidence_digest_intact: bool
    previous_digest_intact: bool
    status_intact: bool
    runtime_state_intact: bool
    errors: tuple = ()


class ReconciliationEngine:
    """Read-only reconciliation over real runtime observations.

    Public surface (exactly three side-effect-free operations):

    * ``reconcile`` - normalize observations + store verification into an
      immutable, digest-bound ``RuntimeEvidenceReconciliation``
    * ``verify``    - recompute the digests and fail closed on any tamper
    * ``inspect``   - immutable mapping views of a reconciliation
    """

    def reconcile(
        self,
        observations: Iterable[ObservedRuntimeState],
        *,
        reconciliation_id: str,
        reconciled_at: str,
        previous: Iterable[ObservedRuntimeState] | None = None,
        store_verification: StoreReadVerification | None = None,
    ) -> RuntimeEvidenceReconciliation:
        """Reconcile real-store observations into immutable evidence."""
        _text(reconciliation_id, "reconciliation_id")
        _text(reconciled_at, "reconciled_at")

        current = tuple(observations)
        for state in current:
            if not isinstance(state, ObservedRuntimeState):
                raise RuntimeEvidenceReconciliationError(
                    "reconcile accepts ObservedRuntimeState values only"
                )
        _check_offline(current)

        prior: tuple[ObservedRuntimeState, ...] = ()
        if previous is not None:
            prior = tuple(previous)
            for state in prior:
                if not isinstance(state, ObservedRuntimeState):
                    raise RuntimeEvidenceReconciliationError(
                        "previous observations must be ObservedRuntimeState values"
                    )
            _check_offline(prior)

        # store-level integrity comes from P5.4's verification result
        schema_valid = True
        integrity_valid = True
        store_digest = ""
        if store_verification is not None:
            if not isinstance(store_verification, StoreReadVerification):
                raise RuntimeEvidenceReconciliationError(
                    "store_verification must be a StoreReadVerification"
                )
            schema_valid = store_verification.schema_intact
            integrity_valid = (
                store_verification.valid and store_verification.data_intact
            )
            store_digest = store_verification.locally_computed_digest

        observation_digest = _observations_digest(current)
        previous_observation_digest = _observations_digest(prior) if prior else ""
        previous_available = bool(prior)
        changed = (
            _changed_fields(current, prior) if prior else frozenset()
        )
        status = _classify_status(
            schema_valid, integrity_valid, previous_available, changed
        )
        drift_detected = status in (EXPECTED_DRIFT, UNEXPECTED_DRIFT)

        payload = {
            "reconciliation_id": reconciliation_id,
            "reconciled_at": reconciled_at,
            "observation_digest": observation_digest,
            "previous_observation_digest": previous_observation_digest,
            "previous_available": previous_available,
            "store_digest": store_digest,
            "schema_valid": schema_valid,
            "integrity_valid": integrity_valid,
            "runtime_state": [_observed_payload(state) for state in current],
            "drift_detected": drift_detected,
            "drift_fields": sorted(changed),
            "reconciliation_status": status,
        }
        evidence_digest = _digest(payload)
        return RuntimeEvidenceReconciliation(
            reconciliation_id=reconciliation_id,
            reconciled_at=reconciled_at,
            observation_digest=observation_digest,
            previous_observation_digest=previous_observation_digest,
            previous_available=previous_available,
            store_digest=store_digest,
            schema_valid=schema_valid,
            integrity_valid=integrity_valid,
            runtime_state=current,
            drift_detected=drift_detected,
            drift_fields=tuple(sorted(changed)),
            reconciliation_status=status,
            evidence_digest=evidence_digest,
        )

    def verify(
        self,
        reconciliation: RuntimeEvidenceReconciliation,
        observations: Iterable[ObservedRuntimeState],
        previous: Iterable[ObservedRuntimeState] | None = None,
    ) -> ReconciliationVerification:
        """Verify a reconciliation against its inputs (fails closed)."""
        errors: list[str] = []
        if not isinstance(reconciliation, RuntimeEvidenceReconciliation):
            raise RuntimeEvidenceReconciliationError(
                "verification requires a RuntimeEvidenceReconciliation"
            )
        current = tuple(observations)
        for state in current:
            if not isinstance(state, ObservedRuntimeState):
                raise RuntimeEvidenceReconciliationError(
                    "verification requires ObservedRuntimeState inputs"
                )

        observation_digest_intact = (
            reconciliation.observation_digest == _observations_digest(current)
        )
        if not observation_digest_intact:
            errors.append("observation_digest does not match the current observations")

        prior: tuple[ObservedRuntimeState, ...] = ()
        if previous is not None:
            prior = tuple(previous)
        previous_digest_intact = True
        if prior:
            previous_digest_intact = (
                reconciliation.previous_observation_digest
                == _observations_digest(prior)
            )
            if not previous_digest_intact:
                errors.append(
                    "previous_observation_digest does not match the previous observations"
                )
        elif reconciliation.previous_available:
            previous_digest_intact = False
            errors.append(
                "reconciliation claims a previous read but none was provided"
            )

        payload = {
            "reconciliation_id": reconciliation.reconciliation_id,
            "reconciled_at": reconciliation.reconciled_at,
            "observation_digest": reconciliation.observation_digest,
            "previous_observation_digest": reconciliation.previous_observation_digest,
            "previous_available": reconciliation.previous_available,
            "store_digest": reconciliation.store_digest,
            "schema_valid": reconciliation.schema_valid,
            "integrity_valid": reconciliation.integrity_valid,
            "runtime_state": [
                _observed_payload(state) for state in reconciliation.runtime_state
            ],
            "drift_detected": reconciliation.drift_detected,
            "drift_fields": list(reconciliation.drift_fields),
            "reconciliation_status": reconciliation.reconciliation_status,
        }
        evidence_digest_intact = reconciliation.evidence_digest == _digest(payload)
        if not evidence_digest_intact:
            errors.append("evidence_digest does not match the reconciliation fields")

        status_intact = reconciliation.reconciliation_status in (
            NO_DRIFT,
            EXPECTED_DRIFT,
            UNEXPECTED_DRIFT,
            INTEGRITY_FAILURE,
            SCHEMA_FAILURE,
        )
        if not status_intact:
            errors.append("reconciliation_status is not a known vocabulary value")

        runtime_state_intact = all(
            isinstance(state, ObservedRuntimeState)
            for state in reconciliation.runtime_state
        ) and tuple(reconciliation.runtime_state) == current
        if not runtime_state_intact:
            errors.append("runtime_state diverges from the current observations")

        valid = (
            observation_digest_intact
            and evidence_digest_intact
            and previous_digest_intact
            and status_intact
            and runtime_state_intact
            and not errors
        )
        return ReconciliationVerification(
            valid=valid,
            observation_digest_intact=observation_digest_intact,
            evidence_digest_intact=evidence_digest_intact,
            previous_digest_intact=previous_digest_intact,
            status_intact=status_intact,
            runtime_state_intact=runtime_state_intact,
            errors=tuple(errors),
        )

    def inspect(
        self, reconciliation: RuntimeEvidenceReconciliation
    ) -> Mapping[str, Any]:
        """Return an immutable mapping view of a reconciliation."""
        if not isinstance(reconciliation, RuntimeEvidenceReconciliation):
            raise RuntimeEvidenceReconciliationError(
                "inspect accepts a RuntimeEvidenceReconciliation only"
            )
        return MappingProxyType(
            {
                "reconciliation_id": reconciliation.reconciliation_id,
                "reconciled_at": reconciliation.reconciled_at,
                "observation_digest": reconciliation.observation_digest,
                "previous_observation_digest": (
                    reconciliation.previous_observation_digest
                ),
                "previous_available": reconciliation.previous_available,
                "store_digest": reconciliation.store_digest,
                "schema_valid": reconciliation.schema_valid,
                "integrity_valid": reconciliation.integrity_valid,
                "drift_detected": reconciliation.drift_detected,
                "drift_fields": reconciliation.drift_fields,
                "reconciliation_status": reconciliation.reconciliation_status,
                "evidence_digest": reconciliation.evidence_digest,
            }
        )
