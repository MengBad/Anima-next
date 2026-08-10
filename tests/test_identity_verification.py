import copy
import hashlib
import json

from anima.memory.continuity import MemoryCategory, MemoryStatus, MemoryRecord
from anima.memory.identity_verification import (
    IdentityManifest,
    memory_artifact_digest,
    verify_identity_continuity,
)


def memory_artifact():
    return {
        "schema_version": 1,
        "records": [
            {
                "memory_id": "identity-1",
                "category": MemoryCategory.IDENTITY.value,
                "content": "Anima continuity test identity",
                "source": "phase4.2-test",
                "observed_at": "2099-01-01T00:00:00+00:00",
                "confidence": 1.0,
                "status": MemoryStatus.CONFIRMED.value,
                "scope": "identity",
                "authorization": "test-authorization",
            }
        ],
    }


def project_state(manifest, artifact):
    from anima.memory.continuity import MemoryArtifact

    parsed = MemoryArtifact.from_mapping(artifact)
    return {
        "identity_id": manifest["identity_id"],
        "lineage_id": manifest["lineage_id"],
        "source_instance": manifest["source_instance"],
        "source_commit": manifest["source_commit"],
        "source_version": manifest["source_version"],
        "foundation_state": manifest["foundation_state"],
        "persona_config_digest": manifest["persona_config_digest"],
        "memory_manifest_digest": memory_artifact_digest(parsed),
    }


def manifest_for(artifact):
    from anima.memory.continuity import MemoryArtifact

    parsed = MemoryArtifact.from_mapping(artifact)
    persona_digest = hashlib.sha256(b"persona-config-v1").hexdigest()
    return {
        "schema_version": 1,
        "identity_id": "identity-anima-1",
        "lineage_id": "lineage-anima-1",
        "source_instance": "instance-local-1",
        "source_commit": "37e6b27",
        "source_version": "0.3.0rc2",
        "foundation_state": "FROZEN",
        "identity_artifact_version": "identity-v1",
        "persona_config_digest": persona_digest,
        "memory_manifest_digest": memory_artifact_digest(parsed),
        "created_at": "2099-01-01T00:00:00+00:00",
        "verification_policy": {
            "required_foundation_state": "FROZEN",
            "require_identity_category": True,
        },
    }


def test_valid_manifest_memory_and_project_state_pass():
    artifact = memory_artifact()
    manifest = manifest_for(artifact)
    result = verify_identity_continuity(manifest, artifact, project_state(manifest, artifact))
    assert result == {
        "identity_match": True,
        "lineage_match": True,
        "artifact_integrity": True,
        "foundation_compatibility": True,
        "errors": [],
        "warnings": [],
        "stop": False,
    }


def test_manifest_is_serializable_and_validatable():
    artifact = memory_artifact()
    manifest = manifest_for(artifact)
    parsed = IdentityManifest.from_mapping(json.loads(json.dumps(manifest)))
    assert parsed.to_mapping() == manifest


def test_digest_mismatch_stops():
    artifact = memory_artifact()
    manifest = manifest_for(artifact)
    manifest["memory_manifest_digest"] = "0" * 64
    result = verify_identity_continuity(manifest, artifact, project_state(manifest, artifact))
    assert result["stop"] is True
    assert result["artifact_integrity"] is False
    assert any("memory_digest_mismatch" in error for error in result["errors"])


def test_missing_manifest_field_stops():
    artifact = memory_artifact()
    manifest = manifest_for(artifact)
    manifest.pop("lineage_id")
    result = verify_identity_continuity(manifest, artifact, project_state(manifest_for(artifact), artifact))
    assert result["stop"] is True
    assert any("manifest_invalid" in error for error in result["errors"])


def test_wrong_lineage_stops_without_repair():
    artifact = memory_artifact()
    manifest = manifest_for(artifact)
    state = project_state(manifest, artifact)
    state["lineage_id"] = "lineage-other"
    before = copy.deepcopy(state)
    result = verify_identity_continuity(manifest, artifact, state)
    assert result["stop"] is True
    assert result["lineage_match"] is False
    assert state == before


def test_incompatible_foundation_state_stops():
    artifact = memory_artifact()
    manifest = manifest_for(artifact)
    manifest["foundation_state"] = "EVOLVING"
    state = project_state(manifest, artifact)
    state["foundation_state"] = "EVOLVING"
    result = verify_identity_continuity(manifest, artifact, state)
    assert result["stop"] is True
    assert result["foundation_compatibility"] is False


def test_corrupted_memory_artifact_stops():
    artifact = memory_artifact()
    manifest = manifest_for(artifact)
    corrupted = copy.deepcopy(artifact)
    corrupted["records"][0]["status"] = "not-a-status"
    result = verify_identity_continuity(manifest, corrupted, project_state(manifest, artifact))
    assert result["stop"] is True
    assert any("memory_artifact_invalid" in error for error in result["errors"])
