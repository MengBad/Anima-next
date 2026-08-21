"""ExecutionLedger — the idempotency fact store (F5.1 + F5.2).

Records the *side-effect facts* of execution, deliberately SEPARATE from the
WorkflowRunStore (F5 ruling):

    WorkflowRunStore   — lifecycle state: what the workflow thinks is happening
    ExecutionLedger    — side-effect facts: what the external world has actually changed

The two are different truth sources and must not merge: a run snapshot can say
"task t1 is PENDING" while the ledger knows "the external call for (run_id, t1, attempt
1) already happened" — exactly the dispatch-durability gap F5 addresses.

Contract (frozen in F5 / F5.1 / F5.2):

- Key: the idempotency key ``(run_id, task_id, attempt)`` — uniquely identifies one
  logical execution attempt.
- ``get(key)``  -> the recorded observation, or None (no fact yet).
- ``put(key, observation)`` -> record the fact (idempotency is the caller's gate: an
  executor consults the ledger BEFORE acting and records AFTER acting).

F5.2 added durable persistence on top of the F5.1 in-memory contract:

- Backend: the shared ``PersistenceProvider`` (atomic tempfile + fsync + os.replace),
  under the ``execution_ledger/`` storage subdomain — deliberately NOT the
  ``workflow_runs/`` domain owned by ``WorkflowRunStore``.
- Key serialization: the idempotency key is stored as a STRUCTURED dict
  ``{"run_id", "task_id", "attempt"}`` in each file's ``"key"`` field, never a Python
  tuple repr — so a cross-language / cross-format migration can still parse it.
- Observations are PURE DATA: ``result`` / ``error`` / ``metadata`` survive a JSON
  round-trip; callables / runtime instances are rejected at the serialization boundary.
- Atomic boundary: a fact is recorded with a single ``write_json`` call. If the durable
  write fails, the fact is simply absent (no half-written file), so the executor is free
  to retry — exactly the F5.2 failure semantics (no external transaction yet).

Constructor:

- ``ExecutionLedger()``               -> in-memory only (F5.1 compatibility).
- ``ExecutionLedger(persistence=pp)`` -> in-memory + durable (F5.2).
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from typing import Any, Optional

from anima.core.persistence import PersistenceProvider  # storage primitive only
from anima.runtime.executor import ExecutionObservation

#: storage subdomain, kept separate from WorkflowRunStore's ``workflow_runs/``
_SUBDIR = "execution_ledger"

#: characters safe in a filename (run_id/task_id come from plan/workflow ids)
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def _safe(s: Any) -> str:
    return _SAFE.sub("_", str(s))


def _key_dict(execution_key: tuple) -> dict:
    run_id, task_id, attempt = execution_key
    return {"run_id": run_id, "task_id": task_id, "attempt": attempt}


def _key_path(execution_key: tuple) -> str:
    """Deterministic on-disk path for a key.

    Filename = ``{run_id}__{task_id}__{attempt}__{token8}.json`` under execution_ledger/.
    The 8-char token is a hash of the canonical key JSON, guaranteeing a unique,
    stable file per key even if sanitization collides on the readable prefix.
    """
    kd = _key_dict(execution_key)
    canonical = json.dumps(kd, sort_keys=True, ensure_ascii=False)
    token = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]
    return (
        f"{_SUBDIR}/"
        f"{_safe(kd['run_id'])}__{_safe(kd['task_id'])}__{_safe(kd['attempt'])}__{token}.json"
    )


def _observation_to_dict(obs: ExecutionObservation) -> dict:
    """Pure-data projection (must be JSON-serializable)."""
    return {
        "task_id": obs.task_id,
        "success": obs.success,
        "result": obs.result,
        "error": obs.error,
        "metadata": obs.metadata,
    }


def _observation_from_dict(d: dict) -> ExecutionObservation:
    return ExecutionObservation(
        task_id=d["task_id"],
        success=d["success"],
        result=d["result"],
        error=d["error"],
        metadata=d["metadata"],
    )


class ExecutionLedger:
    """Idempotency fact store: in-memory (F5.1) and optionally durable (F5.2)."""

    def __init__(self, persistence: Optional[PersistenceProvider] = None) -> None:
        # memory cache (fast path; also the only store in F5.1 mode)
        self._memory: dict[tuple, Any] = {}
        # durable backend (None => memory-only, F5.1-compatible)
        self._persistence = persistence
        # per-key thread locks: serialise check-execute-record for the SAME key
        # so concurrent callers produce exactly one side effect (P7.17-A Phase 4).
        # Distinct keys never block each other.
        self._locks: dict[tuple, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ---- read -----------------------------------------------------------

    def get(self, execution_key: tuple) -> Optional[Any]:
        """Return the recorded observation for ``execution_key``, or None if absent."""
        cached = self._memory.get(execution_key)
        if cached is not None:
            return cached
        if self._persistence is not None:
            data = self._persistence.read_json(_key_path(execution_key))
            if data is not None and "observation" in data:
                obs = _observation_from_dict(data["observation"])
                self._memory[execution_key] = obs  # backfill cache
                return obs
        return None

    def has(self, execution_key: tuple) -> bool:
        """Whether a fact is already recorded for this key (idempotency probe)."""
        return self.get(execution_key) is not None

    def lock_for(self, execution_key: tuple) -> threading.Lock:
        # Per-key lock (created on demand) so get->side-effect->put for the SAME
        # key is atomic across threads; distinct keys never block each other.
        with self._locks_guard:
            lock = self._locks.get(execution_key)
            if lock is None:
                lock = threading.Lock()
                self._locks[execution_key] = lock
            return lock

    # ---- write ----------------------------------------------------------

    def put(self, execution_key: tuple, observation: ExecutionObservation) -> None:
        """Record the side-effect fact for ``execution_key``.

        In durable mode the fact is written atomically via ``PersistenceProvider``
        BEFORE the memory cache is updated, so a failed/interrupted durable write
        leaves NO half-written entry (the fact simply stays absent and may be
        retried by the executor). In memory-only mode the fact is kept in memory.

        The observation must be pure data (result/error/metadata JSON-serializable);
        a non-serializable payload raises at the ``write_json`` boundary.
        """
        if self.has(execution_key):
            return  # already recorded: original fact preserved, no overwrite
        if self._persistence is not None:
            payload = {
                "key": _key_dict(execution_key),
                "observation": _observation_to_dict(observation),
            }
            # atomic write first; only update cache on success
            self._persistence.write_json(_key_path(execution_key), payload)
            self._memory[execution_key] = observation
        else:
            self._memory[execution_key] = observation

    # ---- retention (L2) -------------------------------------------------

    def cleanup(self, before: Optional[float] = None) -> int:
        """Remove COMPLETED (success=True) records; preserve failed / recoverable.

        L2 retention: a successful fact is done and safe to drop once retention
        decides; failed / recoverable / unknown records are preserved for replay.
        before=None removes ALL completed records. before is an optional unix-ts
        cutoff resolved from metadata ts/recorded_at (absent => eligible).
        """
        removed = 0
        for key in list(self._memory.keys()):
            obs = self._memory.get(key)
            if obs is None or not obs.success:
                continue
            if before is not None:
                md = obs.metadata or {}
                ts = md.get("ts") or md.get("recorded_at")
                try:
                    ts = float(ts) if ts is not None else None
                except (TypeError, ValueError):
                    ts = None
                if ts is not None and ts >= before:
                    continue
            self._memory.pop(key, None)
            removed += 1
            if self._persistence is not None:
                try:
                    target = self._persistence.data_dir / _key_path(key)
                    if target.exists():
                        target.unlink()
                    self._persistence.invalidate_cache(_key_path(key))
                except Exception:
                    pass
        return removed
