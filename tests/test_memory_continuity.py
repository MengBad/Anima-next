import json

import pytest

from anima.memory.continuity import (
    MemoryArtifact,
    MemoryCategory,
    MemoryContractError,
    MemoryStatus,
    ReadOnlyMemoryLoader,
    REQUIRED_RECORD_FIELDS,
    verify_continuity,
)


def _record(memory_id="m-1", category="identity", status="confirmed"):
    return {
        "memory_id": memory_id,
        "category": category,
        "content": "Foundation remains frozen.",
        "source": "operator-confirmed",
        "observed_at": "2026-08-10T00:00:00Z",
        "confidence": 1.0,
        "status": status,
        "scope": "anima-next",
        "authorization": "human-confirmed",
    }


def test_record_contract_contains_exact_required_fields():
    artifact = MemoryArtifact.from_mapping({"schema_version": 1, "records": [_record()]})
    assert set(artifact.records[0].to_mapping()) == REQUIRED_RECORD_FIELDS
    assert artifact.records[0].status is MemoryStatus.CONFIRMED


def test_all_four_categories_are_isolated():
    records = [
        _record("identity-1", "identity"),
        _record("project-1", "project"),
        _record("experience-1", "experience"),
        _record("collab-1", "user_collaboration"),
    ]
    artifact = MemoryArtifact.from_mapping({"schema_version": 1, "records": records})
    assert [item.memory_id for item in artifact.for_category(MemoryCategory.IDENTITY)] == [
        "identity-1"
    ]
    assert [item.memory_id for item in artifact.for_category("user_collaboration")] == [
        "collab-1"
    ]


@pytest.mark.parametrize("status", [item.value for item in MemoryStatus])
def test_status_values_are_explicit(status):
    artifact = MemoryArtifact.from_mapping(
        {"schema_version": 1, "records": [_record(status=status)]}
    )
    assert artifact.records[0].status.value == status


def test_invalid_record_is_rejected():
    raw = _record()
    raw.pop("authorization")
    with pytest.raises(MemoryContractError, match="authorization"):
        MemoryArtifact.from_mapping({"schema_version": 1, "records": [raw]})


def test_invalid_confidence_and_unknown_category_are_rejected():
    raw = _record()
    raw["confidence"] = 1.1
    with pytest.raises(MemoryContractError, match="confidence"):
        MemoryArtifact.from_mapping({"schema_version": 1, "records": [raw]})

    raw = _record()
    raw["category"] = "execution"
    with pytest.raises(MemoryContractError, match="category"):
        MemoryArtifact.from_mapping({"schema_version": 1, "records": [raw]})


def test_loader_is_read_only_and_verification_reports_counts(tmp_path):
    path = tmp_path / "memory_continuity.json"
    payload = {
        "schema_version": 1,
        "records": [
            _record("i-1", "identity"),
            _record("p-1", "project", "observed"),
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = path.read_bytes()

    artifact = ReadOnlyMemoryLoader().load(path)
    result = verify_continuity(path)

    assert len(artifact.records) == 2
    assert result.valid is True
    assert result.record_count == 2
    assert result.category_counts == {
        "identity": 1,
        "project": 1,
        "experience": 0,
        "user_collaboration": 0,
    }
    assert path.read_bytes() == before


def test_verification_does_not_accept_malformed_artifact(tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text("{\"schema_version\": 1, \"records\": [}", encoding="utf-8")
    result = verify_continuity(path)
    assert result.valid is False
    assert result.record_count == 0
    assert result.errors
