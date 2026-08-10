"""Opt-in, offline Runtime Identity Attestation verification.

This module composes the Phase 4 Identity, Handoff, and Continuity Manifest
verifiers. It is intentionally not imported by Anima startup unless the
explicit ``--identity-attestation`` option is supplied. It never writes,
migrates, repairs, restores, or grants authority.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .continuity_manifest import verify_continuity_manifest
from .handoff_verification import verify_instance_handoff
from .identity_verification import verify_identity_continuity


ATTESTATION_SCHEMA_VERSION = 1
ATTESTATION_FIELDS = frozenset({
    "schema_version", "attestation_id", "instance_id", "identity_reference",
    "continuity_manifest_reference", "source_version", "foundation_state",
    "source_commit", "verification_timestamp", "verification_result",
})
IDENTITY_REFERENCE_FIELDS = frozenset({"identity_id", "lineage_id", "digest"})
MANIFEST_REFERENCE_FIELDS = frozenset({"manifest_id", "digest"})
RESULT_FIELDS = frozenset({
    "identity_match", "manifest_match", "version_match", "foundation_compatible",
    "memory_continuity_verified", "handoff_verified", "errors", "warnings", "stop",
})
BUNDLE_FIELDS = frozenset({
    "attestation", "identity_manifest", "memory_artifact", "continuity_manifest",
    "source_instance", "target_instance", "handoff_artifact", "project_state",
})


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _digest(value: Any, field: str) -> str:
    value = _text(value, field).lower()
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{field} must be a SHA-256 digest")
    return value


def _timestamp(value: Any, field: str) -> None:
    value = _text(value, field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")


def _strict(raw: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    missing = expected - set(raw)
    extra = set(raw) - expected
    if missing:
        raise ValueError(f"{label} missing fields: {sorted(missing)}")
    if extra:
        raise ValueError(f"{label} has unknown fields: {sorted(extra)}")


def _canonical_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _stop(errors: list[str], warnings: list[str] | None = None) -> dict[str, Any]:
    return {
        "identity_match": False,
        "manifest_match": False,
        "version_match": False,
        "foundation_compatible": False,
        "memory_continuity_verified": False,
        "handoff_verified": False,
        "errors": errors,
        "warnings": warnings or [],
        "stop": True,
    }


@dataclass(frozen=True, slots=True)
class RuntimeIdentityContext:
    """Immutable, observation-only identity context for one running Core."""

    instance_id: str
    identity_id: str
    lineage_id: str
    source_commit: str
    source_version: str
    attestation_status: str

    def __post_init__(self) -> None:
        if self.attestation_status != "PASS":
            raise ValueError("runtime identity context requires PASS attestation")

    def as_mapping(self) -> dict[str, str]:
        return {
            "instance_id": self.instance_id,
            "identity_id": self.identity_id,
            "lineage_id": self.lineage_id,
            "source_commit": self.source_commit,
            "source_version": self.source_version,
            "attestation_status": self.attestation_status,
        }


def verify_runtime_identity_attestation(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Verify one explicit attestation bundle without side effects."""

    try:
        _strict(bundle, BUNDLE_FIELDS, "attestation bundle")
        attestation = bundle["attestation"]
        identity_ref = attestation["identity_reference"]
        manifest_ref = attestation["continuity_manifest_reference"]
        result = attestation["verification_result"]
        _strict(attestation, ATTESTATION_FIELDS, "runtime attestation")
        _strict(identity_ref, IDENTITY_REFERENCE_FIELDS, "identity_reference")
        _strict(manifest_ref, MANIFEST_REFERENCE_FIELDS, "continuity_manifest_reference")
        _strict(result, RESULT_FIELDS, "verification_result")
        if attestation["schema_version"] != ATTESTATION_SCHEMA_VERSION:
            raise ValueError("unsupported attestation schema_version")
        _text(attestation["attestation_id"], "attestation_id")
        _text(attestation["instance_id"], "instance_id")
        _text(attestation["source_version"], "source_version")
        _text(attestation["foundation_state"], "foundation_state")
        _text(attestation["source_commit"], "source_commit")
        _timestamp(attestation["verification_timestamp"], "verification_timestamp")
        for field in identity_ref:
            _text(identity_ref[field], f"identity_reference.{field}")
        _digest(identity_ref["digest"], "identity_reference.digest")
        _text(manifest_ref["manifest_id"], "continuity_manifest_reference.manifest_id")
        _digest(manifest_ref["digest"], "continuity_manifest_reference.digest")
        if not isinstance(result["errors"], list) or not isinstance(result["warnings"], list):
            raise ValueError("verification_result errors and warnings must be lists")
    except (TypeError, KeyError, ValueError) as exc:
        return _stop([f"attestation_invalid: {exc}"])

    identity_manifest = bundle["identity_manifest"]
    continuity_manifest = bundle["continuity_manifest"]
    project_state = bundle["project_state"]
    manifest_project_ref = (
        continuity_manifest.get("project_state_reference")
        if isinstance(continuity_manifest, Mapping)
        else None
    )
    identity_match = (
        isinstance(identity_manifest, Mapping)
        and identity_manifest.get("identity_id") == identity_ref["identity_id"]
        and identity_manifest.get("lineage_id") == identity_ref["lineage_id"]
        and _canonical_digest(identity_manifest) == identity_ref["digest"]
    )
    manifest_match = (
        isinstance(continuity_manifest, Mapping)
        and continuity_manifest.get("manifest_id") == manifest_ref["manifest_id"]
        and _canonical_digest(continuity_manifest) == manifest_ref["digest"]
    )
    version_match = (
        isinstance(manifest_project_ref, Mapping)
        and manifest_project_ref.get("source_commit") == attestation["source_commit"]
        and manifest_project_ref.get("source_version") == attestation["source_version"]
        and isinstance(project_state, Mapping)
        and project_state.get("source_commit") == attestation["source_commit"]
        and project_state.get("source_version") == attestation["source_version"]
    )
    foundation_compatible = (
        attestation["foundation_state"] == "FROZEN"
        and isinstance(continuity_manifest, Mapping)
        and continuity_manifest.get("foundation_state") == "FROZEN"
        and isinstance(project_state, Mapping)
        and project_state.get("foundation_state") == "FROZEN"
    )
    errors: list[str] = []
    if not identity_match:
        errors.append("identity_mismatch: attestation identity reference differs")
    if not manifest_match:
        errors.append("manifest_mismatch: Continuity Manifest reference differs")
    if not version_match:
        errors.append("version_mismatch: source version differs")
    if not foundation_compatible:
        errors.append("foundation_incompatible: Foundation is not FROZEN-compatible")

    identity_result = verify_identity_continuity(
        identity_manifest, bundle["memory_artifact"], project_state
    )
    handoff_result = verify_instance_handoff(
        bundle["source_instance"], bundle["target_instance"], bundle["handoff_artifact"]
    )
    continuity_result = verify_continuity_manifest(
        continuity_manifest, identity_manifest, bundle["memory_artifact"],
        handoff_result, project_state,
    )
    memory_verified = not identity_result["stop"] and continuity_result["memory_digest_match"]
    handoff_verified = not handoff_result["stop"] and continuity_result["handoff_valid"]
    if not memory_verified:
        errors.append("memory_continuity_failed: identity or manifest verification stopped")
    if not handoff_verified:
        errors.append("handoff_failed: handoff verification stopped")

    actual = {
        "identity_match": identity_match,
        "manifest_match": manifest_match,
        "version_match": version_match,
        "foundation_compatible": foundation_compatible,
        "memory_continuity_verified": memory_verified,
        "handoff_verified": handoff_verified,
    }
    if any(result[key] is not actual[key] for key in actual):
        errors.append("verification_result_mismatch: supplied result differs from recomputed result")
    if result["errors"]:
        errors.append("verification_result_not_clean: supplied result contains errors")
    if result["stop"] is not False:
        errors.append("verification_result_stop: supplied result is not successful")
    if errors:
        return {**actual, "errors": errors, "warnings": identity_result.get("warnings", []) + handoff_result.get("warnings", []), "stop": True}
    return {**actual, "errors": [], "warnings": identity_result.get("warnings", []) + handoff_result.get("warnings", []), "stop": False}


def verify_runtime_identity_attestation_file(path: str | Path) -> dict[str, Any]:
    """Read one explicitly named local JSON bundle and verify it read-only."""
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            bundle = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        return _stop([f"attestation_input_invalid: {exc}"])
    return verify_runtime_identity_attestation(bundle)


def bind_runtime_identity_context(
    bundle: Mapping[str, Any],
    expected_instance_id: str | None = None,
) -> tuple[RuntimeIdentityContext | None, dict[str, Any]]:
    """Create context only from a successful, explicit attestation result."""

    result = verify_runtime_identity_attestation(bundle)
    if result["stop"]:
        return None, result
    attestation = bundle["attestation"]
    if expected_instance_id is not None and attestation["instance_id"] != expected_instance_id:
        return None, {
            **result,
            "errors": ["instance_id_mismatch: attestation differs from runtime instance"],
            "stop": True,
        }
    identity_ref = attestation["identity_reference"]
    context = RuntimeIdentityContext(
        instance_id=attestation["instance_id"],
        identity_id=identity_ref["identity_id"],
        lineage_id=identity_ref["lineage_id"],
        source_commit=attestation["source_commit"],
        source_version=attestation["source_version"],
        attestation_status="PASS",
    )
    return context, result


def bind_runtime_identity_context_file(
    path: str | Path,
    expected_instance_id: str | None = None,
) -> tuple[RuntimeIdentityContext | None, dict[str, Any]]:
    """Read and bind one explicit bundle, without persistence side effects."""

    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            bundle = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        return None, _stop([f"attestation_input_invalid: {exc}"])
    return bind_runtime_identity_context(bundle, expected_instance_id=expected_instance_id)


__all__ = [
    "RuntimeIdentityContext",
    "bind_runtime_identity_context",
    "bind_runtime_identity_context_file",
    "verify_runtime_identity_attestation",
    "verify_runtime_identity_attestation_file",
]
