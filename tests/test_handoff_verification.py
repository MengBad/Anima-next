import copy
import hashlib
import json

from anima.memory.continuity import MemoryArtifact
from anima.memory.handoff_verification import verify_instance_handoff
from anima.memory.identity_verification import memory_artifact_digest


def memory_artifact():
    return {
        "schema_version": 1,
        "records": [
            {
                "memory_id": "identity-1", "category": "identity",
                "content": "handoff identity", "source": "phase4.3-test",
                "observed_at": "2099-01-01T00:00:00+00:00", "confidence": 1.0,
                "status": "confirmed", "scope": "identity", "authorization": "auth-1",
            }
        ],
    }


def manifest(artifact):
    parsed = MemoryArtifact.from_mapping(artifact)
    return {
        "schema_version": 1, "identity_id": "identity-anima-1", "lineage_id": "lineage-1",
        "source_instance": "source-1", "source_commit": "62b5240", "source_version": "0.3.0rc2",
        "foundation_state": "FROZEN", "identity_artifact_version": "identity-v1",
        "persona_config_digest": hashlib.sha256(b"persona").hexdigest(),
        "memory_manifest_digest": memory_artifact_digest(parsed),
        "created_at": "2099-01-01T00:00:00+00:00",
        "verification_policy": {"required_foundation_state": "FROZEN", "require_identity_category": True},
    }


def source_and_target(identity_manifest):
    canonical = json.dumps(identity_manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return (
        {"source_identity_id": "identity-anima-1", "source_lineage_id": "lineage-1", "source_version": "0.3.0rc2", "source_manifest_digest": hashlib.sha256(canonical).hexdigest()},
        {"target_identity_id": "identity-anima-1", "target_lineage_id": "lineage-1", "target_environment": "development"},
    )


def bundle():
    artifact = memory_artifact()
    identity_manifest = manifest(artifact)
    source, target = source_and_target(identity_manifest)
    memory_digest = memory_artifact_digest(MemoryArtifact.from_mapping(artifact))
    return source, target, {
        "identity_manifest": identity_manifest,
        "memory_artifact_manifest": {"artifact_id": "memory-1", "schema_version": 1, "memory_manifest_digest": memory_digest, "source_instance": "source-1", "source_lineage_id": "lineage-1", "provenance_refs": ["memory-source-1"], "created_at": "2099-01-01T00:00:00+00:00"},
        "memory_artifact": artifact,
        "project_state_reference": {"identity_id": "identity-anima-1", "lineage_id": "lineage-1", "source_instance": "source-1", "source_commit": "62b5240", "source_version": "0.3.0rc2", "foundation_state": "FROZEN", "persona_config_digest": identity_manifest["persona_config_digest"], "memory_manifest_digest": memory_digest},
        "authorization_record": {"authorization_id": "handoff-auth-1", "status": "approved", "source_lineage_id": "lineage-1", "target_environment": "development"},
        "verification_result": {"verification_id": "prior-1", "result": "pending"},
    }


def test_valid_handoff_passes():
    source, target, artifact = bundle()
    result = verify_instance_handoff(source, target, artifact)
    assert result["stop"] is False
    assert all(result[key] for key in ("source_identity_valid", "target_eligible", "identity_manifest_consistent", "memory_digest_consistent", "artifact_provenance_complete", "authorization_present", "foundation_compatible"))


def test_artifact_mismatch_stops():
    source, target, artifact = bundle()
    artifact["memory_artifact_manifest"]["memory_manifest_digest"] = "0" * 64
    result = verify_instance_handoff(source, target, artifact)
    assert result["stop"] is True
    assert any("memory_digest_mismatch" in error for error in result["errors"])


def test_identity_mismatch_stops():
    source, target, artifact = bundle()
    target["target_identity_id"] = "identity-other"
    result = verify_instance_handoff(source, target, artifact)
    assert result["stop"] is True
    assert result["target_eligible"] is False


def test_missing_authorization_stops():
    source, target, artifact = bundle()
    artifact["authorization_record"] = {}
    result = verify_instance_handoff(source, target, artifact)
    assert result["stop"] is True
    assert result["authorization_present"] is False


def test_version_mismatch_stops():
    source, target, artifact = bundle()
    source["source_version"] = "different-version"
    result = verify_instance_handoff(source, target, artifact)
    assert result["stop"] is True
    assert result["source_identity_valid"] is False


def test_foundation_mismatch_stops():
    source, target, artifact = bundle()
    artifact["project_state_reference"]["foundation_state"] = "EVOLVING"
    result = verify_instance_handoff(source, target, artifact)
    assert result["stop"] is True
    assert result["foundation_compatible"] is False


def test_input_is_not_mutated():
    source, target, artifact = bundle()
    before = (copy.deepcopy(source), copy.deepcopy(target), copy.deepcopy(artifact))
    verify_instance_handoff(source, target, artifact)
    assert (source, target, artifact) == before
