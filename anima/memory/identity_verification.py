"""Offline, read-only Identity Continuity verification.

This module is deliberately not imported by Anima startup or runtime paths. It
validates an explicit Identity Manifest against a read-only Memory artifact and
an explicit project-state snapshot. It never repairs, merges, writes, migrates,
or grants authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from .continuity import MemoryArtifact, MemoryContractError


IDENTITY_MANIFEST_SCHEMA_VERSION = 1
FOUNDATION_FROZEN = "FROZEN"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

REQUIRED_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "identity_id",
        "lineage_id",
        "source_instance",
        "source_commit",
        "source_version",
        "foundation_state",
        "identity_artifact_version",
        "persona_config_digest",
        "memory_manifest_digest",
        "created_at",
        "verification_policy",
    }
)
REQUIRED_PROJECT_FIELDS = frozenset(
    {
        "identity_id",
        "lineage_id",
        "source_instance",
        "source_commit",
        "source_version",
        "foundation_state",
        "persona_config_digest",
        "memory_manifest_digest",
    }
)


class IdentityVerificationError(ValueError):
    """Raised when an Identity Manifest violates its input contract."""


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IdentityVerificationError(f"{field} must be a non-empty string")
    return value


def _timestamp(value: Any) -> str:
    value = _required_string(value, "created_at")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IdentityVerificationError("created_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise IdentityVerificationError("created_at must include a timezone")
    return value


def _digest(value: Any, field: str) -> str:
    value = _required_string(value, field).lower()
    if not _DIGEST_RE.fullmatch(value):
        raise IdentityVerificationError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _strict_fields(raw: Mapping[str, Any], required: frozenset[str], label: str) -> None:
    missing = required - set(raw)
    extra = set(raw) - required
    if missing:
        raise IdentityVerificationError(f"{label} missing fields: {sorted(missing)}")
    if extra:
        raise IdentityVerificationError(f"{label} has unknown fields: {sorted(extra)}")


@dataclass(frozen=True, slots=True)
class IdentityManifest:
    schema_version: int
    identity_id: str
    lineage_id: str
    source_instance: str
    source_commit: str
    source_version: str
    foundation_state: str
    identity_artifact_version: str
    persona_config_digest: str
    memory_manifest_digest: str
    created_at: str
    verification_policy: Mapping[str, Any]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "IdentityManifest":
        if not isinstance(raw, Mapping):
            raise IdentityVerificationError("identity manifest must be an object")
        _strict_fields(raw, REQUIRED_MANIFEST_FIELDS, "identity manifest")
        if raw["schema_version"] != IDENTITY_MANIFEST_SCHEMA_VERSION:
            raise IdentityVerificationError(
                f"unsupported identity manifest schema_version: {raw['schema_version']!r}"
            )
        if not isinstance(raw["verification_policy"], Mapping):
            raise IdentityVerificationError("verification_policy must be an object")
        policy = dict(raw["verification_policy"])
        if policy.get("required_foundation_state") != FOUNDATION_FROZEN:
            raise IdentityVerificationError(
                "verification_policy.required_foundation_state must be FROZEN"
            )
        if not isinstance(policy.get("require_identity_category"), bool):
            raise IdentityVerificationError(
                "verification_policy.require_identity_category must be boolean"
            )
        return cls(
            schema_version=raw["schema_version"],
            identity_id=_required_string(raw["identity_id"], "identity_id"),
            lineage_id=_required_string(raw["lineage_id"], "lineage_id"),
            source_instance=_required_string(raw["source_instance"], "source_instance"),
            source_commit=_required_string(raw["source_commit"], "source_commit"),
            source_version=_required_string(raw["source_version"], "source_version"),
            foundation_state=_required_string(raw["foundation_state"], "foundation_state"),
            identity_artifact_version=_required_string(
                raw["identity_artifact_version"], "identity_artifact_version"
            ),
            persona_config_digest=_digest(raw["persona_config_digest"], "persona_config_digest"),
            memory_manifest_digest=_digest(
                raw["memory_manifest_digest"], "memory_manifest_digest"
            ),
            created_at=_timestamp(raw["created_at"]),
            verification_policy=policy,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "identity_id": self.identity_id,
            "lineage_id": self.lineage_id,
            "source_instance": self.source_instance,
            "source_commit": self.source_commit,
            "source_version": self.source_version,
            "foundation_state": self.foundation_state,
            "identity_artifact_version": self.identity_artifact_version,
            "persona_config_digest": self.persona_config_digest,
            "memory_manifest_digest": self.memory_manifest_digest,
            "created_at": self.created_at,
            "verification_policy": dict(self.verification_policy),
        }


def _memory_mapping(artifact: MemoryArtifact) -> dict[str, Any]:
    return {
        "schema_version": artifact.schema_version,
        "records": [record.to_mapping() for record in artifact.records],
    }


def memory_artifact_digest(artifact: MemoryArtifact) -> str:
    canonical = json.dumps(
        _memory_mapping(artifact), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _invalid_result(errors: list[str], warnings: list[str] | None = None) -> dict[str, Any]:
    return {
        "identity_match": False,
        "lineage_match": False,
        "artifact_integrity": False,
        "foundation_compatibility": False,
        "errors": errors,
        "warnings": warnings or [],
        "stop": True,
    }


def verify_identity_continuity(
    manifest: Mapping[str, Any] | IdentityManifest,
    memory_artifact: Mapping[str, Any] | MemoryArtifact,
    project_state: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify identity continuity without side effects.

    Any failed verification rule returns ``stop=True``. No branch attempts to
    choose a replacement identity or repair an artifact.
    """

    errors: list[str] = []
    warnings: list[str] = []
    try:
        identity = (
            manifest
            if isinstance(manifest, IdentityManifest)
            else IdentityManifest.from_mapping(manifest)
        )
    except (IdentityVerificationError, TypeError) as exc:
        return _invalid_result([f"manifest_invalid: {exc}"])

    if not isinstance(project_state, Mapping):
        return _invalid_result(["project_state_invalid: project state must be an object"])
    try:
        _strict_fields(project_state, REQUIRED_PROJECT_FIELDS, "project state")
        for field in REQUIRED_PROJECT_FIELDS - {"persona_config_digest", "memory_manifest_digest"}:
            _required_string(project_state[field], f"project_state.{field}")
        _digest(project_state["persona_config_digest"], "project_state.persona_config_digest")
        _digest(project_state["memory_manifest_digest"], "project_state.memory_manifest_digest")
    except (IdentityVerificationError, TypeError) as exc:
        return _invalid_result([f"project_state_invalid: {exc}"])

    identity_match = (
        identity.identity_id == project_state["identity_id"]
        and identity.source_instance == project_state["source_instance"]
    )
    lineage_match = identity.lineage_id == project_state["lineage_id"]
    if not identity_match:
        errors.append("identity_mismatch: identity_id or source_instance differs")
    if not lineage_match:
        errors.append("lineage_mismatch: lineage_id differs")

    if identity.source_commit != project_state["source_commit"]:
        errors.append("source_mismatch: source_commit differs")
    if identity.source_version != project_state["source_version"]:
        errors.append("version_mismatch: source_version differs")

    foundation_compatibility = (
        identity.foundation_state == project_state["foundation_state"] == FOUNDATION_FROZEN
        and identity.verification_policy["required_foundation_state"] == FOUNDATION_FROZEN
    )
    if not foundation_compatibility:
        errors.append("foundation_incompatible: required state is FROZEN")

    persona_match = identity.persona_config_digest == project_state["persona_config_digest"]
    if not persona_match:
        errors.append("persona_digest_mismatch: persona_config_digest differs")

    try:
        artifact = (
            memory_artifact
            if isinstance(memory_artifact, MemoryArtifact)
            else MemoryArtifact.from_mapping(memory_artifact)
        )
    except (MemoryContractError, TypeError, KeyError) as exc:
        return {
            **_invalid_result([f"memory_artifact_invalid: {exc}"], warnings),
            "identity_match": identity_match,
            "lineage_match": lineage_match,
            "foundation_compatibility": foundation_compatibility,
        }

    actual_memory_digest = memory_artifact_digest(artifact)
    memory_digest_match = actual_memory_digest == identity.memory_manifest_digest
    project_memory_digest_match = (
        actual_memory_digest == project_state["memory_manifest_digest"]
    )
    if not memory_digest_match:
        errors.append("memory_digest_mismatch: manifest digest differs")
    if not project_memory_digest_match:
        errors.append("memory_project_digest_mismatch: project state digest differs")

    if identity.verification_policy["require_identity_category"] and not artifact.for_category("identity"):
        errors.append("identity_category_missing: artifact has no identity records")
    elif not artifact.for_category("identity"):
        warnings.append("identity_category_empty: artifact has no identity records")

    artifact_integrity = not any(
        error.startswith(("memory_", "persona_digest_mismatch")) for error in errors
    )
    if errors:
        return {
            "identity_match": identity_match,
            "lineage_match": lineage_match,
            "artifact_integrity": artifact_integrity,
            "foundation_compatibility": foundation_compatibility,
            "errors": errors,
            "warnings": warnings,
            "stop": True,
        }
    return {
        "identity_match": True,
        "lineage_match": True,
        "artifact_integrity": True,
        "foundation_compatibility": True,
        "errors": [],
        "warnings": warnings,
        "stop": False,
    }


__all__ = [
    "IDENTITY_MANIFEST_SCHEMA_VERSION",
    "IdentityManifest",
    "IdentityVerificationError",
    "memory_artifact_digest",
    "verify_identity_continuity",
]
