"""Read-only Memory Continuity artifact contract.

This module deliberately does not connect to chat memory, vector memory,
autonomy, permissions, or execution.  It validates and loads an explicitly
provided JSON artifact only; it never writes, learns, embeds, or applies data.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = 1
REQUIRED_RECORD_FIELDS = frozenset(
    {
        "memory_id",
        "category",
        "content",
        "source",
        "observed_at",
        "confidence",
        "status",
        "scope",
        "authorization",
    }
)
ARTIFACT_FIELDS = frozenset({"schema_version", "records"})


class MemoryCategory(str, Enum):
    IDENTITY = "identity"
    PROJECT = "project"
    EXPERIENCE = "experience"
    USER_COLLABORATION = "user_collaboration"


class MemoryStatus(str, Enum):
    OBSERVED = "observed"
    CONFIRMED = "confirmed"
    PROPOSED = "proposed"
    DEPRECATED = "deprecated"
    REJECTED = "rejected"


class MemoryContractError(ValueError):
    """Raised when an artifact violates the frozen Phase 1 contract."""


def _require_non_empty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MemoryContractError(f"{field} must be a non-empty string")
    return value


def _validate_timestamp(value: Any) -> str:
    timestamp = _require_non_empty_string(value, "observed_at")
    try:
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MemoryContractError("observed_at must be an ISO-8601 timestamp") from exc
    return timestamp


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """One explicitly authored continuity fact.

    ``content`` is intentionally text-only in this first contract.  This
    keeps the artifact inspectable and prevents nested control data from being
    interpreted as executable configuration.
    """

    memory_id: str
    category: MemoryCategory
    content: str
    source: str
    observed_at: str
    confidence: float
    status: MemoryStatus
    scope: str
    authorization: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "MemoryRecord":
        if not isinstance(raw, Mapping):
            raise MemoryContractError("each memory record must be an object")
        keys = set(raw)
        missing = REQUIRED_RECORD_FIELDS - keys
        extra = keys - REQUIRED_RECORD_FIELDS
        if missing:
            raise MemoryContractError(f"record missing fields: {sorted(missing)}")
        if extra:
            raise MemoryContractError(f"record has unknown fields: {sorted(extra)}")

        memory_id = _require_non_empty_string(raw["memory_id"], "memory_id")
        content = _require_non_empty_string(raw["content"], "content")
        source = _require_non_empty_string(raw["source"], "source")
        scope = _require_non_empty_string(raw["scope"], "scope")
        authorization = _require_non_empty_string(raw["authorization"], "authorization")

        try:
            category = MemoryCategory(raw["category"])
        except ValueError as exc:
            raise MemoryContractError(
                f"category must be one of: {', '.join(item.value for item in MemoryCategory)}"
            ) from exc
        try:
            status = MemoryStatus(raw["status"])
        except ValueError as exc:
            raise MemoryContractError(
                f"status must be one of: {', '.join(item.value for item in MemoryStatus)}"
            ) from exc

        confidence = raw["confidence"]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise MemoryContractError("confidence must be a number")
        if not 0.0 <= float(confidence) <= 1.0:
            raise MemoryContractError("confidence must be between 0 and 1")

        return cls(
            memory_id=memory_id,
            category=category,
            content=content,
            source=source,
            observed_at=_validate_timestamp(raw["observed_at"]),
            confidence=float(confidence),
            status=status,
            scope=scope,
            authorization=authorization,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "category": self.category.value,
            "content": self.content,
            "source": self.source,
            "observed_at": self.observed_at,
            "confidence": self.confidence,
            "status": self.status.value,
            "scope": self.scope,
            "authorization": self.authorization,
        }


@dataclass(frozen=True, slots=True)
class MemoryArtifact:
    """Validated artifact with category-isolated read access."""

    schema_version: int
    records: tuple[MemoryRecord, ...]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "MemoryArtifact":
        if not isinstance(raw, Mapping):
            raise MemoryContractError("memory artifact must be an object")
        keys = set(raw)
        missing = ARTIFACT_FIELDS - keys
        extra = keys - ARTIFACT_FIELDS
        if missing:
            raise MemoryContractError(f"artifact missing fields: {sorted(missing)}")
        if extra:
            raise MemoryContractError(f"artifact has unknown fields: {sorted(extra)}")
        if raw["schema_version"] != SCHEMA_VERSION:
            raise MemoryContractError(
                f"unsupported schema_version: {raw['schema_version']!r}"
            )
        if not isinstance(raw["records"], list):
            raise MemoryContractError("artifact records must be a list")

        records = tuple(MemoryRecord.from_mapping(item) for item in raw["records"])
        ids = [record.memory_id for record in records]
        duplicates = sorted({memory_id for memory_id in ids if ids.count(memory_id) > 1})
        if duplicates:
            raise MemoryContractError(f"duplicate memory_id values: {duplicates}")
        return cls(schema_version=SCHEMA_VERSION, records=records)

    def for_category(self, category: MemoryCategory | str) -> tuple[MemoryRecord, ...]:
        category = MemoryCategory(category)
        return tuple(record for record in self.records if record.category is category)


class ReadOnlyMemoryLoader:
    """Load one explicit JSON artifact without any persistence side effect."""

    def load(self, path: str | Path) -> MemoryArtifact:
        artifact_path = Path(path)
        with artifact_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        return MemoryArtifact.from_mapping(copy.deepcopy(raw))


@dataclass(frozen=True, slots=True)
class ContinuityVerification:
    valid: bool
    record_count: int
    category_counts: dict[str, int]
    errors: tuple[str, ...]


def verify_continuity(path: str | Path) -> ContinuityVerification:
    """Validate an artifact and return a report without changing the file."""

    try:
        artifact = ReadOnlyMemoryLoader().load(path)
    except (OSError, json.JSONDecodeError, MemoryContractError) as exc:
        return ContinuityVerification(
            valid=False,
            record_count=0,
            category_counts={category.value: 0 for category in MemoryCategory},
            errors=(str(exc),),
        )

    counts = {
        category.value: len(artifact.for_category(category))
        for category in MemoryCategory
    }
    return ContinuityVerification(
        valid=True,
        record_count=len(artifact.records),
        category_counts=counts,
        errors=(),
    )


__all__ = [
    "ARTIFACT_FIELDS",
    "MemoryArtifact",
    "MemoryCategory",
    "MemoryContractError",
    "MemoryRecord",
    "MemoryStatus",
    "ReadOnlyMemoryLoader",
    "REQUIRED_RECORD_FIELDS",
    "SCHEMA_VERSION",
    "ContinuityVerification",
    "verify_continuity",
]
