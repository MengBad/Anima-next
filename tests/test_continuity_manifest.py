import copy
import hashlib
import json

from anima.memory.continuity import MemoryArtifact
from anima.memory.continuity_manifest import verify_continuity_manifest
from anima.memory.identity_verification import memory_artifact_digest


def _memory():
    return {
        "schema_version": 1,
        "records": [{
            "memory_id": "identity-1", "category": "identity",
            "content": "continuity test identity", "source": "phase4.4-test",
            "observed_at": "2099-01-01T00:00:00+00:00", "confidence": 1.0,
            "status": "confirmed", "scope": "identity", "authorization": "auth-1",
        }],
    }


def _identity(memory):
    digest = memory_artifact_digest(MemoryArtifact.from_mapping(memory))
    return {
        "schema_version": 1, "identity_id": "identity-anima-1", "lineage_id": "lineage-1",
        "source_instance": "source-1", "source_commit": "62b5240", "source_version": "0.3.0rc2",
        "foundation_state": "FROZEN", "identity_artifact_version": "identity-v1",
        "persona_config_digest": hashlib.sha256(b"persona").hexdigest(),
        "memory_manifest_digest": digest, "created_at": "2099-01-01T00:00:00+00:00",
        "verification_policy": {"required_foundation_state": "FROZEN", "require_identity_category": True},
    }


def _bundle():
    memory = _memory()
    identity = _identity(memory)
    identity_digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    memory_digest = memory_artifact_digest(MemoryArtifact.from_mapping(memory))
    manifest = {
        "schema_version": 1, "manifest_id": "continuity-1",
        "identity_reference": {"identity_id": "identity-anima-1", "lineage_id": "lineage-1", "artifact_version": "identity-v1", "digest": identity_digest},
        "memory_artifact_reference": {"artifact_id": "memory-1", "digest": memory_digest, "schema_version": 1, "source_lineage_id": "lineage-1"},
        "project_state_reference": {"source_commit": "62b5240", "source_version": "0.3.0rc2"},
        "foundation_state": "FROZEN",
        "verification_metadata": {"verifier_version": "phase4.4-v1", "verified_at": "2099-01-01T00:00:00+00:00", "policy": {"mode": "offline-read-only"}},
        "handoff_reference": {"handoff_id": "handoff-1", "verification_id": "verification-1"},
        "authorization_reference": "auth-1", "created_at": "2099-01-01T00:00:00+00:00",
    }
    handoff = {
        "source_identity_valid": True, "target_eligible": True, "identity_manifest_consistent": True,
        "memory_digest_consistent": True, "artifact_provenance_complete": True,
        "authorization_present": True, "foundation_compatible": True, "errors": [], "stop": False,
    }
    project = {"source_commit": "62b5240", "source_version": "0.3.0rc2", "foundation_state": "FROZEN"}
    return manifest, identity, memory, handoff, project


def test_valid_manifest_passes_without_mutation():
    bundle = _bundle()
    before = copy.deepcopy(bundle)
    result = verify_continuity_manifest(*bundle, authorization_reference="auth-1")
    assert result == {"manifest_valid": True, "identity_valid": True, "memory_digest_match": True, "handoff_valid": True, "project_version_match": True, "foundation_compatible": True, "authorization_reference_present": True, "errors": [], "warnings": [], "stop": False}
    assert bundle == before


def test_missing_identity_stops():
    bundle = list(_bundle())
    bundle[1] = {}
    result = verify_continuity_manifest(*bundle, authorization_reference="auth-1")
    assert result["stop"] is True
    assert any("identity_invalid" in error for error in result["errors"])


def test_memory_mismatch_stops():
    bundle = _bundle()
    bundle[2]["records"][0]["content"] = "changed"
    result = verify_continuity_manifest(*bundle, authorization_reference="auth-1")
    assert result["stop"] is True
    assert result["memory_digest_match"] is False


def test_invalid_handoff_stops():
    bundle = _bundle()
    bundle[3]["stop"] = True
    result = verify_continuity_manifest(*bundle, authorization_reference="auth-1")
    assert result["stop"] is True
    assert result["handoff_valid"] is False


def test_version_mismatch_stops():
    bundle = _bundle()
    bundle[4]["source_version"] = "different"
    result = verify_continuity_manifest(*bundle, authorization_reference="auth-1")
    assert result["stop"] is True
    assert result["project_version_match"] is False


def test_foundation_mismatch_stops():
    bundle = _bundle()
    bundle[4]["foundation_state"] = "EVOLVING"
    result = verify_continuity_manifest(*bundle, authorization_reference="auth-1")
    assert result["stop"] is True
    assert result["foundation_compatible"] is False


def test_authorization_mismatch_stops():
    bundle = _bundle()
    result = verify_continuity_manifest(*bundle, authorization_reference="other-auth")
    assert result["stop"] is True
    assert result["authorization_reference_present"] is False
