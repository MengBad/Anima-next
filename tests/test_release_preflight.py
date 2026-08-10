import copy
import hashlib
import json
from datetime import datetime, timezone

from anima.memory.continuity import MemoryArtifact
from anima.memory.identity_verification import memory_artifact_digest
from anima.release_preflight import verify_release_preflight


def _base_bundle():
    memory = {
        "schema_version": 1,
        "records": [{
            "memory_id": "synthetic-identity-1", "category": "identity",
            "content": "synthetic continuity identity", "source": "synthetic:phase5.4.2",
            "observed_at": "2099-01-01T00:00:00+00:00", "confidence": 1.0,
            "status": "confirmed", "scope": "identity", "authorization": "auth-1",
        }],
    }
    memory_digest = memory_artifact_digest(MemoryArtifact.from_mapping(memory))
    identity = {
        "schema_version": 1, "identity_id": "identity-anima-1", "lineage_id": "lineage-1",
        "source_instance": "source-1", "source_commit": "rc-commit-1", "source_version": "0.3.0rc2",
        "foundation_state": "FROZEN", "identity_artifact_version": "identity-v1",
        "persona_config_digest": hashlib.sha256(b"synthetic-persona").hexdigest(),
        "memory_manifest_digest": memory_digest, "created_at": "2099-01-01T00:00:00+00:00",
        "verification_policy": {"required_foundation_state": "FROZEN", "require_identity_category": True},
    }
    identity_digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    project = {
        "identity_id": "identity-anima-1", "lineage_id": "lineage-1", "source_instance": "source-1",
        "source_commit": "rc-commit-1", "source_version": "0.3.0rc2", "foundation_state": "FROZEN",
        "persona_config_digest": identity["persona_config_digest"], "memory_manifest_digest": memory_digest,
    }
    handoff = {
        "identity_manifest": identity,
        "memory_artifact_manifest": {
            "artifact_id": "synthetic-memory-1", "schema_version": 1,
            "memory_manifest_digest": memory_digest, "source_instance": "source-1",
            "source_lineage_id": "lineage-1", "provenance_refs": ["synthetic:phase5.4.2"],
            "created_at": "2099-01-01T00:00:00+00:00",
        },
        "memory_artifact": memory, "project_state_reference": project,
        "authorization_record": {"authorization_id": "handoff-auth-1", "status": "approved", "source_lineage_id": "lineage-1", "target_environment": "development"},
        "verification_result": {"verification_id": "prior-1", "result": "pending"},
    }
    manifest = {
        "schema_version": 1, "manifest_id": "continuity-1",
        "identity_reference": {"identity_id": "identity-anima-1", "lineage_id": "lineage-1", "artifact_version": "identity-v1", "digest": identity_digest},
        "memory_artifact_reference": {"artifact_id": "synthetic-memory-1", "digest": memory_digest, "schema_version": 1, "source_lineage_id": "lineage-1"},
        "project_state_reference": {"source_commit": "rc-commit-1", "source_version": "0.3.0rc2"},
        "foundation_state": "FROZEN",
        "verification_metadata": {"verifier_version": "phase4.4-v1", "verified_at": "2099-01-01T00:00:00+00:00", "policy": {"mode": "offline-read-only"}},
        "handoff_reference": {"handoff_id": "handoff-1", "verification_id": "verification-1"},
        "authorization_reference": "auth-1", "created_at": "2099-01-01T00:00:00+00:00",
    }
    manifest_digest = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    bundle = {
        "attestation": {
            "schema_version": 1, "attestation_id": "attestation-1", "instance_id": "target-instance-1",
            "identity_reference": {"identity_id": "identity-anima-1", "lineage_id": "lineage-1", "digest": identity_digest},
            "continuity_manifest_reference": {"manifest_id": "continuity-1", "digest": manifest_digest},
            "source_version": "0.3.0rc2", "source_commit": "rc-commit-1", "foundation_state": "FROZEN",
            "verification_timestamp": "2099-01-01T00:00:00+00:00", "expires_at": "2099-01-02T00:00:00+00:00",
            "verification_result": {"identity_match": True, "manifest_match": True, "version_match": True, "foundation_compatible": True, "memory_continuity_verified": True, "handoff_verified": True, "errors": [], "warnings": [], "stop": False},
        },
        "identity_manifest": identity, "memory_artifact": memory, "continuity_manifest": manifest,
        "source_instance": {"source_identity_id": "identity-anima-1", "source_lineage_id": "lineage-1", "source_version": "0.3.0rc2", "source_manifest_digest": identity_digest},
        "target_instance": {"target_identity_id": "identity-anima-1", "target_lineage_id": "lineage-1", "target_environment": "development"},
        "handoff_artifact": handoff, "project_state": project,
    }
    return bundle


def _preflight(bundle=None):
    bundle = bundle or _base_bundle()
    archive = b"synthetic-release-archive"
    return {
        "release_candidate": {
            "commit": "rc-commit-1",
            "archive_sha256": hashlib.sha256(archive).hexdigest(),
            "attestation_bundle_sha256": hashlib.sha256(json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        },
        "attestation_bundle": bundle,
        "rollback_metadata": {
            "previous_commit": "previous-commit-1",
            "previous_archive_sha256": "a" * 64,
            "procedure_ref": "release-rollback-v1",
        },
    }, archive


def _clock():
    return datetime(2099, 1, 1, 12, tzinfo=timezone.utc)


def test_valid_preflight_passes():
    preflight, archive = _preflight()
    result = verify_release_preflight(preflight, archive, clock=_clock)
    assert result["stop"] is False
    assert result["preflight_valid"] is True


def test_commit_mismatch_stops():
    preflight, archive = _preflight()
    preflight["release_candidate"]["commit"] = "other-commit"
    assert verify_release_preflight(preflight, archive, clock=_clock)["stop"] is True


def test_archive_digest_mismatch_stops():
    preflight, archive = _preflight()
    preflight["release_candidate"]["archive_sha256"] = "0" * 64
    assert verify_release_preflight(preflight, archive, clock=_clock)["stop"] is True


def test_identity_mismatch_stops():
    preflight, archive = _preflight()
    preflight["attestation_bundle"]["attestation"]["identity_reference"]["identity_id"] = "other"
    assert verify_release_preflight(preflight, archive, clock=_clock)["stop"] is True


def test_memory_digest_mismatch_stops():
    preflight, archive = _preflight()
    preflight["attestation_bundle"]["memory_artifact"]["records"][0]["content"] = "tampered"
    assert verify_release_preflight(preflight, archive, clock=_clock)["stop"] is True


def test_handoff_mismatch_stops():
    preflight, archive = _preflight()
    preflight["attestation_bundle"]["handoff_artifact"]["authorization_record"]["status"] = "rejected"
    assert verify_release_preflight(preflight, archive, clock=_clock)["stop"] is True


def test_foundation_mismatch_stops():
    preflight, archive = _preflight()
    preflight["attestation_bundle"]["attestation"]["foundation_state"] = "EVOLVING"
    assert verify_release_preflight(preflight, archive, clock=_clock)["stop"] is True


def test_expired_attestation_stops():
    preflight, archive = _preflight()
    preflight["attestation_bundle"]["attestation"]["expires_at"] = "2099-01-01T12:00:00+00:00"
    assert verify_release_preflight(preflight, archive, clock=_clock)["stop"] is True


def test_malformed_expiry_stops():
    preflight, archive = _preflight()
    preflight["attestation_bundle"]["attestation"]["expires_at"] = "bad"
    assert verify_release_preflight(preflight, archive, clock=_clock)["stop"] is True


def test_missing_expiry_stops():
    preflight, archive = _preflight()
    del preflight["attestation_bundle"]["attestation"]["expires_at"]
    assert verify_release_preflight(preflight, archive, clock=_clock)["stop"] is True


def test_preflight_does_not_mutate_input():
    preflight, archive = _preflight()
    before = copy.deepcopy(preflight)
    verify_release_preflight(preflight, archive, clock=_clock)
    assert preflight == before


def test_non_synthetic_memory_provenance_stops():
    preflight, archive = _preflight()
    preflight["attestation_bundle"]["memory_artifact"]["records"][0]["source"] = "real-user-chat"
    result = verify_release_preflight(preflight, archive, clock=_clock)
    assert result["stop"] is True
    assert result["security_clean"] is True
    assert any("memory_provenance" in error for error in result["errors"])


def test_credential_material_stops():
    preflight, archive = _preflight()
    preflight["attestation_bundle"]["handoff_artifact"]["authorization_record"]["authorization_id"] = "ssh-rsa AAAA"
    result = verify_release_preflight(preflight, archive, clock=_clock)
    assert result["stop"] is True
    assert result["security_clean"] is False
