"""Offline, read-only Anima instance handoff verification.

The verifier consumes explicit source/target descriptors and handoff artifacts.
It never migrates, merges, overwrites, starts, or authorizes an instance.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Mapping

from .continuity import MemoryArtifact, MemoryContractError
from .identity_verification import (
    IdentityManifest,
    IdentityVerificationError,
    memory_artifact_digest,
    verify_identity_continuity,
)


SOURCE_FIELDS = frozenset(
    {"source_identity_id", "source_lineage_id", "source_version", "source_manifest_digest"}
)
TARGET_FIELDS = frozenset({"target_identity_id", "target_lineage_id", "target_environment"})
HANDOFF_FIELDS = frozenset(
    {
        "identity_manifest",
        "memory_artifact_manifest",
        "memory_artifact",
        "project_state_reference",
        "authorization_record",
        "verification_result",
    }
)
MEMORY_MANIFEST_FIELDS = frozenset(
    {
        "artifact_id",
        "schema_version",
        "memory_manifest_digest",
        "source_instance",
        "source_lineage_id",
        "provenance_refs",
        "created_at",
    }
)
AUTH_FIELDS = frozenset(
    {"authorization_id", "status", "source_lineage_id", "target_environment"}
)
ALLOWED_TARGET_ENVIRONMENTS = frozenset({"local", "development", "staging"})


def _strict_fields(raw: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    missing = expected - set(raw)
    extra = set(raw) - expected
    if missing:
        raise ValueError(f"{label} missing fields: {sorted(missing)}")
    if extra:
        raise ValueError(f"{label} has unknown fields: {sorted(extra)}")


def _nonempty(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _digest(value: Any, field: str) -> str:
    value = _nonempty(value, field).lower()
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{field} must be a SHA-256 digest")
    return value


def _timestamp(value: Any, field: str) -> None:
    value = _nonempty(value, field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")


def _manifest_digest(manifest: IdentityManifest) -> str:
    canonical = json.dumps(
        manifest.to_mapping(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _empty_result(errors: list[str], warnings: list[str] | None = None) -> dict[str, Any]:
    return {
        "source_identity_valid": False,
        "target_eligible": False,
        "identity_manifest_consistent": False,
        "memory_digest_consistent": False,
        "artifact_provenance_complete": False,
        "authorization_present": False,
        "foundation_compatible": False,
        "errors": errors,
        "warnings": warnings or [],
        "stop": True,
    }


def verify_instance_handoff(
    source_instance: Mapping[str, Any],
    target_instance: Mapping[str, Any],
    handoff_artifact: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify one explicit handoff bundle without side effects."""

    errors: list[str] = []
    warnings: list[str] = []
    try:
        _strict_fields(source_instance, SOURCE_FIELDS, "source_instance")
        _strict_fields(target_instance, TARGET_FIELDS, "target_instance")
        _strict_fields(handoff_artifact, HANDOFF_FIELDS, "handoff_artifact")
        for field in ("source_identity_id", "source_lineage_id", "source_version"):
            _nonempty(source_instance[field], f"source_instance.{field}")
        _digest(source_instance["source_manifest_digest"], "source_instance.source_manifest_digest")
        for field in ("target_identity_id", "target_lineage_id", "target_environment"):
            _nonempty(target_instance[field], f"target_instance.{field}")
    except (TypeError, ValueError) as exc:
        return _empty_result([f"handoff_contract_invalid: {exc}"])

    try:
        identity_manifest = IdentityManifest.from_mapping(handoff_artifact["identity_manifest"])
        source_manifest_digest = _manifest_digest(identity_manifest)
    except (IdentityVerificationError, TypeError, ValueError) as exc:
        return _empty_result([f"identity_manifest_invalid: {exc}"])

    source_identity_valid = (
        identity_manifest.identity_id == source_instance["source_identity_id"]
        and identity_manifest.lineage_id == source_instance["source_lineage_id"]
        and identity_manifest.source_version == source_instance["source_version"]
        and source_manifest_digest == source_instance["source_manifest_digest"]
    )
    if not source_identity_valid:
        errors.append("source_identity_invalid: source descriptor does not match manifest")

    target_eligible = (
        target_instance["target_identity_id"] == identity_manifest.identity_id
        and target_instance["target_lineage_id"] == identity_manifest.lineage_id
        and target_instance["target_environment"] in ALLOWED_TARGET_ENVIRONMENTS
    )
    if target_instance["target_environment"] not in ALLOWED_TARGET_ENVIRONMENTS:
        errors.append("target_ineligible: target environment is not an offline handoff environment")
    if target_instance["target_identity_id"] != identity_manifest.identity_id:
        errors.append("target_identity_mismatch: target identity differs")
    if target_instance["target_lineage_id"] != identity_manifest.lineage_id:
        errors.append("target_lineage_mismatch: target lineage differs")

    memory_manifest = handoff_artifact["memory_artifact_manifest"]
    identity_manifest_consistent = (
        isinstance(memory_manifest, Mapping)
        and memory_manifest.get("source_instance") == identity_manifest.source_instance
    )
    if not identity_manifest_consistent:
        errors.append("identity_manifest_inconsistent: source instance binding differs")

    memory_digest_consistent = False
    artifact_provenance_complete = False
    try:
        _strict_fields(memory_manifest, MEMORY_MANIFEST_FIELDS, "memory_artifact_manifest")
        _nonempty(memory_manifest["artifact_id"], "memory_artifact_manifest.artifact_id")
        _digest(memory_manifest["memory_manifest_digest"], "memory_artifact_manifest.memory_manifest_digest")
        _nonempty(memory_manifest["source_instance"], "memory_artifact_manifest.source_instance")
        _nonempty(memory_manifest["source_lineage_id"], "memory_artifact_manifest.source_lineage_id")
        _timestamp(memory_manifest["created_at"], "memory_artifact_manifest.created_at")
        provenance = memory_manifest["provenance_refs"]
        artifact_provenance_complete = isinstance(provenance, list) and bool(provenance) and all(
            isinstance(item, str) and item.strip() for item in provenance
        )
        if not artifact_provenance_complete:
            errors.append("provenance_incomplete: provenance_refs must be non-empty")
        if memory_manifest["source_instance"] != identity_manifest.source_instance:
            errors.append("provenance_source_mismatch: source_instance differs")
        if memory_manifest["source_lineage_id"] != identity_manifest.lineage_id:
            errors.append("provenance_lineage_mismatch: source_lineage_id differs")

        memory = MemoryArtifact.from_mapping(handoff_artifact["memory_artifact"])
        actual_digest = memory_artifact_digest(memory)
        memory_digest_consistent = (
            actual_digest == identity_manifest.memory_manifest_digest
            == memory_manifest["memory_manifest_digest"]
        )
        if not memory_digest_consistent:
            errors.append("memory_digest_mismatch: manifest and artifact digests differ")
    except (MemoryContractError, TypeError, ValueError, KeyError) as exc:
        errors.append(f"memory_artifact_invalid: {exc}")

    source_verification = None
    try:
        source_verification = verify_identity_continuity(
            identity_manifest, handoff_artifact["memory_artifact"], handoff_artifact["project_state_reference"]
        )
        if source_verification["stop"]:
            errors.extend(f"source_verification: {error}" for error in source_verification["errors"])
    except (TypeError, KeyError) as exc:
        errors.append(f"project_state_invalid: {exc}")

    authorization_present = False
    try:
        authorization = handoff_artifact["authorization_record"]
        _strict_fields(authorization, AUTH_FIELDS, "authorization_record")
        authorization_present = (
            bool(_nonempty(authorization["authorization_id"], "authorization_id"))
            and authorization["status"] == "approved"
            and authorization["source_lineage_id"] == identity_manifest.lineage_id
            and authorization["target_environment"] == target_instance["target_environment"]
        )
        if not authorization_present:
            errors.append("authorization_invalid: missing, unapproved, or out of scope")
    except (TypeError, ValueError) as exc:
        errors.append(f"authorization_invalid: {exc}")

    foundation_compatible = bool(source_verification and source_verification["foundation_compatibility"])
    if not foundation_compatible and not any("foundation" in error for error in errors):
        errors.append("foundation_incompatible: source foundation is not compatible")

    if not isinstance(handoff_artifact["verification_result"], Mapping):
        errors.append("verification_result_invalid: verification result must be an object")
    else:
        warnings.append("verification_result_is_untrusted_input: result was recomputed")

    return {
        "source_identity_valid": source_identity_valid,
        "target_eligible": target_eligible,
        "identity_manifest_consistent": identity_manifest_consistent,
        "memory_digest_consistent": memory_digest_consistent,
        "artifact_provenance_complete": artifact_provenance_complete,
        "authorization_present": authorization_present,
        "foundation_compatible": foundation_compatible,
        "errors": errors,
        "warnings": warnings,
        "stop": bool(errors),
    }


__all__ = ["verify_instance_handoff"]
