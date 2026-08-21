"""P7.17-A Phase 2 — Structured JSON logging for the Execution Bridge.

Every execution MUST be recorded as one machine-analyzable JSON line
(JSONL, append-only, atomic-ish per line):

    {
      execution_id, execution_key, task_id, capability_id, provider,
      result, latency_ms, evidence_id, error_type, timestamp
    }

Failure classification is standardized (P7.17-A):
    PROVIDER_FAILURE / RESOLUTION_FAILURE / LEDGER_FAILURE /
    SANDBOX_FAILURE / IDENTITY_FAILURE / UNKNOWN_FAILURE

The F5 identity chain (P7.17-A Phase 3) stays F5-domain-autonomous:
``evidence_id_for`` deterministically derives an evidence id from the
execution_key (sha256) — it never merges System B request identity, never
imports governance/health, and never reuses ``request_id`` as the key.

Scope limits (P7.17-A): no System B, no gateway, no capability_mode, no
provider expansion, no env bypass.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "ExecutionEvent",
    "ExecutionEventLog",
    "classify_failure",
    "evidence_id_for",
    "new_execution_id",
    "FAILURE_TYPES",
]

#: standardized failure vocabulary (P7.17-A Phase 2).
FAILURE_TYPES = (
    "PROVIDER_FAILURE",
    "RESOLUTION_FAILURE",
    "LEDGER_FAILURE",
    "SANDBOX_FAILURE",
    "IDENTITY_FAILURE",
    "UNKNOWN_FAILURE",
)

_REQUIRED_KEYS = (
    "execution_id",
    "execution_key",
    "task_id",
    "capability_id",
    "provider",
    "result",
    "latency_ms",
    "evidence_id",
    "error_type",
    "timestamp",
)


@dataclass(frozen=True, slots=True)
class ExecutionEvent:
    """One immutable execution fact, serializable to a single JSON line."""

    execution_id: str
    execution_key: tuple
    task_id: str
    capability_id: str
    provider: str
    result: str  # SUCCESS / FAILED / SKIPPED
    latency_ms: float
    evidence_id: str
    error_type: str  # one of FAILURE_TYPES, or "" on success

    def to_json(self) -> str:
        payload = {
            "execution_id": self.execution_id,
            "execution_key": json.dumps(list(self.execution_key), ensure_ascii=False),
            "task_id": self.task_id,
            "capability_id": self.capability_id,
            "provider": self.provider,
            "result": self.result,
            "latency_ms": round(float(self.latency_ms), 3),
            "evidence_id": self.evidence_id,
            "error_type": self.error_type,
            "timestamp": time.time(),
        }
        assert set(payload) == set(_REQUIRED_KEYS)  # schema guard
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def new_execution_id() -> str:
    """A fresh unique execution id (uuid4 hex)."""
    return uuid.uuid4().hex


def evidence_id_for(execution_key: tuple) -> str:
    """Deterministic F5 evidence id derived from the execution_key (sha256).

    Same key -> same evidence id (replay/duplicate detection); the attempt
    component changes the id.  F5-domain only — never the System B evidence.
    """
    return hashlib.sha256(repr(execution_key).encode("utf-8")).hexdigest()


def classify_failure(
    observation: Any,
    *,
    ledger_error: Optional[BaseException] = None,
) -> str:
    """Map an execution outcome to the standardized failure vocabulary.

    Order matters: a ledger failure is reported even if the provider act
    itself succeeded (a durable fact that failed to record is a ledger
    failure); a resolution refusal precedes provider-level refusals.
    """
    if ledger_error is not None:
        return "LEDGER_FAILURE"
    if not getattr(observation, "success", True):
        error = str(getattr(observation, "error", "") or "").lower()
        # pure resolution absence: the capability has NO candidate provider at
        # all (nothing was invoked) — "no provider" / "unavailable" without an
        # actual provider error underneath
        if "no provider" in error or (
            "unavailable" in error and "last error" not in error
        ):
            return "RESOLUTION_FAILURE"
        # provider-level refusal (traversal / policy) — even when the resolver
        # wrapped it as "all candidates failed": a provider WAS invoked and
        # refused; the sandbox was never written (no side effect)
        if "escape" in error or "refus" in error or "denied" in error:
            return "PROVIDER_FAILURE"
        # sandbox-level write failure (I/O, conflict, space)
        if "sandbox" in error or "conflict" in error or "write failed" in error:
            return "SANDBOX_FAILURE"
        if "ledger" in error:
            return "LEDGER_FAILURE"
        if "identity" in error or "key" in error:
            return "IDENTITY_FAILURE"
        return "PROVIDER_FAILURE"
    return ""  # success carries no failure type


class ExecutionEventLog:
    """Append-only JSONL event log under ``data_dir/logs/execution.jsonl``."""

    def __init__(self, data_dir: str) -> None:
        self._path = Path(data_dir) / "logs" / "execution.jsonl"
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # a missing log dir must not crash the caller; record() fails loudly

    @property
    def path(self) -> Path:
        return self._path

    def record(self, event: ExecutionEvent) -> None:
        line = event.to_json()
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()

    def read(self) -> list[dict]:
        """Read all recorded events (for automated analysis / summary)."""
        if not self._path.exists():
            return []
        events: list[dict] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # a torn tail line is skipped, never fatal
        return events
