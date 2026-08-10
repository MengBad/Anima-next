"""Offline Release Candidate and Runtime Attestation preflight validation."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from anima.memory.runtime_identity_attestation import verify_runtime_identity_attestation


PREFLIGHT_FIELDS = frozenset({"release_candidate", "attestation_bundle", "rollback_metadata"})
RELEASE_FIELDS = frozenset({"commit", "archive_sha256", "attestation_bundle_sha256"})
ROLLBACK_FIELDS = frozenset({"previous_commit", "previous_archive_sha256", "procedure_ref"})


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


def _canonical_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _stop(errors: list[str], warnings: list[str] | None = None) -> dict[str, Any]:
    return {
        "preflight_valid": False,
        "release_commit_match": False,
        "archive_digest_match": False,
        "identity_valid": False,
        "manifest_valid": False,
        "handoff_valid": False,
        "memory_integrity": False,
        "expiry_valid": False,
        "foundation_compatible": False,
        "bundle_integrity": False,
        "rollback_metadata_valid": False,
        "security_clean": False,
        "errors": errors,
        "warnings": warnings or [],
        "stop": True,
    }


def _security_issues(value: Any, path: str = "bundle") -> list[str]:
    issues: list[str] = []
    forbidden_key_fragments = (
        "password", "secret", "ssh", "private_key", "api_key", "credential",
        "capability_permission", "executable_payload", "script_payload",
    )
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key).lower()
            if any(fragment in key_text for fragment in forbidden_key_fragments):
                issues.append(f"security_forbidden_field: {path}.{key}")
            issues.extend(_security_issues(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            issues.extend(_security_issues(child, f"{path}[{index}]"))
    elif isinstance(value, str):
        lowered = value.lower()
        if "begin private key" in lowered or "ssh-rsa " in lowered:
            issues.append(f"security_credential_material: {path}")
    return issues


def verify_release_preflight(
    preflight: Mapping[str, Any],
    archive: bytes | bytearray | Path,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Verify an explicit release proof without writes, repairs, or network access."""

    errors: list[str] = []
    warnings: list[str] = []
    try:
        _strict(preflight, PREFLIGHT_FIELDS, "release preflight")
        release = preflight["release_candidate"]
        rollback = preflight["rollback_metadata"]
        bundle = preflight["attestation_bundle"]
        _strict(release, RELEASE_FIELDS, "release_candidate")
        _strict(rollback, ROLLBACK_FIELDS, "rollback_metadata")
        _text(release["commit"], "release_candidate.commit")
        _digest(release["archive_sha256"], "release_candidate.archive_sha256")
        _digest(release["attestation_bundle_sha256"], "release_candidate.attestation_bundle_sha256")
        _text(rollback["previous_commit"], "rollback_metadata.previous_commit")
        _digest(rollback["previous_archive_sha256"], "rollback_metadata.previous_archive_sha256")
        _text(rollback["procedure_ref"], "rollback_metadata.procedure_ref")
        if not isinstance(bundle, Mapping):
            raise ValueError("attestation_bundle must be an object")
    except (TypeError, KeyError, ValueError) as exc:
        return _stop([f"preflight_invalid: {exc}"])

    try:
        archive_bytes = archive if isinstance(archive, (bytes, bytearray)) else Path(archive).read_bytes()
        actual_archive_digest = hashlib.sha256(bytes(archive_bytes)).hexdigest()
    except (OSError, TypeError, ValueError) as exc:
        return _stop([f"archive_invalid: {exc}"])
    archive_digest_match = actual_archive_digest == release["archive_sha256"]
    if not archive_digest_match:
        errors.append("archive_digest_mismatch: release archive differs")

    actual_bundle_digest = _canonical_digest(bundle)
    bundle_integrity = actual_bundle_digest == release["attestation_bundle_sha256"]
    if not bundle_integrity:
        errors.append("bundle_digest_mismatch: attestation bundle differs")

    runtime_result = verify_runtime_identity_attestation(bundle, clock=clock)
    identity_valid = runtime_result["identity_match"]
    manifest_valid = runtime_result["manifest_match"]
    handoff_valid = runtime_result["handoff_verified"]
    memory_integrity = runtime_result["memory_continuity_verified"]
    expiry_valid = not any("expired" in error or "expires_at" in error for error in runtime_result["errors"])
    foundation_compatible = runtime_result["foundation_compatible"]
    if runtime_result["stop"]:
        errors.extend(f"attestation: {error}" for error in runtime_result["errors"])

    attestation = bundle.get("attestation")
    continuity = bundle.get("continuity_manifest")
    project = bundle.get("project_state")
    manifest_project_ref = (
        continuity.get("project_state_reference")
        if isinstance(continuity, Mapping)
        else None
    )
    release_commit_match = (
        isinstance(attestation, Mapping)
        and isinstance(manifest_project_ref, Mapping)
        and isinstance(project, Mapping)
        and release["commit"] == attestation.get("source_commit")
        == manifest_project_ref.get("source_commit")
        == project.get("source_commit")
    )
    if not release_commit_match:
        errors.append("source_commit_mismatch: release and attestation references differ")

    memory_manifest = bundle.get("handoff_artifact", {}).get("memory_artifact_manifest", {})
    records = bundle.get("memory_artifact", {}).get("records", [])
    provenance_ok = (
        isinstance(memory_manifest, Mapping)
        and all(isinstance(record, Mapping) and str(record.get("source", "")).startswith("synthetic:") for record in records)
        and all(str(ref).startswith("synthetic:") for ref in memory_manifest.get("provenance_refs", []))
    )
    if not provenance_ok:
        errors.append("memory_provenance_invalid: only synthetic provenance is allowed")
    memory_integrity = memory_integrity and provenance_ok

    security_issues = _security_issues(bundle)
    if security_issues:
        errors.extend(security_issues)
    security_clean = not security_issues
    rollback_valid = True

    if errors:
        return {
            "preflight_valid": False,
            "release_commit_match": release_commit_match,
            "archive_digest_match": archive_digest_match,
            "identity_valid": identity_valid,
            "manifest_valid": manifest_valid,
            "handoff_valid": handoff_valid,
            "memory_integrity": memory_integrity,
            "expiry_valid": expiry_valid,
            "foundation_compatible": foundation_compatible,
            "bundle_integrity": bundle_integrity,
            "rollback_metadata_valid": rollback_valid,
            "security_clean": security_clean,
            "errors": errors,
            "warnings": warnings,
            "stop": True,
        }
    return {
        "preflight_valid": True,
        "release_commit_match": True,
        "archive_digest_match": True,
        "identity_valid": True,
        "manifest_valid": True,
        "handoff_valid": True,
        "memory_integrity": True,
        "expiry_valid": True,
        "foundation_compatible": True,
        "bundle_integrity": True,
        "rollback_metadata_valid": True,
        "security_clean": True,
        "errors": [],
        "warnings": warnings,
        "stop": False,
    }


__all__ = ["verify_release_preflight"]
