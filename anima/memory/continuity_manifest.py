"""Offline validation for the Continuity Manifest.

The manifest is a reference envelope.  It contains no Memory content and does
not replace Identity, Memory, or Handoff contracts.  This module has no runtime
or persistence integration and never repairs or migrates artifacts.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Mapping

from .continuity import MemoryArtifact, MemoryContractError
from .identity_verification import IdentityManifest, IdentityVerificationError, memory_artifact_digest


MANIFEST_SCHEMA_VERSION = 1
FOUNDATION_FROZEN = "FROZEN"
MANIFEST_FIELDS = frozenset({
    "schema_version", "manifest_id", "identity_reference", "memory_artifact_reference",
    "project_state_reference", "foundation_state", "verification_metadata",
    "handoff_reference", "authorization_reference", "created_at",
})
IDENTITY_REFERENCE_FIELDS = frozenset({"identity_id", "lineage_id", "artifact_version", "digest"})
MEMORY_REFERENCE_FIELDS = frozenset({"artifact_id", "digest", "schema_version", "source_lineage_id"})
PROJECT_REFERENCE_FIELDS = frozenset({"source_commit", "source_version"})
VERIFICATION_FIELDS = frozenset({"verifier_version", "verified_at", "policy"})
HANDOFF_REFERENCE_FIELDS = frozenset({"handoff_id", "verification_id"})


def _strict(raw: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    missing = expected - set(raw)
    extra = set(raw) - expected
    if missing:
        raise ValueError(f"{label} missing fields: {sorted(missing)}")
    if extra:
        raise ValueError(f"{label} has unknown fields: {sorted(extra)}")


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _digest(value: Any, field: str) -> str:
    value = _text(value, field).lower()
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{field} must be a SHA-256 digest")
    return value


def _time(value: Any, field: str) -> None:
    value = _text(value, field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")


def _identity_digest(identity: IdentityManifest) -> str:
    canonical = json.dumps(
        identity.to_mapping(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _stop(errors: list[str], warnings: list[str] | None = None) -> dict[str, Any]:
    return {
        "manifest_valid": False,
        "identity_valid": False,
        "memory_digest_match": False,
        "handoff_valid": False,
        "project_version_match": False,
        "foundation_compatible": False,
        "authorization_reference_present": False,
        "errors": errors,
        "warnings": warnings or [],
        "stop": True,
    }


def verify_continuity_manifest(
    manifest: Mapping[str, Any],
    identity_artifact: Mapping[str, Any] | IdentityManifest,
    memory_artifact: Mapping[str, Any] | MemoryArtifact,
    handoff_verification: Mapping[str, Any],
    project_state: Mapping[str, Any],
    authorization_reference: str | None = None,
) -> dict[str, Any]:
    """Verify a reference-only Continuity Manifest without side effects."""

    errors: list[str] = []
    warnings: list[str] = []
    try:
        _strict(manifest, MANIFEST_FIELDS, "continuity manifest")
        if manifest["schema_version"] != MANIFEST_SCHEMA_VERSION:
            raise ValueError("unsupported continuity manifest schema_version")
        _text(manifest["manifest_id"], "manifest_id")
        _time(manifest["created_at"], "created_at")
        _text(manifest["foundation_state"], "foundation_state")
        identity_ref = manifest["identity_reference"]
        memory_ref = manifest["memory_artifact_reference"]
        project_ref = manifest["project_state_reference"]
        verification = manifest["verification_metadata"]
        handoff_ref = manifest["handoff_reference"]
        if not isinstance(identity_ref, Mapping):
            raise ValueError("identity_reference must be an object")
        if not isinstance(memory_ref, Mapping):
            raise ValueError("memory_artifact_reference must be an object")
        if not isinstance(project_ref, Mapping):
            raise ValueError("project_state_reference must be an object")
        if not isinstance(verification, Mapping):
            raise ValueError("verification_metadata must be an object")
        if not isinstance(handoff_ref, Mapping):
            raise ValueError("handoff_reference must be an object")
        _strict(identity_ref, IDENTITY_REFERENCE_FIELDS, "identity_reference")
        _strict(memory_ref, MEMORY_REFERENCE_FIELDS, "memory_artifact_reference")
        _strict(project_ref, PROJECT_REFERENCE_FIELDS, "project_state_reference")
        _strict(verification, VERIFICATION_FIELDS, "verification_metadata")
        _strict(handoff_ref, HANDOFF_REFERENCE_FIELDS, "handoff_reference")
        _text(manifest["authorization_reference"], "authorization_reference")
        _text(verification["verifier_version"], "verification_metadata.verifier_version")
        _time(verification["verified_at"], "verification_metadata.verified_at")
        if not isinstance(verification["policy"], Mapping):
            raise ValueError("verification_metadata.policy must be an object")
        _text(handoff_ref["handoff_id"], "handoff_reference.handoff_id")
        _text(handoff_ref["verification_id"], "handoff_reference.verification_id")
        _digest(identity_ref["digest"], "identity_reference.digest")
        _digest(memory_ref["digest"], "memory_artifact_reference.digest")
    except (TypeError, ValueError) as exc:
        return _stop([f"manifest_invalid: {exc}"])

    try:
        identity = identity_artifact if isinstance(identity_artifact, IdentityManifest) else IdentityManifest.from_mapping(identity_artifact)
    except (IdentityVerificationError, TypeError, ValueError) as exc:
        return _stop([f"identity_invalid: {exc}"])

    identity_valid = (
        _identity_digest(identity) == manifest["identity_reference"]["digest"]
        and identity.identity_id == manifest["identity_reference"]["identity_id"]
        and identity.lineage_id == manifest["identity_reference"]["lineage_id"]
        and identity.identity_artifact_version == manifest["identity_reference"]["artifact_version"]
    )
    if not identity_valid:
        errors.append("identity_reference_mismatch: artifact does not match reference")

    try:
        memory = memory_artifact if isinstance(memory_artifact, MemoryArtifact) else MemoryArtifact.from_mapping(memory_artifact)
        actual_memory_digest = memory_artifact_digest(memory)
    except (MemoryContractError, TypeError, ValueError) as exc:
        return _stop([*errors, f"memory_invalid: {exc}"])

    memory_digest_match = (
        actual_memory_digest == manifest["memory_artifact_reference"]["digest"]
        == identity.memory_manifest_digest
    )
    if not memory_digest_match:
        errors.append("memory_digest_mismatch: artifact digest does not match references")

    project_version_match = (
        isinstance(project_state, Mapping)
        and project_state.get("source_commit") == manifest["project_state_reference"]["source_commit"]
        and project_state.get("source_version") == manifest["project_state_reference"]["source_version"]
        and identity.source_commit == manifest["project_state_reference"]["source_commit"]
        and identity.source_version == manifest["project_state_reference"]["source_version"]
    )
    if not project_version_match:
        errors.append("project_version_mismatch: project state differs from references")

    foundation_compatible = (
        manifest["foundation_state"] == identity.foundation_state == FOUNDATION_FROZEN
        and isinstance(project_state, Mapping)
        and project_state.get("foundation_state") == FOUNDATION_FROZEN
    )
    if not foundation_compatible:
        errors.append("foundation_incompatible: Foundation state is not FROZEN-compatible")

    handoff_fields = {
        "source_identity_valid", "target_eligible", "identity_manifest_consistent",
        "memory_digest_consistent", "artifact_provenance_complete",
        "authorization_present", "foundation_compatible", "errors", "stop",
    }
    handoff_valid = (
        isinstance(handoff_verification, Mapping)
        and handoff_fields <= set(handoff_verification)
        and all(handoff_verification.get(field) is True for field in (
            "source_identity_valid", "target_eligible", "identity_manifest_consistent",
            "memory_digest_consistent", "artifact_provenance_complete",
            "authorization_present", "foundation_compatible",
        ))
        and handoff_verification.get("stop") is False
        and handoff_verification.get("errors") == []
    )
    if not handoff_valid:
        errors.append("handoff_invalid: handoff verification is not a successful result")

    authorization_present = bool(_text(manifest["authorization_reference"], "authorization_reference"))
    if authorization_reference is not None and authorization_reference != manifest["authorization_reference"]:
        authorization_present = False
        errors.append("authorization_reference_mismatch: supplied reference differs")
    if not authorization_present:
        errors.append("authorization_reference_missing: no matching reference")

    if errors:
        return {
            "manifest_valid": False,
            "identity_valid": identity_valid,
            "memory_digest_match": memory_digest_match,
            "handoff_valid": handoff_valid,
            "project_version_match": project_version_match,
            "foundation_compatible": foundation_compatible,
            "authorization_reference_present": authorization_present,
            "errors": errors,
            "warnings": warnings,
            "stop": True,
        }
    return {
        "manifest_valid": True,
        "identity_valid": True,
        "memory_digest_match": True,
        "handoff_valid": True,
        "project_version_match": True,
        "foundation_compatible": True,
        "authorization_reference_present": True,
        "errors": [],
        "warnings": warnings,
        "stop": False,
    }


__all__ = ["verify_continuity_manifest"]
