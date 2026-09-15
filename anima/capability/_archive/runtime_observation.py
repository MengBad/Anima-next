"""P5.1 Runtime Observation Bridge / Read-only Adapter.

This module is the first - and deliberately the *thinnest* - bridge between the
frozen P3/P4 governance evidence and any runtime observer.  It is:

  * READ-ONLY        - it never writes, never stores, never repairs
  * OBSERVATION-ONLY - it normalizes evidence into immutable observations
  * NON-EXECUTOR     - there is no activate / execute / apply / mutate entry point
  * OFFLINE-ONLY     - observed runtime state must come from a synthetic,
                       fixture, recorded, local or mock source

Direction of information flow (single direction, never reversed)::

    Governance Evidence  ->  Runtime Observation  ->  Observation Verification
      (P3 / P4 records)        (normalized, frozen)     (recomputed, fails closed)

What this module explicitly does NOT do
---------------------------------------
* It does not re-decide governance.  A gate decision is consumed as immutable
  evidence; it is never re-judged.
* It does not upgrade a claim.  ``approved`` is reported as ``approved``; it is
  never reported as ``executed``, ``active``, ``deployed`` or ``running``.
* It does not repair drift.  When the observed runtime state contradicts the
  governance record, the contradiction is *reported*; nothing is changed on
  either side.

Digest convention (identical to P3/P4)::

    sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")))

The payload mirrors below intentionally duplicate the upstream payload shapes so
that a digest can be recomputed without importing private helpers.  The mirror
is pinned to upstream behaviour by
``tests/test_capability_runtime_observation.py::test_evidence_digest_mirror_matches_upstream_builders``
- if an upstream payload ever changes, that test fails loudly rather than
silently accepting a stale mirror.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from anima.capability_activation_proof import ActivationRecord
from anima.capability_staged_activation import StagedActivationRecord
from anima.capability_suspend_revocation import RevocationRecord, SuspensionRecord
from anima.capability_evolution_contract import (
    EvolutionGateRecord,
    EvolutionProposal,
    EvolutionRecord,
)

__all__ = [
    "NOT_ACTIVATED",
    "NOT_APPLICABLE",
    "NOT_EXECUTED",
    "OBSERVATION_SCOPE",
    "OFFLINE_SOURCE_PREFIXES",
    "UNKNOWN",
    "EvidenceKind",
    "ObservationSnapshot",
    "ObservedRuntimeState",
    "ReadOnlyRuntimeObservationAdapter",
    "RuntimeDriftReport",
    "RuntimeObservation",
    "RuntimeObservationError",
    "RuntimeObservationVerification",
    "detect_runtime_drift",
    "recompute_evidence_digest",
    "verify_runtime_observation",
]


OBSERVATION_SCOPE = "synthetic-only"
NOT_ACTIVATED = "not_activated"
NOT_EXECUTED = "not_executed"
NOT_APPLICABLE = "not_applicable"
UNKNOWN = "unknown"

#: Only offline evidence may be presented as an observed runtime state.
OFFLINE_SOURCE_PREFIXES = (
    "synthetic://",
    "fixture://",
    "offline://",
    "local://",
    "recorded://",
    "mock://",
)

_HEX = frozenset("0123456789abcdef")


class RuntimeObservationError(ValueError):
    """Raised when observation input violates the read-only boundary."""


class EvidenceKind:
    """Governance evidence kinds this adapter is allowed to observe."""

    ACTIVATION_RECORD = "activation_record"
    STAGED_ACTIVATION = "staged_activation"
    SUSPENSION = "suspension_record"
    REVOCATION = "revocation_record"
    EVOLUTION_GATE = "evolution_gate_record"
    EVOLUTION_RECORD = "evolution_record"


#: Evidence kinds whose scope lives on the bound EvolutionProposal, not on the
#: record itself.  Observing them requires the proposal for provenance.
_PROPOSAL_BOUND_KINDS = frozenset(
    {EvidenceKind.EVOLUTION_GATE, EvidenceKind.EVOLUTION_RECORD}
)


def _digest(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _is_hex64(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeObservationError(f"{field_name} must be a non-empty string")
    return value


# ---------------------------------------------------------------------------
# evidence classification + read-only digest mirror
# ---------------------------------------------------------------------------
def _classify(evidence: Any) -> str:
    if isinstance(evidence, ActivationRecord):
        return EvidenceKind.ACTIVATION_RECORD
    if isinstance(evidence, StagedActivationRecord):
        return EvidenceKind.STAGED_ACTIVATION
    if isinstance(evidence, SuspensionRecord):
        return EvidenceKind.SUSPENSION
    if isinstance(evidence, RevocationRecord):
        return EvidenceKind.REVOCATION
    if isinstance(evidence, EvolutionGateRecord):
        return EvidenceKind.EVOLUTION_GATE
    if isinstance(evidence, EvolutionRecord):
        return EvidenceKind.EVOLUTION_RECORD
    raise RuntimeObservationError(
        f"unsupported governance evidence: {type(evidence).__name__}"
    )


def _evidence_payload(evidence: Any, kind: str) -> dict[str, Any]:
    """Mirror of the upstream digest payload (read-only reconstruction)."""
    if kind == EvidenceKind.ACTIVATION_RECORD:
        return {
            "record_id": evidence.record_id,
            "plan_id": evidence.plan_id,
            "proposal_id": evidence.proposal_id,
            "review_id": evidence.review_id,
            "plan_digest": evidence.plan_digest,
            "scope": evidence.scope,
            "status": evidence.status,
            "recorded_at": evidence.recorded_at,
        }
    if kind == EvidenceKind.STAGED_ACTIVATION:
        return {
            "stage_id": evidence.stage_id,
            "plan_id": evidence.plan_id,
            "activation_record_id": evidence.activation_record_id,
            "proof_id": evidence.proof_id,
            "capability_id": evidence.capability_id,
            "scope": evidence.scope,
            "stage": evidence.stage,
            "runtime_activation": evidence.runtime_activation,
            "gate_refs": list(evidence.gate_refs),
            "staged_at": evidence.staged_at,
        }
    if kind == EvidenceKind.SUSPENSION:
        return {
            "suspension_id": evidence.suspension_id,
            "activation_record_id": evidence.activation_record_id,
            "capability_id": evidence.capability_id,
            "scope": evidence.scope,
            "authorization": evidence.authorization,
            "status": evidence.status,
            "runtime_activation": evidence.runtime_activation,
            "activation_record_digest": evidence.activation_record_digest,
            "suspended_at": evidence.suspended_at,
        }
    if kind == EvidenceKind.REVOCATION:
        return {
            "revocation_id": evidence.revocation_id,
            "activation_record_id": evidence.activation_record_id,
            "suspension_id": evidence.suspension_id,
            "capability_id": evidence.capability_id,
            "scope": evidence.scope,
            "authorization": evidence.authorization,
            "status": evidence.status,
            "runtime_activation": evidence.runtime_activation,
            "activation_record_digest": evidence.activation_record_digest,
            "suspension_digest": evidence.suspension_digest,
            "revoked_at": evidence.revoked_at,
        }
    if kind == EvidenceKind.EVOLUTION_GATE:
        return {
            "gate_id": evidence.gate_id,
            "proposal_id": evidence.proposal_id,
            "proposal_digest": evidence.proposal_digest,
            "review_id": evidence.review_id,
            "decision": evidence.decision,
            "reviewer": evidence.reviewer,
            "evidence_refs": list(evidence.evidence_refs),
            "created_at": evidence.created_at,
            "rationale": evidence.rationale,
        }
    if kind == EvidenceKind.EVOLUTION_RECORD:
        return {
            "record_id": evidence.record_id,
            "proposal_id": evidence.proposal_id,
            "proposal_digest": evidence.proposal_digest,
            "review_id": evidence.review_id,
            "review_digest": evidence.review_digest,
            "gate_id": evidence.gate_id,
            "gate_digest": evidence.gate_digest,
            "decision": evidence.decision,
            "lifecycle_state": evidence.lifecycle_state,
            "execution_status": evidence.execution_status,
            "recorder": evidence.recorder,
            "recorded_at": evidence.recorded_at,
        }
    raise RuntimeObservationError(f"unsupported evidence kind: {kind!r}")


_CARRIED_DIGEST_FIELD = {
    EvidenceKind.ACTIVATION_RECORD: "record_digest",
    EvidenceKind.STAGED_ACTIVATION: "stage_digest",
    EvidenceKind.SUSPENSION: "suspension_digest",
    EvidenceKind.REVOCATION: "revocation_digest",
    EvidenceKind.EVOLUTION_GATE: "digest",
    EvidenceKind.EVOLUTION_RECORD: "record_digest",
}

_EVIDENCE_ID_FIELD = {
    EvidenceKind.ACTIVATION_RECORD: "record_id",
    EvidenceKind.STAGED_ACTIVATION: "stage_id",
    EvidenceKind.SUSPENSION: "suspension_id",
    EvidenceKind.REVOCATION: "revocation_id",
    EvidenceKind.EVOLUTION_GATE: "gate_id",
    EvidenceKind.EVOLUTION_RECORD: "record_id",
}


def recompute_evidence_digest(evidence: Any) -> str:
    """Recompute the evidence digest from its own fields (pure, read-only)."""
    kind = _classify(evidence)
    return _digest(_evidence_payload(evidence, kind))


def _carried_digest(evidence: Any, kind: str) -> str:
    return getattr(evidence, _CARRIED_DIGEST_FIELD[kind])


def _evidence_id(evidence: Any, kind: str) -> str:
    return getattr(evidence, _EVIDENCE_ID_FIELD[kind])


# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RuntimeObservation:
    """An immutable, digest-bound, read-only view of one governance record.

    The observation reports *what the evidence says* - faithfully.  It never
    sanitizes, upgrades or downgrades a claim; verification is what fails
    closed.
    """

    observation_id: str
    evidence_kind: str
    evidence_id: str
    evidence_digest: str
    subject_id: str
    scope: str
    governance_state: str
    runtime_activation_claim: str
    execution_claim: str
    provenance_refs: tuple
    observed_at: str
    observation_digest: str

    def __post_init__(self) -> None:
        for field in (
            "observation_id",
            "evidence_kind",
            "evidence_id",
            "evidence_digest",
            "subject_id",
            "scope",
            "governance_state",
            "runtime_activation_claim",
            "execution_claim",
            "observed_at",
            "observation_digest",
        ):
            _text(getattr(self, field), field)
        if not isinstance(self.provenance_refs, tuple):
            raise RuntimeObservationError("provenance_refs must be a tuple")
        for name in ("evidence_digest", "observation_digest"):
            if not _is_hex64(getattr(self, name)):
                raise RuntimeObservationError(f"{name} must be a SHA-256 hex digest")


@dataclass(frozen=True, slots=True)
class ObservedRuntimeState:
    """A runtime state reported by an *offline* observer.

    This is deliberately separate from the governance state: the two are never
    merged, and the governance record is never overwritten by what a runtime
    observer claims.
    """

    subject_id: str
    runtime_activation: str
    execution_status: str
    observed_at: str
    source: str

    def __post_init__(self) -> None:
        for field in (
            "subject_id",
            "runtime_activation",
            "execution_status",
            "observed_at",
            "source",
        ):
            _text(getattr(self, field), field)
        if not self.source.startswith(OFFLINE_SOURCE_PREFIXES):
            raise RuntimeObservationError(
                "observed runtime state must come from an offline source "
                f"({'|'.join(OFFLINE_SOURCE_PREFIXES)}); got {self.source!r}"
            )


@dataclass(frozen=True, slots=True)
class ObservationSnapshot:
    """An immutable, digest-bound batch of observations."""

    snapshot_id: str
    observations: tuple
    collected_at: str
    snapshot_digest: str

    def __post_init__(self) -> None:
        _text(self.snapshot_id, "snapshot_id")
        _text(self.collected_at, "collected_at")
        if not isinstance(self.observations, tuple):
            raise RuntimeObservationError("observations must be a tuple")
        if not _is_hex64(self.snapshot_digest):
            raise RuntimeObservationError("snapshot_digest must be a SHA-256 hex digest")


@dataclass(frozen=True, slots=True)
class RuntimeObservationVerification:
    """Proof object returned by :func:`verify_runtime_observation`.

    ``valid`` describes the *integrity of the observation itself*.  Drift is
    reported separately via ``drift_detected`` and never silently invalidates a
    faithful observation.
    """

    valid: bool
    evidence_intact: bool
    governance_binding_intact: bool
    scope_intact: bool
    no_mutation: bool
    no_execution: bool
    drift_detected: bool
    errors: tuple = ()


@dataclass(frozen=True, slots=True)
class RuntimeDriftReport:
    """A report - not a remediation - of governance/runtime disagreement."""

    subject_id: str
    drift_detected: bool
    contradictions: tuple
    governance_claim: str
    observed_claim: str
    reported_at: str


# ---------------------------------------------------------------------------
# normalization
# ---------------------------------------------------------------------------
def _derived_execution_claim(runtime_activation_claim: str) -> str:
    """P3 evidence carries no execution field; derive it, never upgrade it."""
    if runtime_activation_claim == NOT_ACTIVATED:
        return NOT_EXECUTED
    return UNKNOWN


def _normalize(
    evidence: Any, kind: str, proposal: EvolutionProposal | None
) -> dict[str, Any]:
    if kind in _PROPOSAL_BOUND_KINDS:
        if proposal is None:
            raise RuntimeObservationError(
                f"{kind} carries no scope; the bound EvolutionProposal is required"
            )
        if not isinstance(proposal, EvolutionProposal):
            raise RuntimeObservationError("proposal must be an EvolutionProposal")
        if proposal.proposal_id != evidence.proposal_id:
            raise RuntimeObservationError("proposal does not belong to this evidence")
        if proposal.proposal_digest != evidence.proposal_digest:
            raise RuntimeObservationError("proposal digest binding broken")

    if kind == EvidenceKind.ACTIVATION_RECORD:
        claim = evidence.status
        return {
            "subject_id": evidence.proposal_id,
            "scope": evidence.scope,
            "governance_state": evidence.status,
            "runtime_activation_claim": claim,
            "execution_claim": _derived_execution_claim(claim),
            "provenance_refs": (
                evidence.plan_id,
                evidence.review_id,
                evidence.plan_digest,
            ),
        }
    if kind == EvidenceKind.STAGED_ACTIVATION:
        claim = evidence.runtime_activation
        return {
            "subject_id": evidence.capability_id,
            "scope": evidence.scope,
            "governance_state": evidence.stage,
            "runtime_activation_claim": claim,
            "execution_claim": _derived_execution_claim(claim),
            "provenance_refs": (
                evidence.plan_id,
                evidence.activation_record_id,
                evidence.proof_id,
            )
            + tuple(evidence.gate_refs),
        }
    if kind == EvidenceKind.SUSPENSION:
        claim = evidence.runtime_activation
        return {
            "subject_id": evidence.capability_id,
            "scope": evidence.scope,
            "governance_state": evidence.status,
            "runtime_activation_claim": claim,
            "execution_claim": _derived_execution_claim(claim),
            "provenance_refs": (
                evidence.activation_record_id,
                evidence.activation_record_digest,
                evidence.authorization,
            ),
        }
    if kind == EvidenceKind.REVOCATION:
        claim = evidence.runtime_activation
        return {
            "subject_id": evidence.capability_id,
            "scope": evidence.scope,
            "governance_state": evidence.status,
            "runtime_activation_claim": claim,
            "execution_claim": _derived_execution_claim(claim),
            "provenance_refs": (
                evidence.activation_record_id,
                evidence.suspension_id,
                evidence.activation_record_digest,
                evidence.suspension_digest,
                evidence.authorization,
            ),
        }
    if kind == EvidenceKind.EVOLUTION_GATE:
        # A gate records a decision.  It is structurally not an executor
        # (proven by P4.2 verify_evolution_gate.gate_not_executor), so the
        # observation reports the decision and nothing more.
        return {
            "subject_id": evidence.proposal_id,
            "scope": proposal.scope,  # type: ignore[union-attr]
            "governance_state": evidence.decision,
            "runtime_activation_claim": NOT_APPLICABLE,
            "execution_claim": NOT_EXECUTED,
            "provenance_refs": (
                evidence.proposal_digest,
                evidence.review_id,
                evidence.reviewer,
            )
            + tuple(evidence.evidence_refs),
        }
    if kind == EvidenceKind.EVOLUTION_RECORD:
        return {
            "subject_id": evidence.proposal_id,
            "scope": proposal.scope,  # type: ignore[union-attr]
            "governance_state": evidence.lifecycle_state,
            "runtime_activation_claim": NOT_APPLICABLE,
            "execution_claim": evidence.execution_status,
            "provenance_refs": (
                evidence.proposal_digest,
                evidence.review_digest,
                evidence.gate_digest,
                evidence.recorder,
            ),
        }
    raise RuntimeObservationError(f"unsupported evidence kind: {kind!r}")


def _observation_payload(fields: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "observation_id": fields["observation_id"],
        "evidence_kind": fields["evidence_kind"],
        "evidence_id": fields["evidence_id"],
        "evidence_digest": fields["evidence_digest"],
        "subject_id": fields["subject_id"],
        "scope": fields["scope"],
        "governance_state": fields["governance_state"],
        "runtime_activation_claim": fields["runtime_activation_claim"],
        "execution_claim": fields["execution_claim"],
        "provenance_refs": list(fields["provenance_refs"]),
        "observed_at": fields["observed_at"],
    }


# ---------------------------------------------------------------------------
# the adapter (read-only surface only)
# ---------------------------------------------------------------------------
class ReadOnlyRuntimeObservationAdapter:
    """Read-only bridge from governance evidence to runtime observations.

    The public surface is intentionally limited to five side-effect-free
    operations: ``observe``, ``read_state``, ``snapshot``, ``inspect`` and
    ``collect_observation``.  No operation writes, stores, dispatches, or
    reaches outside the process.
    """

    def observe(
        self,
        evidence: Any,
        *,
        observation_id: str,
        observed_at: str,
        proposal: EvolutionProposal | None = None,
    ) -> RuntimeObservation:
        _text(observation_id, "observation_id")
        _text(observed_at, "observed_at")
        kind = _classify(evidence)
        carried = _carried_digest(evidence, kind)
        if not _is_hex64(carried):
            raise RuntimeObservationError("evidence digest is not a SHA-256 hex digest")
        normalized = _normalize(evidence, kind, proposal)
        fields = {
            "observation_id": observation_id,
            "evidence_kind": kind,
            "evidence_id": _evidence_id(evidence, kind),
            "evidence_digest": carried,
            "observed_at": observed_at,
            **normalized,
        }
        return RuntimeObservation(
            observation_digest=_digest(_observation_payload(fields)), **fields
        )

    def read_state(self, evidence: Any) -> str:
        """Return only the governance state string carried by the evidence."""
        kind = _classify(evidence)
        if kind == EvidenceKind.STAGED_ACTIVATION:
            return evidence.stage
        if kind == EvidenceKind.EVOLUTION_GATE:
            return evidence.decision
        if kind == EvidenceKind.EVOLUTION_RECORD:
            return evidence.lifecycle_state
        return evidence.status

    def collect_observation(
        self,
        evidences: Iterable[Any],
        *,
        observation_id_prefix: str,
        observed_at: str,
        proposals: Mapping[str, EvolutionProposal] | None = None,
    ) -> tuple:
        _text(observation_id_prefix, "observation_id_prefix")
        collected: list[RuntimeObservation] = []
        for index, evidence in enumerate(evidences, start=1):
            kind = _classify(evidence)
            proposal = None
            if kind in _PROPOSAL_BOUND_KINDS and proposals is not None:
                proposal = proposals.get(evidence.proposal_id)
            collected.append(
                self.observe(
                    evidence,
                    observation_id=f"{observation_id_prefix}-{index}",
                    observed_at=observed_at,
                    proposal=proposal,
                )
            )
        return tuple(collected)

    def snapshot(
        self,
        observations: Sequence[RuntimeObservation],
        *,
        snapshot_id: str,
        collected_at: str,
    ) -> ObservationSnapshot:
        _text(snapshot_id, "snapshot_id")
        _text(collected_at, "collected_at")
        items = tuple(observations)
        for item in items:
            if not isinstance(item, RuntimeObservation):
                raise RuntimeObservationError(
                    "snapshot accepts RuntimeObservation instances only"
                )
        payload = {
            "snapshot_id": snapshot_id,
            "observation_digests": [item.observation_digest for item in items],
            "collected_at": collected_at,
        }
        return ObservationSnapshot(
            snapshot_id=snapshot_id,
            observations=items,
            collected_at=collected_at,
            snapshot_digest=_digest(payload),
        )

    def inspect(self, observation: RuntimeObservation) -> Mapping[str, Any]:
        """Return an immutable mapping view of an observation."""
        if not isinstance(observation, RuntimeObservation):
            raise RuntimeObservationError("inspect accepts a RuntimeObservation only")
        return MappingProxyType(
            {
                "observation_id": observation.observation_id,
                "evidence_kind": observation.evidence_kind,
                "evidence_id": observation.evidence_id,
                "evidence_digest": observation.evidence_digest,
                "subject_id": observation.subject_id,
                "scope": observation.scope,
                "governance_state": observation.governance_state,
                "runtime_activation_claim": observation.runtime_activation_claim,
                "execution_claim": observation.execution_claim,
                "provenance_refs": observation.provenance_refs,
                "observed_at": observation.observed_at,
                "observation_digest": observation.observation_digest,
            }
        )


# ---------------------------------------------------------------------------
# verification (fails closed)
# ---------------------------------------------------------------------------
def verify_runtime_observation(
    observation: RuntimeObservation,
    evidence: Any,
    observed_state: ObservedRuntimeState | None = None,
) -> RuntimeObservationVerification:
    """Verify an observation against its governance evidence (fails closed).

    Proves that the observation is a faithful, unmodified, digest-bound view of
    the evidence, that it stays inside the synthetic-only scope, that observing
    changed nothing, and that no execution is claimed.
    """
    errors: list[str] = []

    if not isinstance(observation, RuntimeObservation):
        raise RuntimeObservationError("verification requires a RuntimeObservation")

    kind = _classify(evidence)
    if observation.evidence_kind != kind:
        errors.append("observation evidence_kind does not match the evidence type")

    carried = _carried_digest(evidence, kind)
    recomputed = recompute_evidence_digest(evidence)

    # --- evidence integrity: the record still hashes to its own digest ---
    evidence_intact = carried == recomputed
    if not evidence_intact:
        errors.append("evidence digest does not match its recomputed payload")

    # --- governance binding: identity + digest + faithful normalization ---
    governance_binding_intact = (
        observation.evidence_id == _evidence_id(evidence, kind)
        and observation.evidence_digest == carried
    )
    if not governance_binding_intact:
        errors.append("observation is not bound to this evidence")

    expected_observation_digest = _digest(
        _observation_payload(
            {
                "observation_id": observation.observation_id,
                "evidence_kind": observation.evidence_kind,
                "evidence_id": observation.evidence_id,
                "evidence_digest": observation.evidence_digest,
                "subject_id": observation.subject_id,
                "scope": observation.scope,
                "governance_state": observation.governance_state,
                "runtime_activation_claim": observation.runtime_activation_claim,
                "execution_claim": observation.execution_claim,
                "provenance_refs": observation.provenance_refs,
                "observed_at": observation.observed_at,
            }
        )
    )
    if observation.observation_digest != expected_observation_digest:
        governance_binding_intact = False
        errors.append("observation digest does not match its own fields")

    # --- scope: synthetic-only, never widened by observation ---
    scope_intact = observation.scope == OBSERVATION_SCOPE
    if not scope_intact:
        errors.append(
            f"observation scope must remain {OBSERVATION_SCOPE!r}; got {observation.scope!r}"
        )

    # --- no mutation: the evidence is unchanged since it was observed ---
    no_mutation = recomputed == observation.evidence_digest
    if not no_mutation:
        errors.append("evidence changed after it was observed")

    # --- no execution: the observation never claims execution or activation ---
    no_execution = observation.execution_claim in (
        NOT_EXECUTED,
        NOT_APPLICABLE,
    ) and observation.runtime_activation_claim in (NOT_ACTIVATED, NOT_APPLICABLE)
    if not no_execution:
        errors.append(
            "observation claims runtime activation or execution "
            f"(runtime_activation_claim={observation.runtime_activation_claim!r}, "
            f"execution_claim={observation.execution_claim!r})"
        )

    # --- drift is reported, never repaired, and never merged into validity ---
    drift_detected = False
    if observed_state is not None:
        drift_detected = detect_runtime_drift(observation, observed_state).drift_detected

    valid = (
        evidence_intact
        and governance_binding_intact
        and scope_intact
        and no_mutation
        and no_execution
        and not errors
    )
    return RuntimeObservationVerification(
        valid=valid,
        evidence_intact=evidence_intact,
        governance_binding_intact=governance_binding_intact,
        scope_intact=scope_intact,
        no_mutation=no_mutation,
        no_execution=no_execution,
        drift_detected=drift_detected,
        errors=tuple(errors),
    )


def detect_runtime_drift(
    observation: RuntimeObservation, observed_state: ObservedRuntimeState
) -> RuntimeDriftReport:
    """Report - never repair - disagreement between governance and runtime.

    If the observed runtime contradicts the governance record, the contradiction
    is recorded in the report.  Nothing is activated, revoked, corrected or
    written back.  Resolving a contradiction is a governance decision and is out
    of scope for the observation layer.
    """
    if not isinstance(observation, RuntimeObservation):
        raise RuntimeObservationError("drift detection requires a RuntimeObservation")
    if not isinstance(observed_state, ObservedRuntimeState):
        raise RuntimeObservationError("drift detection requires an ObservedRuntimeState")
    if observation.subject_id != observed_state.subject_id:
        raise RuntimeObservationError(
            "observed runtime state belongs to a different subject"
        )

    contradictions: list[str] = []
    if (
        observation.runtime_activation_claim != NOT_APPLICABLE
        and observed_state.runtime_activation != observation.runtime_activation_claim
    ):
        contradictions.append(
            "runtime activation contradicts governance: governance="
            f"{observation.runtime_activation_claim!r}, observed="
            f"{observed_state.runtime_activation!r}"
        )
    if (
        observation.execution_claim != NOT_APPLICABLE
        and observed_state.execution_status != observation.execution_claim
    ):
        contradictions.append(
            "execution status contradicts governance: governance="
            f"{observation.execution_claim!r}, observed="
            f"{observed_state.execution_status!r}"
        )

    return RuntimeDriftReport(
        subject_id=observation.subject_id,
        drift_detected=bool(contradictions),
        contradictions=tuple(contradictions),
        governance_claim=(
            f"{observation.runtime_activation_claim}/{observation.execution_claim}"
        ),
        observed_claim=(
            f"{observed_state.runtime_activation}/{observed_state.execution_status}"
        ),
        reported_at=observed_state.observed_at,
    )
