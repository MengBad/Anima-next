"""Offline-only Memory Authority convergence prototype.

This module is intentionally not imported by runtime startup or any existing
memory writer.  It provides a synthetic-data bridge for reviewing the
relationship between source observations, candidates, conflicts, and a
canonical in-memory representation.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


class ConvergenceContractError(ValueError):
    """Raised when an offline convergence contract is invalid."""


class CandidateStatus(str, Enum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    DEPRECATED = "deprecated"
    CONFLICT = "conflict"


class ConflictStatus(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"


class ReviewDecision(str, Enum):
    CONFIRM = "confirm"
    REJECT = "reject"
    DEPRECATE = "deprecate"
    RETAIN = "retain"
    SUPERSEDE = "supersede"
    REQUEST_REVIEW = "request_review"


class RelationType(str, Enum):
    DUPLICATE = "duplicate"
    DERIVED = "derived"
    CONFLICT = "conflict"


class RetentionKind(str, Enum):
    CANONICAL = "canonical"
    SOURCE = "source"
    DERIVED = "derived"
    EVIDENCE = "evidence"
    CONTRADICTION = "contradiction"


class RetentionAction(str, Enum):
    REVIEW_REQUIRED = "review_required"
    RETAIN = "retain"
    RETAIN_TOMBSTONE = "retain_tombstone"
    REBUILD_OR_DELETE = "rebuild_or_delete"
    NOT_INFERRED = "not_inferred"


REQUIRED_CANDIDATE_FIELDS = frozenset(
    {
        "candidate_id",
        "source_type",
        "source_record_id",
        "content",
        "category",
        "scope",
        "observed_at",
        "provenance",
        "confidence",
        "status",
    }
)


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConvergenceContractError(f"{field_name} must be a non-empty string")
    return value


def _timestamp(value: Any, field_name: str) -> str:
    value = _text(value, field_name)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConvergenceContractError(
            f"{field_name} must be an ISO-8601 timestamp"
        ) from exc
    return value


@dataclass(frozen=True, slots=True)
class Provenance:
    """Traceability for a candidate transformation."""

    source_path: str
    source_record_id: str
    observed_at: str
    producer: str
    evidence_refs: tuple[str, ...] = ()
    transformation_chain: tuple[str, ...] = ()
    source_digest: str | None = None

    def __post_init__(self) -> None:
        _text(self.source_path, "source_path")
        _text(self.source_record_id, "source_record_id")
        _timestamp(self.observed_at, "observed_at")
        _text(self.producer, "producer")
        for ref in (*self.evidence_refs, *self.transformation_chain):
            _text(ref, "provenance reference")
        if self.source_digest is not None:
            if len(self.source_digest) != 64 or any(
                character not in "0123456789abcdef" for character in self.source_digest
            ):
                raise ConvergenceContractError("source_digest must be a SHA-256 hex digest")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "source_record_id": self.source_record_id,
            "observed_at": self.observed_at,
            "producer": self.producer,
            "evidence_refs": list(self.evidence_refs),
            "transformation_chain": list(self.transformation_chain),
            "source_digest": self.source_digest,
        }


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    """A proposed memory; never canonical by construction."""

    candidate_id: str
    source_type: str
    source_record_id: str
    content: str
    category: str
    scope: str
    observed_at: str
    provenance: Provenance
    evidence_refs: tuple[str, ...]
    confidence: float
    status: CandidateStatus = CandidateStatus.PROPOSED
    transformation_chain: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "candidate_id",
            "source_type",
            "source_record_id",
            "content",
            "category",
            "scope",
        ):
            _text(getattr(self, name), name)
        _timestamp(self.observed_at, "observed_at")
        if isinstance(self.confidence, bool) or not 0.0 <= float(self.confidence) <= 1.0:
            raise ConvergenceContractError("confidence must be between 0 and 1")
        for ref in (*self.evidence_refs, *self.transformation_chain):
            _text(ref, "candidate reference")
        if self.provenance.source_record_id != self.source_record_id:
            raise ConvergenceContractError(
                "provenance source_record_id must match candidate source_record_id"
            )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "source_type": self.source_type,
            "source_record_id": self.source_record_id,
            "content": self.content,
            "category": self.category,
            "scope": self.scope,
            "observed_at": self.observed_at,
            "provenance": self.provenance.to_mapping(),
            "evidence_refs": list(self.evidence_refs),
            "confidence": float(self.confidence),
            "status": self.status.value,
            "transformation_chain": list(self.transformation_chain),
        }

    @property
    def review_ready(self) -> bool:
        return self.status is CandidateStatus.PROPOSED


@dataclass(frozen=True, slots=True)
class ConflictRecord:
    candidate_a: str
    candidate_b: str
    conflict_type: str
    evidence_refs: tuple[str, ...]
    detected_at: str
    status: ConflictStatus = ConflictStatus.OPEN
    resolution: str | None = None
    resolver: str | None = None
    resolution_evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("candidate_a", "candidate_b", "conflict_type"):
            _text(getattr(self, name), name)
        if self.candidate_a == self.candidate_b:
            raise ConvergenceContractError("a conflict needs two candidates")
        _timestamp(self.detected_at, "detected_at")
        for ref in (*self.evidence_refs, *self.resolution_evidence):
            _text(ref, "conflict reference")
        if self.status is ConflictStatus.OPEN and any(
            value is not None
            for value in (self.resolution, self.resolver)
        ):
            raise ConvergenceContractError("open conflicts cannot have a resolution")
        if self.status is ConflictStatus.RESOLVED:
            _text(self.resolution, "resolution")
            _text(self.resolver, "resolver")


@dataclass(frozen=True, slots=True)
class IdentityAccessDecision:
    allowed: bool
    reason: str
    candidate_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LifecycleTransitionRecord:
    candidate_id: str
    from_status: CandidateStatus
    to_status: CandidateStatus
    decision: ReviewDecision
    reviewer: str
    transitioned_at: str
    evidence_refs: tuple[str, ...]
    reason: str

    def __post_init__(self) -> None:
        _text(self.candidate_id, "candidate_id")
        _text(self.reviewer, "reviewer")
        _timestamp(self.transitioned_at, "transitioned_at")
        _text(self.reason, "reason")
        if self.from_status is self.to_status:
            raise ConvergenceContractError("lifecycle transition must change status")
        if not self.evidence_refs:
            raise ConvergenceContractError("lifecycle transition requires evidence_refs")
        for ref in self.evidence_refs:
            _text(ref, "transition evidence reference")


@dataclass(frozen=True, slots=True)
class SyntheticSourceRecord:
    source_type: str
    source_record_id: str
    content: str
    category: str
    scope: str
    observed_at: str

    def __post_init__(self) -> None:
        for name in ("source_type", "source_record_id", "content", "category", "scope"):
            _text(getattr(self, name), name)
        _timestamp(self.observed_at, "observed_at")


@dataclass(frozen=True, slots=True)
class CrossStoreRelation:
    relation_id: str
    relation_type: RelationType
    left_source_record_id: str
    right_source_record_id: str
    evidence_refs: tuple[str, ...]
    created_at: str

    def __post_init__(self) -> None:
        for name in (
            "relation_id",
            "left_source_record_id",
            "right_source_record_id",
        ):
            _text(getattr(self, name), name)
        if self.left_source_record_id == self.right_source_record_id:
            raise ConvergenceContractError("relation needs two source records")
        _timestamp(self.created_at, "created_at")
        if not self.evidence_refs:
            raise ConvergenceContractError("relation requires evidence_refs")
        for ref in self.evidence_refs:
            _text(ref, "relation evidence reference")


@dataclass(frozen=True, slots=True)
class DeletionImpact:
    source_record_id: str
    affected_candidate_ids: tuple[str, ...]
    affected_relation_ids: tuple[str, ...]
    related_source_record_ids: tuple[str, ...]
    canonical_memory_impact: str
    deletion_executed: bool = False


@dataclass(frozen=True, slots=True)
class RetentionReference:
    reference_id: str
    kind: RetentionKind
    source_record_id: str
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.reference_id, "reference_id")
        _text(self.source_record_id, "source_record_id")
        for reference in self.depends_on:
            _text(reference, "retention dependency")


@dataclass(frozen=True, slots=True)
class RetentionAnalysis:
    source_record_id: str
    references: tuple[RetentionReference, ...]
    actions: Mapping[str, RetentionAction]
    deletion_executed: bool = False
    requires_authorization: bool = True
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProvenanceBindingResult:
    bound: bool
    source_digest: str | None
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(slots=True)
class OfflineMemoryAuthorityPrototype:
    """Synthetic-only review store with explicit, non-automatic transitions."""

    _candidates: dict[str, MemoryCandidate] = field(default_factory=dict)
    _canonical: dict[str, MemoryCandidate] = field(default_factory=dict)
    _conflicts: list[ConflictRecord] = field(default_factory=list)
    _sources: dict[str, SyntheticSourceRecord] = field(default_factory=dict)
    _relations: dict[str, CrossStoreRelation] = field(default_factory=dict)
    _candidate_source_ids: dict[str, str] = field(default_factory=dict)
    _retention_refs: dict[str, RetentionReference] = field(default_factory=dict)

    def form_candidate(self, observation: Mapping[str, Any]) -> MemoryCandidate:
        """Copy a source observation into a proposed candidate."""
        raw = copy.deepcopy(dict(observation))
        missing = REQUIRED_CANDIDATE_FIELDS - set(raw)
        if missing:
            raise ConvergenceContractError(
                f"observation missing fields: {sorted(missing)}"
            )
        provenance = raw.pop("provenance")
        if not isinstance(provenance, Provenance):
            raise ConvergenceContractError("provenance must be a Provenance object")
        source_snapshot = {
            key: raw[key]
            for key in (
                "source_type",
                "source_record_id",
                "content",
                "category",
                "scope",
                "observed_at",
            )
        }
        source_digest = _digest(source_snapshot)
        if provenance.source_digest not in (None, source_digest):
            raise ConvergenceContractError("provenance source_digest does not match source")
        provenance = Provenance(
            source_path=provenance.source_path,
            source_record_id=provenance.source_record_id,
            observed_at=provenance.observed_at,
            producer=provenance.producer,
            evidence_refs=provenance.evidence_refs,
            transformation_chain=provenance.transformation_chain,
            source_digest=source_digest,
        )
        candidate = MemoryCandidate(
            provenance=provenance,
            evidence_refs=tuple(raw.pop("evidence_refs", ())),
            transformation_chain=tuple(raw.pop("transformation_chain", ())),
            **raw,
        )
        if candidate.status is not CandidateStatus.PROPOSED:
            raise ConvergenceContractError("new candidates must start as proposed")
        if candidate.candidate_id in self._candidates:
            raise ConvergenceContractError("duplicate candidate_id")
        self._candidates[candidate.candidate_id] = candidate
        return candidate

    def register_source(self, source: SyntheticSourceRecord) -> None:
        """Register an immutable synthetic source record for relation analysis."""
        if source.source_record_id in self._sources:
            raise ConvergenceContractError("duplicate source_record_id")
        self._sources[source.source_record_id] = copy.deepcopy(source)

    def attach_candidate_source(self, candidate_id: str) -> None:
        candidate = self._candidates[candidate_id]
        if candidate.provenance.source_record_id not in self._sources:
            raise ConvergenceContractError("candidate source record is not registered")
        self._candidate_source_ids[candidate_id] = candidate.provenance.source_record_id

    def create_relation(
        self,
        relation_id: str,
        relation_type: RelationType,
        left_source_record_id: str,
        right_source_record_id: str,
        *,
        evidence_refs: tuple[str, ...],
        created_at: str,
    ) -> CrossStoreRelation:
        if relation_id in self._relations:
            raise ConvergenceContractError("duplicate relation_id")
        if left_source_record_id not in self._sources:
            raise ConvergenceContractError("left source record is not registered")
        if right_source_record_id not in self._sources:
            raise ConvergenceContractError("right source record is not registered")
        relation = CrossStoreRelation(
            relation_id=relation_id,
            relation_type=relation_type,
            left_source_record_id=left_source_record_id,
            right_source_record_id=right_source_record_id,
            evidence_refs=evidence_refs,
            created_at=created_at,
        )
        self._relations[relation_id] = relation
        return relation

    def analyze_deletion(self, source_record_id: str) -> DeletionImpact:
        if source_record_id not in self._sources:
            raise ConvergenceContractError("source record is not registered")
        relation_ids: list[str] = []
        related_ids: set[str] = set()
        for relation in self._relations.values():
            if source_record_id in (
                relation.left_source_record_id,
                relation.right_source_record_id,
            ):
                relation_ids.append(relation.relation_id)
                related_ids.update(
                    {
                        relation.left_source_record_id,
                        relation.right_source_record_id,
                    }
                )
        candidate_ids = tuple(
            candidate_id
            for candidate_id, source_id in self._candidate_source_ids.items()
            if source_id == source_record_id
        )
        related_ids.discard(source_record_id)
        return DeletionImpact(
            source_record_id=source_record_id,
            affected_candidate_ids=tuple(sorted(candidate_ids)),
            affected_relation_ids=tuple(sorted(relation_ids)),
            related_source_record_ids=tuple(sorted(related_ids)),
            canonical_memory_impact="not evaluated; no canonical store is connected",
        )

    def relation_snapshot(self) -> tuple[CrossStoreRelation, ...]:
        return tuple(self._relations.values())

    def register_retention_reference(self, reference: RetentionReference) -> None:
        if reference.reference_id in self._retention_refs:
            raise ConvergenceContractError("duplicate retention reference_id")
        self._retention_refs[reference.reference_id] = copy.deepcopy(reference)

    def analyze_retention(self, source_record_id: str) -> RetentionAnalysis:
        """Return retention/deletion impact recommendations without mutating state."""
        if source_record_id not in self._sources:
            raise ConvergenceContractError("source record is not registered")
        references = tuple(
            reference
            for reference in self._retention_refs.values()
            if reference.source_record_id == source_record_id
            or source_record_id in reference.depends_on
        )
        actions = {
            reference.reference_id: _retention_action(reference.kind)
            for reference in references
        }
        return RetentionAnalysis(
            source_record_id=source_record_id,
            references=references,
            actions=actions,
            notes=(
                "analysis only; no source, canonical, derived, evidence, or contradiction record was changed",
            ),
        )

    def detect_duplicate(self, candidate_id: str) -> str | None:
        candidate = self._candidates[candidate_id]
        for existing in self._candidates.values():
            if existing.candidate_id != candidate_id and (
                existing.source_record_id == candidate.source_record_id
                or (
                    _normalize_content(existing.content)
                    == _normalize_content(candidate.content)
                    and existing.scope == candidate.scope
                )
            ):
                return existing.candidate_id
        return None

    def detect_conflict(
        self,
        candidate_a: str,
        candidate_b: str,
        *,
        conflict_type: str,
        evidence_refs: tuple[str, ...],
        detected_at: str,
    ) -> ConflictRecord:
        if candidate_a not in self._candidates or candidate_b not in self._candidates:
            raise ConvergenceContractError("conflict candidates must exist")
        conflict = ConflictRecord(
            candidate_a=candidate_a,
            candidate_b=candidate_b,
            conflict_type=conflict_type,
            evidence_refs=evidence_refs,
            detected_at=detected_at,
        )
        self._conflicts.append(conflict)
        self._candidates[candidate_a] = _replace_status(
            self._candidates[candidate_a], CandidateStatus.CONFLICT
        )
        self._candidates[candidate_b] = _replace_status(
            self._candidates[candidate_b], CandidateStatus.CONFLICT
        )
        return conflict

    def review(
        self,
        candidate_id: str,
        decision: ReviewDecision,
        *,
        reviewer: str,
        evidence_refs: tuple[str, ...] = (),
    ) -> MemoryCandidate:
        """Apply an explicit review decision; no automatic promotion exists."""
        updated, _ = self.review_with_record(
            candidate_id,
            decision,
            reviewer=reviewer,
            evidence_refs=evidence_refs,
        )
        return updated

    def review_with_record(
        self,
        candidate_id: str,
        decision: ReviewDecision,
        *,
        reviewer: str,
        evidence_refs: tuple[str, ...] = (),
        transitioned_at: str = "2026-08-10T00:00:00Z",
        reason: str = "explicit offline review",
    ) -> tuple[MemoryCandidate, LifecycleTransitionRecord]:
        _text(reviewer, "reviewer")
        if not evidence_refs:
            raise ConvergenceContractError("review requires evidence_refs")
        candidate = self._candidates[candidate_id]
        transitions = {
            ReviewDecision.CONFIRM: CandidateStatus.CONFIRMED,
            ReviewDecision.REJECT: CandidateStatus.REJECTED,
            ReviewDecision.DEPRECATE: CandidateStatus.DEPRECATED,
        }
        if decision not in transitions:
            raise ConvergenceContractError(
                "retain/supersede/request_review require an explicit conflict workflow"
            )
        if candidate.status is not CandidateStatus.PROPOSED:
            raise ConvergenceContractError(
                "only proposed candidates may be confirmed, rejected, or deprecated"
            )
        target_status = transitions[decision]
        updated = _replace_status(candidate, target_status)
        record = LifecycleTransitionRecord(
            candidate_id=candidate_id,
            from_status=candidate.status,
            to_status=target_status,
            decision=decision,
            reviewer=reviewer,
            transitioned_at=transitioned_at,
            evidence_refs=evidence_refs,
            reason=reason,
        )
        self._candidates[candidate_id] = updated
        if target_status is CandidateStatus.CONFIRMED:
            self._canonical[candidate_id] = updated
        return updated, record

    def identity_read(self) -> IdentityAccessDecision:
        allowed = tuple(
            candidate.candidate_id
            for candidate in self._canonical.values()
            if candidate.category == "identity"
            and "identity-authorized" in candidate.evidence_refs
        )
        if not allowed:
            return IdentityAccessDecision(False, "no confirmed identity-authorized memory")
        return IdentityAccessDecision(True, "confirmed identity-authorized memory", allowed)

    def snapshots(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "candidates": tuple(self._candidates.values()),
                "canonical": tuple(self._canonical.values()),
                "conflicts": tuple(self._conflicts),
            }
        )


def verify_provenance_binding(
    candidate: MemoryCandidate,
    source_record: Mapping[str, Any],
) -> ProvenanceBindingResult:
    """Verify a candidate against a supplied immutable source snapshot."""
    required = {
        "source_type",
        "source_record_id",
        "content",
        "category",
        "scope",
        "observed_at",
    }
    missing = required - set(source_record)
    if missing:
        return ProvenanceBindingResult(
            bound=False,
            source_digest=None,
            errors=(f"source record missing fields: {sorted(missing)}",),
        )

    source_snapshot = {key: source_record[key] for key in sorted(required)}
    source_digest = _digest(source_snapshot)
    errors: list[str] = []
    for field_name in required:
        candidate_value = getattr(candidate, field_name)
        if candidate_value != source_record[field_name]:
            errors.append(f"{field_name} does not match source record")
    if candidate.provenance.source_record_id != source_record["source_record_id"]:
        errors.append("provenance source_record_id does not match source record")
    if candidate.provenance.source_digest != source_digest:
        errors.append("provenance source_digest does not match source record")
    if not candidate.provenance.transformation_chain:
        errors.append("transformation_chain is missing")
    if not candidate.provenance.evidence_refs:
        errors.append("provenance evidence_refs are missing")
    return ProvenanceBindingResult(
        bound=not errors,
        source_digest=source_digest,
        errors=tuple(errors),
    )


def _replace_status(candidate: MemoryCandidate, status: CandidateStatus) -> MemoryCandidate:
    return MemoryCandidate(
        candidate_id=candidate.candidate_id,
        source_type=candidate.source_type,
        source_record_id=candidate.source_record_id,
        content=candidate.content,
        category=candidate.category,
        scope=candidate.scope,
        observed_at=candidate.observed_at,
        provenance=candidate.provenance,
        evidence_refs=candidate.evidence_refs,
        confidence=candidate.confidence,
        status=status,
        transformation_chain=candidate.transformation_chain,
    )


def _normalize_content(content: str) -> str:
    return " ".join(content.casefold().split())


def _retention_action(kind: RetentionKind) -> RetentionAction:
    return {
        RetentionKind.CANONICAL: RetentionAction.NOT_INFERRED,
        RetentionKind.SOURCE: RetentionAction.REVIEW_REQUIRED,
        RetentionKind.DERIVED: RetentionAction.REBUILD_OR_DELETE,
        RetentionKind.EVIDENCE: RetentionAction.RETAIN_TOMBSTONE,
        RetentionKind.CONTRADICTION: RetentionAction.RETAIN,
    }[kind]


def _digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "CandidateStatus",
    "CrossStoreRelation",
    "ConflictRecord",
    "ConflictStatus",
    "ConvergenceContractError",
    "IdentityAccessDecision",
    "DeletionImpact",
    "LifecycleTransitionRecord",
    "MemoryCandidate",
    "OfflineMemoryAuthorityPrototype",
    "Provenance",
    "ProvenanceBindingResult",
    "REQUIRED_CANDIDATE_FIELDS",
    "ReviewDecision",
    "RelationType",
    "RetentionAction",
    "RetentionAnalysis",
    "RetentionKind",
    "RetentionReference",
    "SyntheticSourceRecord",
    "verify_provenance_binding",
]
