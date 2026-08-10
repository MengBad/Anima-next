import copy
import hashlib
import json

from click.testing import CliRunner

from anima.cli import cli
from anima.memory.continuity import MemoryArtifact
from anima.memory.continuity_manifest import verify_continuity_manifest
from anima.memory.handoff_verification import verify_instance_handoff
from anima.memory.identity_verification import memory_artifact_digest, verify_identity_continuity
from anima.memory.runtime_identity_attestation import verify_runtime_identity_attestation


def _memory():
    return {"schema_version": 1, "records": [{
        "memory_id": "identity-1", "category": "identity", "content": "attestation identity",
        "source": "phase5.1-test", "observed_at": "2099-01-01T00:00:00+00:00",
        "confidence": 1.0, "status": "confirmed", "scope": "identity", "authorization": "auth-1",
    }]}


def _identity(memory):
    return {
        "schema_version": 1, "identity_id": "identity-anima-1", "lineage_id": "lineage-1",
        "source_instance": "source-1", "source_commit": "62b5240", "source_version": "0.3.0rc2",
        "foundation_state": "FROZEN", "identity_artifact_version": "identity-v1",
        "persona_config_digest": hashlib.sha256(b"persona").hexdigest(),
        "memory_manifest_digest": memory_artifact_digest(MemoryArtifact.from_mapping(memory)),
        "created_at": "2099-01-01T00:00:00+00:00",
        "verification_policy": {"required_foundation_state": "FROZEN", "require_identity_category": True},
    }


def _bundle():
    memory = _memory()
    identity = _identity(memory)
    memory_digest = memory_artifact_digest(MemoryArtifact.from_mapping(memory))
    identity_digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    project = {
        "identity_id": "identity-anima-1", "lineage_id": "lineage-1", "source_instance": "source-1",
        "source_commit": "62b5240", "source_version": "0.3.0rc2", "foundation_state": "FROZEN",
        "persona_config_digest": identity["persona_config_digest"], "memory_manifest_digest": memory_digest,
    }
    handoff_artifact = {
        "identity_manifest": identity,
        "memory_artifact_manifest": {
            "artifact_id": "memory-1", "schema_version": 1, "memory_manifest_digest": memory_digest,
            "source_instance": "source-1", "source_lineage_id": "lineage-1",
            "provenance_refs": ["memory-source-1"], "created_at": "2099-01-01T00:00:00+00:00",
        },
        "memory_artifact": memory,
        "project_state_reference": project,
        "authorization_record": {"authorization_id": "handoff-auth-1", "status": "approved", "source_lineage_id": "lineage-1", "target_environment": "development"},
        "verification_result": {"verification_id": "prior-1", "result": "pending"},
    }
    source = {"source_identity_id": "identity-anima-1", "source_lineage_id": "lineage-1", "source_version": "0.3.0rc2", "source_manifest_digest": identity_digest}
    target = {"target_identity_id": "identity-anima-1", "target_lineage_id": "lineage-1", "target_environment": "development"}
    handoff_result = verify_instance_handoff(source, target, handoff_artifact)
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
    continuity_result = verify_continuity_manifest(manifest, identity, memory, handoff_result, project)
    attestation = {
        "schema_version": 1, "attestation_id": "attestation-1", "instance_id": "target-instance-1",
        "identity_reference": {"identity_id": "identity-anima-1", "lineage_id": "lineage-1", "digest": identity_digest},
        "continuity_manifest_reference": {"manifest_id": "continuity-1", "digest": hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()},
        "source_version": "0.3.0rc2", "foundation_state": "FROZEN",
        "verification_timestamp": "2099-01-01T00:00:00+00:00",
        "verification_result": {"identity_match": True, "manifest_match": True, "version_match": True, "foundation_compatible": True, "memory_continuity_verified": True, "handoff_verified": True, "errors": [], "warnings": [], "stop": False},
    }
    return {"attestation": attestation, "identity_manifest": identity, "memory_artifact": memory, "continuity_manifest": manifest, "source_instance": source, "target_instance": target, "handoff_artifact": handoff_artifact, "project_state": project}


def test_valid_attestation_passes():
    bundle = _bundle()
    result = verify_runtime_identity_attestation(bundle)
    assert result["stop"] is False
    assert all(result[field] for field in ("identity_match", "manifest_match", "version_match", "foundation_compatible", "memory_continuity_verified", "handoff_verified"))


def test_identity_mismatch_stops():
    bundle = _bundle()
    bundle["attestation"]["identity_reference"]["identity_id"] = "other"
    result = verify_runtime_identity_attestation(bundle)
    assert result["stop"] is True
    assert any("identity_mismatch" in error for error in result["errors"])


def test_manifest_mismatch_stops():
    bundle = _bundle()
    bundle["attestation"]["continuity_manifest_reference"]["digest"] = "0" * 64
    result = verify_runtime_identity_attestation(bundle)
    assert result["stop"] is True
    assert result["manifest_match"] is False


def test_version_mismatch_stops():
    bundle = _bundle()
    bundle["attestation"]["source_version"] = "other-version"
    result = verify_runtime_identity_attestation(bundle)
    assert result["stop"] is True
    assert result["version_match"] is False


def test_corrupted_artifact_stops():
    bundle = _bundle()
    bundle["memory_artifact"]["records"][0]["content"] = "corrupted"
    result = verify_runtime_identity_attestation(bundle)
    assert result["stop"] is True
    assert result["memory_continuity_verified"] is False


def test_input_is_not_mutated():
    bundle = _bundle()
    before = copy.deepcopy(bundle)
    verify_runtime_identity_attestation(bundle)
    assert bundle == before


def test_start_attestation_failure_stops_before_runtime(tmp_path):
    path = tmp_path / "attestation.json"
    path.write_text(json.dumps({"not": "a bundle"}), encoding="utf-8")
    result = CliRunner().invoke(cli, ["start", "--identity-attestation", str(path)])
    assert result.exit_code == 1
    assert '"stop": true' in result.output
