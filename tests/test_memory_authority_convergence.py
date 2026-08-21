from __future__ import annotations

import copy

import pytest

from anima.memory.authority_convergence import (
    CandidateStatus,
    ConvergenceContractError,
    LifecycleTransitionRecord,
    OfflineMemoryAuthorityPrototype,
    Provenance,
    ReviewDecision,
    RelationType,
    RetentionAction,
    RetentionKind,
    RetentionReference,
    SyntheticSourceRecord,
    verify_provenance_binding,
)


NOW = "2026-08-10T00:00:00Z"


def _observation(candidate_id="c-1", *, content="user prefers concise replies"):
    return {
        "candidate_id": candidate_id,
        "source_type": "synthetic_chat_memory",
        "source_record_id": f"source-{candidate_id}",
        "content": content,
        "category": "user_collaboration",
        "scope": "synthetic-user",
        "observed_at": NOW,
        "provenance": Provenance(
            source_path="synthetic://chat-memory",
            source_record_id=f"source-{candidate_id}",
            observed_at=NOW,
            producer="synthetic-test-operator",
            evidence_refs=(f"evidence-{candidate_id}",),
            transformation_chain=("observation-to-candidate",),
        ),
        "evidence_refs": (f"evidence-{candidate_id}",),
        "confidence": 0.8,
        "status": CandidateStatus.PROPOSED,
        "transformation_chain": ("observation-to-candidate",),
    }


def test_valid_observation_forms_proposed_candidate_with_provenance():
    prototype = OfflineMemoryAuthorityPrototype()
    candidate = prototype.form_candidate(_observation())

    assert candidate.status is CandidateStatus.PROPOSED
    assert candidate.review_ready is True
    assert candidate.source_record_id == candidate.provenance.source_record_id
    assert prototype.snapshots()["canonical"] == ()


def test_candidate_requires_explicit_review_before_canonical_confirmation():
    prototype = OfflineMemoryAuthorityPrototype()
    prototype.form_candidate(_observation())

    with pytest.raises(ConvergenceContractError, match="evidence_refs"):
        prototype.review("c-1", ReviewDecision.CONFIRM, reviewer="operator")

    reviewed = prototype.review(
        "c-1", ReviewDecision.CONFIRM, reviewer="operator", evidence_refs=("review-1",)
    )
    assert reviewed.status is CandidateStatus.CONFIRMED
    assert [item.candidate_id for item in prototype.snapshots()["canonical"]] == ["c-1"]


def test_review_emits_explicit_lifecycle_transition_record():
    prototype = OfflineMemoryAuthorityPrototype()
    prototype.form_candidate(_observation())

    candidate, transition = prototype.review_with_record(
        "c-1",
        ReviewDecision.CONFIRM,
        reviewer="operator",
        evidence_refs=("review-1",),
        transitioned_at=NOW,
        reason="synthetic evidence reviewed",
    )

    assert candidate.status is CandidateStatus.CONFIRMED
    assert isinstance(transition, LifecycleTransitionRecord)
    assert transition.from_status is CandidateStatus.PROPOSED
    assert transition.to_status is CandidateStatus.CONFIRMED
    assert transition.decision is ReviewDecision.CONFIRM


def test_non_proposed_candidate_cannot_be_confirmed_again():
    prototype = OfflineMemoryAuthorityPrototype()
    prototype.form_candidate(_observation())
    prototype.review(
        "c-1", ReviewDecision.REJECT, reviewer="operator", evidence_refs=("review-1",)
    )

    with pytest.raises(ConvergenceContractError, match="only proposed"):
        prototype.review(
            "c-1", ReviewDecision.CONFIRM, reviewer="operator", evidence_refs=("review-2",)
        )


def test_rejected_candidate_cannot_enter_canonical_set():
    prototype = OfflineMemoryAuthorityPrototype()
    prototype.form_candidate(_observation())
    rejected = prototype.review(
        "c-1", ReviewDecision.REJECT, reviewer="operator", evidence_refs=("review-1",)
    )

    assert rejected.status is CandidateStatus.REJECTED
    assert prototype.snapshots()["canonical"] == ()


def test_conflict_is_recorded_without_automatic_winner():
    prototype = OfflineMemoryAuthorityPrototype()
    prototype.form_candidate(_observation("c-1", content="prefers concise replies"))
    prototype.form_candidate(_observation("c-2", content="prefers detailed replies"))

    conflict = prototype.detect_conflict(
        "c-1",
        "c-2",
        conflict_type="preference_conflict",
        evidence_refs=("comparison-1",),
        detected_at=NOW,
    )

    assert conflict.status.value == "open"
    assert conflict.resolution is None
    assert prototype.snapshots()["canonical"] == ()


def test_identity_requires_confirmation_and_separate_identity_authorization():
    prototype = OfflineMemoryAuthorityPrototype()
    raw = _observation("identity-1")
    raw["category"] = "identity"
    prototype.form_candidate(raw)

    assert prototype.identity_read().allowed is False
    prototype.review(
        "identity-1", ReviewDecision.CONFIRM, reviewer="operator", evidence_refs=("review-1",)
    )
    assert prototype.identity_read().allowed is False

    authorized = _observation("identity-2")
    authorized["category"] = "identity"
    authorized["evidence_refs"] = ("identity-authorized",)
    authorized["provenance"] = Provenance(
        source_path="synthetic://identity",
        source_record_id="source-identity-2",
        observed_at=NOW,
        producer="synthetic-test-operator",
        evidence_refs=("identity-authorized",),
    )
    prototype.form_candidate(authorized)
    prototype.review(
        "identity-2", ReviewDecision.CONFIRM, reviewer="operator", evidence_refs=("review-2",)
    )
    decision = prototype.identity_read()
    assert decision.allowed is True
    assert decision.candidate_ids == ("identity-2",)


def test_source_observation_is_not_mutated_and_candidate_is_frozen():
    prototype = OfflineMemoryAuthorityPrototype()
    observation = _observation()
    before = copy.deepcopy(observation)
    candidate = prototype.form_candidate(observation)

    assert observation == before
    with pytest.raises(AttributeError):
        candidate.content = "mutated"  # type: ignore[misc]


def test_duplicate_source_record_is_detected_without_merging():
    prototype = OfflineMemoryAuthorityPrototype()
    first = _observation("c-1")
    second = _observation("c-2")
    second["source_record_id"] = first["source_record_id"]
    second["provenance"] = Provenance(
        source_path="synthetic://chat-memory",
        source_record_id=first["source_record_id"],
        observed_at=NOW,
        producer="synthetic-test-operator",
    )
    prototype.form_candidate(first)
    prototype.form_candidate(second)

    assert prototype.detect_duplicate("c-2") == "c-1"
    assert len(prototype.snapshots()["candidates"]) == 2


def test_normalized_duplicate_does_not_auto_merge():
    prototype = OfflineMemoryAuthorityPrototype()
    prototype.form_candidate(_observation("c-1", content="User prefers concise replies"))
    prototype.form_candidate(_observation("c-2", content="  user   prefers concise   replies "))

    assert prototype.detect_duplicate("c-2") == "c-1"
    assert len(prototype.snapshots()["candidates"]) == 2


def test_malformed_observation_is_rejected_without_state_change():
    prototype = OfflineMemoryAuthorityPrototype()
    raw = _observation()
    raw.pop("content")

    with pytest.raises(ConvergenceContractError, match="content"):
        prototype.form_candidate(raw)
    assert prototype.snapshots()["candidates"] == ()


def test_missing_provenance_is_rejected_without_state_change():
    prototype = OfflineMemoryAuthorityPrototype()
    raw = _observation()
    raw.pop("provenance")

    with pytest.raises(ConvergenceContractError, match="provenance"):
        prototype.form_candidate(raw)
    assert prototype.snapshots()["candidates"] == ()


def test_candidate_cannot_be_confirmed_by_formation():
    prototype = OfflineMemoryAuthorityPrototype()
    raw = _observation()
    raw["status"] = CandidateStatus.CONFIRMED

    with pytest.raises(ConvergenceContractError, match="proposed"):
        prototype.form_candidate(raw)
    assert prototype.snapshots()["canonical"] == ()


def test_synthetic_prototype_has_no_runtime_source_adapters():
    prototype = OfflineMemoryAuthorityPrototype()
    candidate = prototype.form_candidate(_observation())

    assert candidate.provenance.source_path.startswith("synthetic://")
    assert candidate.source_type.startswith("synthetic_")


def test_provenance_binding_verifies_source_snapshot_and_digest():
    prototype = OfflineMemoryAuthorityPrototype()
    observation = _observation()
    candidate = prototype.form_candidate(observation)
    source_record = {
        key: observation[key]
        for key in (
            "source_type",
            "source_record_id",
            "content",
            "category",
            "scope",
            "observed_at",
        )
    }

    result = verify_provenance_binding(candidate, source_record)

    assert result.bound is True
    assert result.source_digest == candidate.provenance.source_digest
    assert result.errors == ()


def test_provenance_binding_rejects_changed_source_without_mutating_candidate():
    prototype = OfflineMemoryAuthorityPrototype()
    observation = _observation()
    candidate = prototype.form_candidate(observation)
    source_record = {
        key: observation[key]
        for key in (
            "source_type",
            "source_record_id",
            "content",
            "category",
            "scope",
            "observed_at",
        )
    }
    source_record["content"] = "changed after formation"
    before = candidate.to_mapping()

    result = verify_provenance_binding(candidate, source_record)

    assert result.bound is False
    assert any("content" in error for error in result.errors)
    assert any("source_digest" in error for error in result.errors)
    assert candidate.to_mapping() == before


def test_provenance_binding_rejects_missing_evidence_or_transformation():
    prototype = OfflineMemoryAuthorityPrototype()
    observation = _observation()
    observation["evidence_refs"] = ()
    observation["transformation_chain"] = ()
    observation["provenance"] = Provenance(
        source_path="synthetic://chat-memory",
        source_record_id="source-c-1",
        observed_at=NOW,
        producer="synthetic-test-operator",
    )
    candidate = prototype.form_candidate(observation)
    source_record = {
        key: observation[key]
        for key in (
            "source_type",
            "source_record_id",
            "content",
            "category",
            "scope",
            "observed_at",
        )
    }

    result = verify_provenance_binding(candidate, source_record)

    assert result.bound is False
    assert "transformation_chain is missing" in result.errors
    assert "provenance evidence_refs are missing" in result.errors


def _source(source_id: str, content: str) -> SyntheticSourceRecord:
    return SyntheticSourceRecord(
        source_type="synthetic_source",
        source_record_id=source_id,
        content=content,
        category="experience",
        scope="synthetic",
        observed_at=NOW,
    )


def test_cross_store_relations_are_explicit_and_not_merges():
    prototype = OfflineMemoryAuthorityPrototype()
    prototype.register_source(_source("s-1", "same observation"))
    prototype.register_source(_source("s-2", "same observation"))

    relation = prototype.create_relation(
        "r-1",
        RelationType.DUPLICATE,
        "s-1",
        "s-2",
        evidence_refs=("dedup-review-1",),
        created_at=NOW,
    )

    assert relation.relation_type is RelationType.DUPLICATE
    assert len(prototype.relation_snapshot()) == 1
    assert len(prototype._sources) == 2  # no merge or deletion


def test_derived_and_conflict_relations_are_distinct():
    prototype = OfflineMemoryAuthorityPrototype()
    for source_id, content in (("s-1", "source"), ("s-2", "derived"), ("s-3", "conflict")):
        prototype.register_source(_source(source_id, content))

    prototype.create_relation(
        "r-derived", RelationType.DERIVED, "s-1", "s-2",
        evidence_refs=("derivation-1",), created_at=NOW,
    )
    prototype.create_relation(
        "r-conflict", RelationType.CONFLICT, "s-1", "s-3",
        evidence_refs=("conflict-1",), created_at=NOW,
    )

    assert {item.relation_type for item in prototype.relation_snapshot()} == {
        RelationType.DERIVED,
        RelationType.CONFLICT,
    }


def test_deletion_impact_analysis_is_read_only_and_reports_relations_and_candidates():
    prototype = OfflineMemoryAuthorityPrototype()
    prototype.register_source(_source("source-c-1", "candidate source"))
    prototype.register_source(_source("s-2", "related"))
    candidate = prototype.form_candidate(_observation())
    prototype.attach_candidate_source(candidate.candidate_id)
    prototype.create_relation(
        "r-1", RelationType.DUPLICATE, "source-c-1", "s-2",
        evidence_refs=("dedup-1",), created_at=NOW,
    )

    before_sources = tuple(prototype._sources)
    impact = prototype.analyze_deletion("source-c-1")

    assert impact.affected_candidate_ids == ("c-1",)
    assert impact.affected_relation_ids == ("r-1",)
    assert impact.related_source_record_ids == ("s-2",)
    assert impact.deletion_executed is False
    assert tuple(prototype._sources) == before_sources


def test_relation_requires_registered_sources_and_evidence():
    prototype = OfflineMemoryAuthorityPrototype()
    prototype.register_source(_source("s-1", "source"))

    with pytest.raises(ConvergenceContractError, match="not registered"):
        prototype.create_relation(
            "r-1", RelationType.DUPLICATE, "s-1", "missing",
            evidence_refs=("e-1",), created_at=NOW,
        )

    prototype.register_source(_source("s-2", "other"))
    with pytest.raises(ConvergenceContractError, match="evidence_refs"):
        prototype.create_relation(
            "r-1", RelationType.DUPLICATE, "s-1", "s-2",
            evidence_refs=(), created_at=NOW,
        )


def test_retention_analysis_maps_dependency_kinds_without_deleting():
    prototype = OfflineMemoryAuthorityPrototype()
    prototype.register_source(_source("s-1", "source"))
    references = (
        RetentionReference("canonical-1", RetentionKind.CANONICAL, "s-1"),
        RetentionReference("source-1", RetentionKind.SOURCE, "s-1"),
        RetentionReference("derived-1", RetentionKind.DERIVED, "derived-owner", ("s-1",)),
        RetentionReference("evidence-1", RetentionKind.EVIDENCE, "evidence-owner", ("s-1",)),
        RetentionReference("contradiction-1", RetentionKind.CONTRADICTION, "s-1"),
    )
    for reference in references:
        prototype.register_retention_reference(reference)

    analysis = prototype.analyze_retention("s-1")

    assert analysis.deletion_executed is False
    assert analysis.requires_authorization is True
    assert analysis.actions == {
        "canonical-1": RetentionAction.NOT_INFERRED,
        "source-1": RetentionAction.REVIEW_REQUIRED,
        "derived-1": RetentionAction.REBUILD_OR_DELETE,
        "evidence-1": RetentionAction.RETAIN_TOMBSTONE,
        "contradiction-1": RetentionAction.RETAIN,
    }
    assert tuple(prototype._retention_refs) == tuple(item.reference_id for item in references)


def test_retention_analysis_requires_registered_source_and_preserves_references():
    prototype = OfflineMemoryAuthorityPrototype()
    reference = RetentionReference("source-1", RetentionKind.SOURCE, "s-1")
    prototype.register_retention_reference(reference)

    with pytest.raises(ConvergenceContractError, match="not registered"):
        prototype.analyze_retention("s-1")

    prototype.register_source(_source("s-1", "source"))
    before = tuple(prototype._retention_refs)
    analysis = prototype.analyze_retention("s-1")

    assert analysis.references == (reference,)
    assert tuple(prototype._retention_refs) == before


def test_retention_reference_duplicate_is_rejected():
    prototype = OfflineMemoryAuthorityPrototype()
    reference = RetentionReference("r-1", RetentionKind.SOURCE, "s-1")
    prototype.register_retention_reference(reference)

    with pytest.raises(ConvergenceContractError, match="duplicate"):
        prototype.register_retention_reference(reference)
