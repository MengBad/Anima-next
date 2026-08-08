"""F7.3b — Recovery Marker Lifecycle store (side record, NOT run-state).

A recovery marker tracks the lifecycle of a recovery attempt for an originally
crashed RUNNING run. It is a SIDE RECORD, fully independent of the run snapshot:

- It is NEVER written through ``WorkflowInterface._transition`` (O1 single owner of
  run.status) and NEVER via the frozen ``WorkflowRunStore`` (F4.1).
- It lives in its own ``recovery_markers/`` namespace on the SAME ``PersistenceProvider``
  the run store uses, so no on-disk migration of run snapshots is required and existing
  deployments upgrade with ZERO migration (a missing ``recovery_markers/`` dir is simply
  treated as "no marker yet" = current behavior).
- The ledger (I3) remains the AUTHORITATIVE idempotency guard. The marker is a forensic
  + optimization signal only: it is never consulted for correctness.

Design invariants (F7.3 §55):

- ``original_run_id == source_run_id`` (I3 conservation).
- ``original_run_id`` is the unique key: at most one marker per crashed run.
- The ``phase`` drives startup re-enumeration: only ``RECOVERED`` may be SKIPPED;
  ``RECOVERING`` / ``FAILED`` MUST be retried (ledger still兜底 if we choose not to skip).

Lifecycle (written by the F5.5 ``ResumeCoordinator.recover_and_resume``, the single
caller of the F4 primitive):

    (start)            -> RECOVERING   (original_run_id, "", source_run_id)
    _resume_run OK     -> RECOVERED     (original_run_id, new_run_id, source_run_id)
    any exception      -> FAILED        (error captured)
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

from anima.core.persistence import PersistenceProvider

MARKER_SCHEMA_VERSION = 1
MARKER_REL_DIR = "recovery_markers"


class MarkerPhase(str, Enum):
    """Lifecycle phase of a recovery attempt for one crashed run."""

    RECOVERING = "RECOVERING"
    RECOVERED = "RECOVERED"
    FAILED = "FAILED"


@dataclass
class RecoveryMarker:
    """A persisted side record describing one recovery attempt."""

    original_run_id: str
    new_run_id: str
    source_run_id: str
    phase: MarkerPhase
    schema_version: int = MARKER_SCHEMA_VERSION
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["phase"] = self.phase.value  # serialize as plain str
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RecoveryMarker":
        d = dict(d)
        # backward-compatible: tolerate legacy/unknown phase strings (default to RECOVERING
        # so an unrecognized marker is RETRIED, never silently skipped) and missing fields.
        raw_phase = d.get("phase", MarkerPhase.RECOVERING.value)
        try:
            phase = MarkerPhase(raw_phase)
        except ValueError:
            phase = MarkerPhase.RECOVERING
        d["phase"] = phase
        d.setdefault("schema_version", MARKER_SCHEMA_VERSION)
        return cls(**d)


class RecoveryMarkerStore:
    """Durable side record of recovery attempts, built on the shared ``PersistenceProvider``.

    Uses the provider's atomic ``write_json`` / ``read_json`` (tempfile + fsync + os.replace)
    so a crash mid-write never leaves a half-written marker. Never touches run-state or the
    frozen ``WorkflowRunStore``; correctness never depends on these records.
    """

    def __init__(self, persistence: PersistenceProvider) -> None:
        # Reuse the SAME provider the WorkflowRunStore uses (no second data root, no migration).
        self._persistence = persistence

    def _rel_path(self, original_run_id: str) -> str:
        return f"{MARKER_REL_DIR}/{original_run_id}.json"

    # ---- writes (each is an atomic side record) ----

    def write_recovering(self, original_run_id: str, new_run_id: str, source_run_id: str) -> RecoveryMarker:
        """Record that a recovery attempt has STARTED (RECOVERING)."""
        marker = RecoveryMarker(
            original_run_id=original_run_id,
            new_run_id=new_run_id,
            source_run_id=source_run_id,
            phase=MarkerPhase.RECOVERING,
        )
        self._persistence.write_json(self._rel_path(original_run_id), marker.to_dict())
        return marker

    def mark_recovered(self, original_run_id: str, new_run_id: str, source_run_id: str) -> RecoveryMarker:
        """Record that the recovery attempt COMPLETED (RECOVERED)."""
        marker = RecoveryMarker(
            original_run_id=original_run_id,
            new_run_id=new_run_id,
            source_run_id=source_run_id,
            phase=MarkerPhase.RECOVERED,
            completed_at=time.time(),
        )
        self._persistence.write_json(self._rel_path(original_run_id), marker.to_dict())
        return marker

    def mark_failed(self, original_run_id: str, error: Exception, new_run_id: str = "", source_run_id: str = "") -> RecoveryMarker:
        """Record that the recovery attempt FAILED, preserving any prior ids if known."""
        existing = self.get(original_run_id)
        if existing is not None:
            new_run_id = new_run_id or existing.new_run_id
            source_run_id = source_run_id or existing.source_run_id
        marker = RecoveryMarker(
            original_run_id=original_run_id,
            new_run_id=new_run_id,
            source_run_id=source_run_id,
            phase=MarkerPhase.FAILED,
            completed_at=time.time(),
            error=str(error),
        )
        self._persistence.write_json(self._rel_path(original_run_id), marker.to_dict())
        return marker

    # ---- reads ----

    def get(self, original_run_id: str) -> Optional[RecoveryMarker]:
        """Return the marker for a crashed run, or ``None`` if none exists yet."""
        d = self._persistence.read_json(self._rel_path(original_run_id), None)
        if d is None:
            return None
        return RecoveryMarker.from_dict(d)

    def exists(self, original_run_id: str) -> bool:
        return self.get(original_run_id) is not None

    def is_recovered(self, original_run_id: str) -> bool:
        """Convenience: True only when a marker exists AND its phase is RECOVERED."""
        m = self.get(original_run_id)
        return m is not None and m.phase is MarkerPhase.RECOVERED

    # ---- cleanup (operations only; correctness never depends on deletion) ----

    def delete(self, original_run_id: str) -> None:
        """Remove a marker (e.g. GC after retention). No-op if it does not exist."""
        p = self._persistence.data_dir / self._rel_path(original_run_id)
        if p.exists():
            p.unlink()
