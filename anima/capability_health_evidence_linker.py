"""P6.10-C — Execution Fact <-> HealthEvidence read-only reconciliation.

Builds a read-only consistency view across two SEPARATE execution-fact sinks:

    ExecutionFact  (P6.10-B domain: data/execution_facts/<evidence_id>.json)
    HealthEvidence (System B F8.10 sink: health_evidence.jsonl)

The linker only READS, COMPARES and REPORTS.  It never writes, never repairs,
never re-runs and never touches the execution ledger.  An execution fact whose
capability is matched by a HealthEvidence row with ``executed=True`` is
CONSISTENT; anything else is reported as an audit state (MISSING / INCONSISTENT
/ SCHEMA_FAILURE) with the store left byte-identical.

Schema mirror: HealthEvidence envelope ``{"schema_version": 1, "evidence": {...}}``
is parsed here (stdlib only) without importing the frozen ``anima.capability.health``
subsystem.  Tests pin the mirror against the real upstream store.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

#: linkage classification (audit-only, never repaired).
CONSISTENT = "CONSISTENT"
INCONSISTENT = "INCONSISTENT"
MISSING_HEALTH_EVIDENCE = "MISSING_HEALTH_EVIDENCE"
SCHEMA_FAILURE = "SCHEMA_FAILURE"

#: expected envelope schema version (mirror of F8.10 store).
HEALTH_EVIDENCE_SCHEMA_VERSION = 1

#: semantic-escalation tokens that must never appear in an execution fact.
_ESCALATION_TOKENS = frozenset(
    {"authorized_running", "activated", "deployed", "production", "approved"}
)


class HealthEvidenceLinkError(ValueError):
    """Raised when reconciliation input cannot be understood (fails closed)."""


def _digest(*parts: Any) -> str:
    h = hashlib.sha256()
    for part in parts:
        if isinstance(part, str):
            h.update(part.encode("utf-8"))
        else:
            h.update(json.dumps(part, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return h.hexdigest()


def _read_execution_fact(path: Path) -> Mapping[str, Any]:
    """Read + parse an ExecutionFact artifact (mirror of P6.10-B schema)."""
    try:
        raw = json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise HealthEvidenceLinkError(f"execution fact unreadable: {exc}") from exc
    if not isinstance(raw, dict):
        raise HealthEvidenceLinkError("execution fact must be a JSON object")
    for name in ("evidence_id", "capability_id"):
        if not isinstance(raw.get(name), str) or not raw.get(name):
            raise HealthEvidenceLinkError(f"execution fact missing {name!r}")
    return raw


def _iter_health_rows(path: Path) -> tuple[list[Mapping[str, Any]], int]:
    """Parse a health_evidence.jsonl into valid rows + bad-row count.

    Read semantics mirror the real store (bad lines are skipped and counted),
    but a file with zero valid rows is a schema failure for reconciliation.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return [], 0  # caller distinguishes missing vs unreadable
    rows: list[Mapping[str, Any]] = []
    bad = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            bad += 1
            continue
        if not isinstance(row, dict) or row.get("schema_version") != HEALTH_EVIDENCE_SCHEMA_VERSION:
            bad += 1
            continue
        ev = row.get("evidence")
        if not isinstance(ev, dict):
            bad += 1
            continue
        rows.append(ev)
    return rows, bad


@dataclass(frozen=True, slots=True)
class ExecutionEvidenceLinkage:
    """Immutable result of a read-only reconciliation (digest-bound)."""

    execution_evidence_id: str
    capability_id: str
    linked_health_evidence_ids: tuple[str, ...]
    capability_matched: bool
    executed_matched: bool
    linkage_status: str
    health_rows_seen: int
    health_rows_bad: int
    linkage_digest: str

    def __post_init__(self) -> None:
        for name in (
            "execution_evidence_id",
            "capability_id",
            "linkage_status",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise HealthEvidenceLinkError(f"{name} must be a non-empty string")
        if not isinstance(self.linked_health_evidence_ids, tuple):
            raise HealthEvidenceLinkError("linked_health_evidence_ids must be a tuple")
        for name in ("capability_matched", "executed_matched"):
            if not isinstance(getattr(self, name), bool):
                raise HealthEvidenceLinkError(f"{name} must be a bool")
        if self.linkage_status not in (
            CONSISTENT,
            INCONSISTENT,
            MISSING_HEALTH_EVIDENCE,
            SCHEMA_FAILURE,
        ):
            raise HealthEvidenceLinkError(f"unknown linkage_status: {self.linkage_status!r}")
        if not (
            len(self.linkage_digest) == 64
            and set(self.linkage_digest) <= frozenset("0123456789abcdef")
        ):
            raise HealthEvidenceLinkError("linkage_digest must be a SHA-256 hex digest")


@dataclass(frozen=True, slots=True)
class LinkageVerification:
    """Integrity / consistency verification of a linkage (fails closed)."""

    valid: bool
    digest_intact: bool
    capability_matched: bool
    executed_matched: bool
    non_escalated: bool


class HealthEvidenceLinker:
    """Read-only reconciliation surface: link / verify / inspect."""

    # -- read-only reconciliation -------------------------------------------
    def link(self, execution_fact_path: str | Path, health_evidence_path: str | Path) -> ExecutionEvidenceLinkage:
        """Reconcile one execution fact against the health evidence sink.

        Never writes, never repairs, never reads the execution ledger.
        """
        fact = _read_execution_fact(Path(execution_fact_path))
        health = Path(health_evidence_path)

        evidence_id = fact["evidence_id"]
        capability_id = fact["capability_id"]

        if not health.exists():
            linkage_digest = _digest(
                "execution-evidence-linkage",
                evidence_id,
                capability_id,
                MISSING_HEALTH_EVIDENCE,
                (),
                False,
                False,
            )
            return ExecutionEvidenceLinkage(
                execution_evidence_id=evidence_id,
                capability_id=capability_id,
                linked_health_evidence_ids=(),
                capability_matched=False,
                executed_matched=False,
                linkage_status=MISSING_HEALTH_EVIDENCE,
                health_rows_seen=0,
                health_rows_bad=0,
                linkage_digest=linkage_digest,
            )

        rows, bad = _iter_health_rows(health)
        if not rows:
            linkage_digest = _digest(
                "execution-evidence-linkage",
                evidence_id,
                capability_id,
                SCHEMA_FAILURE,
                (),
                False,
                False,
                bad,
            )
            return ExecutionEvidenceLinkage(
                execution_evidence_id=evidence_id,
                capability_id=capability_id,
                linked_health_evidence_ids=(),
                capability_matched=False,
                executed_matched=False,
                linkage_status=SCHEMA_FAILURE,
                health_rows_seen=0,
                health_rows_bad=bad,
                linkage_digest=linkage_digest,
            )

        matched = [
            row["evidence_id"]
            for row in rows
            if row.get("capability_id") == capability_id
            and isinstance(row.get("evidence_id"), str)
        ]
        executed_any = any(
            row.get("capability_id") == capability_id and row.get("executed") is True
            for row in rows
        )
        capability_matched = bool(matched)
        if capability_matched and executed_any:
            status = CONSISTENT
        elif capability_matched:
            status = INCONSISTENT
        else:
            status = MISSING_HEALTH_EVIDENCE

        linkage_digest = _digest(
            "execution-evidence-linkage",
            evidence_id,
            capability_id,
            status,
            tuple(sorted(matched)),
            capability_matched,
            executed_any,
            len(rows),
            bad,
        )
        return ExecutionEvidenceLinkage(
            execution_evidence_id=evidence_id,
            capability_id=capability_id,
            linked_health_evidence_ids=tuple(sorted(matched)),
            capability_matched=capability_matched,
            executed_matched=executed_any,
            linkage_status=status,
            health_rows_seen=len(rows),
            health_rows_bad=bad,
            linkage_digest=linkage_digest,
        )

    # -- verification (re-reads and re-derives; fails closed) ---------------
    def verify(
        self,
        linkage: ExecutionEvidenceLinkage,
        execution_fact_path: str | Path,
        health_evidence_path: str | Path,
    ) -> LinkageVerification:
        if not isinstance(linkage, ExecutionEvidenceLinkage):
            return LinkageVerification(
                valid=False, digest_intact=False,
                capability_matched=False, executed_matched=False,
                non_escalated=False,
            )
        try:
            rederived = self.link(execution_fact_path, health_evidence_path)
        except HealthEvidenceLinkError:
            rederived = None
        digest_intact = (
            rederived is not None and rederived.linkage_digest == linkage.linkage_digest
        )
        non_escalated = self._non_escalated(Path(execution_fact_path))
        valid = digest_intact and linkage.capability_matched and linkage.executed_matched
        return LinkageVerification(
            valid=valid,
            digest_intact=digest_intact,
            capability_matched=linkage.capability_matched,
            executed_matched=linkage.executed_matched,
            non_escalated=non_escalated,
        )

    @staticmethod
    def _non_escalated(execution_fact_path: Path) -> bool:
        """The execution fact carries no escalation semantics (field whitelist)."""
        try:
            fact = _read_execution_fact(execution_fact_path)
        except HealthEvidenceLinkError:
            return False
        keys = set(fact.keys())
        # structural guarantee: fact fields never include escalation tokens
        return keys.isdisjoint(_ESCALATION_TOKENS) and "result" not in keys or (
            isinstance(fact.get("result", {}), dict)
            and keys.isdisjoint(_ESCALATION_TOKENS)
        )

    # -- inspection ---------------------------------------------------------
    def inspect(self, linkage: ExecutionEvidenceLinkage) -> dict[str, Any]:
        """Read-only projection of a linkage for audit reporting."""
        return {
            "execution_evidence_id": linkage.execution_evidence_id,
            "capability_id": linkage.capability_id,
            "linked_health_evidence_ids": list(linkage.linked_health_evidence_ids),
            "capability_matched": linkage.capability_matched,
            "executed_matched": linkage.executed_matched,
            "linkage_status": linkage.linkage_status,
            "health_rows_seen": linkage.health_rows_seen,
            "health_rows_bad": linkage.health_rows_bad,
            "linkage_digest": linkage.linkage_digest,
        }
