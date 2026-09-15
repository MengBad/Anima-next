"""P6.4 Execution Ledger Read-only Evidence Adapter - authoritative contract.

P6.4 is a READ-ONLY / AUDIT-ONLY adapter over the execution ledger facts
persisted by the runtime (F5.2 durable layout under ``execution_ledger/``).
It normalizes ledger facts into an immutable, digest-bound observation that
can enter the P5/P6 evidence chain:

    data/execution_ledger/{run}__{task}__{attempt}__{token8}.json
        -> ExecutionLedgerReader.read()     (filesystem read-only)
        -> ExecutionLedgerFactObservation   (frozen, slots, digest-bound)
        -> to_observed_runtime_state()      (P5.1 ObservedRuntimeState)
        -> P5.2 Consumption / P5.5 Reconciliation
        -> Evidence only

It is NOT a control path: it never records a ledger fact (ledger write), never
drives execution (executor act), never runs a plan (orchestrator run-plan),
never invokes a provider (provider invocation), never replays a fact, never
recovers a run, and never activates a capability.

Hard boundary (this contract enforces it):

  * READ ONLY         - read()/verify()/inspect() perform no file write, no
                        DB write, no timestamp write-back, no process spawn
  * NO FROZEN IMPORT  - this module imports NO anima.runtime / anima.constitution /
                        anima.core / anima.watchdog subsystem; the ledger schema
                        is MIRRORED here and pinned by a contract test that
                        records real facts through the upstream ExecutionLedger
  * FAIL CLOSED       - a missing or corrupt artifact raises from read();
                        verify() reports store_present/schema_intact instead.
                        Nothing is auto-created or auto-repaired.
  * MISSING IS STATE  - a missing artifact is NOT_OBSERVED: evidence state, not
                        implementation failure.  Never papered over with a
                        synthetic success, never compensated by other sources.
  * FACT != AUTHORITY - a ledger fact (success=True/False) is an observation
                        fact about what the external world did.  It is never
                        upgraded to approved/authorized/activated/deployed and
                        never rewrites governance records.
  * ADDITIVE DIGEST   - artifact_digest (sha256 of the raw file bytes) and
                        observation_digest (sha256 of the normalized payload)
                        are additive; no upstream digest is ever rewritten.
                        The artifact carries no digest - absence_of_digest is
                        reported as an audit fact.

Schema mirror (read-only reconstruction of the upstream serialization in
``anima/runtime/execution_ledger.py``)::

    {
      "key": {"run_id": str, "task_id": str, "attempt": int},
      "observation": {
        "task_id": str,
        "success": bool,
        "result": dict,
        "error": str,
        "metadata": dict
      }
    }
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from anima.capability_runtime_observation import (
    OFFLINE_SOURCE_PREFIXES,
    ObservedRuntimeState,
)

__all__ = [
    "ExecutionLedgerFactObservation",
    "ExecutionLedgerReadError",
    "ExecutionLedgerReader",
    "LedgerReadVerification",
    "to_observed_runtime_state",
]

#: The offline-source prefix contract is satisfied by local:// paths.
_SOURCE_PREFIX = "local://execution_ledger/"


class ExecutionLedgerReadError(ValueError):
    """Raised when an execution ledger artifact is missing or corrupt (fail closed)."""


@dataclass(frozen=True, slots=True)
class ExecutionLedgerFactObservation:
    """An immutable, digest-bound, read-only view of ONE execution ledger fact.

    Carries the ledger fact verbatim (run_id / task_id / attempt / success /
    result / error / metadata).  ``success`` is an *observed fact* about the
    external world - it is never upgraded to a governance or authorization
    claim, and it never rewrites a governance record.
    """

    run_id: str
    task_id: str
    attempt: int
    success: bool
    result: dict
    error: str
    metadata: dict
    source: str
    artifact_digest: str
    observation_digest: str

    def __post_init__(self) -> None:
        for name in (
            "run_id",
            "task_id",
            "source",
            "artifact_digest",
            "observation_digest",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ExecutionLedgerReadError(f"{name} must be a non-empty string")
        if not isinstance(self.attempt, int):
            raise ExecutionLedgerReadError("attempt must be an int")
        if not isinstance(self.success, bool):
            raise ExecutionLedgerReadError("success must be a bool")
        if not isinstance(self.result, dict):
            raise ExecutionLedgerReadError("result must be an object")
        if not isinstance(self.metadata, dict):
            raise ExecutionLedgerReadError("metadata must be an object")
        if not isinstance(self.error, str):
            raise ExecutionLedgerReadError("error must be a string")
        if not self.source.startswith(OFFLINE_SOURCE_PREFIXES):
            raise ExecutionLedgerReadError(
                "ledger fact source must stay inside the offline-prefix contract"
            )
        for name in ("artifact_digest", "observation_digest"):
            if not (
                len(getattr(self, name)) == 64
                and set(getattr(self, name)) <= frozenset("0123456789abcdef")
            ):
                raise ExecutionLedgerReadError(f"{name} must be a SHA-256 hex digest")


@dataclass(frozen=True, slots=True)
class LedgerReadVerification:
    """Integrity result of a ledger fact read (fails closed, never repairs).

    ``valid`` is the aggregate.  ``absence_of_digest`` reports the audit fact
    that the artifact itself carries no digest; ``artifact_digest`` is the
    locally computed sha256 of the raw file bytes, never presented as if the
    artifact provided it.
    """

    valid: bool
    store_present: bool
    schema_intact: bool
    data_intact: bool
    digest_intact: bool
    absence_of_digest: bool
    artifact_digest: str
    errors: tuple = ()


def _digest(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _is_hex64(value: str) -> bool:
    return len(value) == 64 and set(value) <= frozenset("0123456789abcdef")


# ---------------------------------------------------------------------------
# read-only schema validation (mirror of the upstream ledger serialization)
# ---------------------------------------------------------------------------
def _validate(data: Any, path: Path) -> None:
    """Fail closed unless the artifact matches the execution_ledger schema."""
    if not isinstance(data, dict):
        raise ExecutionLedgerReadError(
            f"execution ledger corrupt: root is not an object: {path}"
        )
    key = data.get("key")
    if not isinstance(key, dict):
        raise ExecutionLedgerReadError(
            f"execution ledger corrupt: 'key' must be an object: {path}"
        )
    for field in ("run_id", "task_id"):
        if not isinstance(key.get(field), str) or not key.get(field):
            raise ExecutionLedgerReadError(
                f"execution ledger corrupt: key.{field!r} must be a non-empty string: {path}"
            )
    if not isinstance(key.get("attempt"), int):
        raise ExecutionLedgerReadError(
            f"execution ledger corrupt: key.attempt must be an int: {path}"
        )
    observation = data.get("observation")
    if not isinstance(observation, dict):
        raise ExecutionLedgerReadError(
            f"execution ledger corrupt: 'observation' must be an object: {path}"
        )
    if not isinstance(observation.get("task_id"), str) or not observation.get("task_id"):
        raise ExecutionLedgerReadError(
            f"execution ledger corrupt: observation.task_id must be a non-empty string: {path}"
        )
    if not isinstance(observation.get("success"), bool):
        raise ExecutionLedgerReadError(
            f"execution ledger corrupt: observation.success must be a bool: {path}"
        )
    for field in ("result", "metadata"):
        if not isinstance(observation.get(field), dict):
            raise ExecutionLedgerReadError(
                f"execution ledger corrupt: observation.{field!r} must be an object: {path}"
            )
    if not isinstance(observation.get("error"), str):
        raise ExecutionLedgerReadError(
            f"execution ledger corrupt: observation.error must be a string: {path}"
        )


def _payload(
    run_id: str,
    task_id: str,
    attempt: int,
    success: bool,
    result: dict,
    error: str,
    metadata: dict,
    source: str,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "task_id": task_id,
        "attempt": attempt,
        "success": success,
        "result": result,
        "error": error,
        "metadata": metadata,
        "source": source,
    }


class ExecutionLedgerReader:
    """Read-only reader over ``data/execution_ledger/*.json`` (F5.2 facts).

    Public surface (exactly three side-effect-free operations):

    * ``read``    - parse one ledger artifact into an
                    ``ExecutionLedgerFactObservation``
    * ``verify``  - integrity verification (fails closed; never repairs)
    * ``inspect`` - immutable mapping view of an observation
    """

    def read(self, artifact_path) -> ExecutionLedgerFactObservation:
        """Read one execution ledger artifact (filesystem read-only, fail closed)."""
        path = Path(artifact_path)
        if not path.is_file():
            raise ExecutionLedgerReadError(
                f"execution ledger NOT_OBSERVED: {path} (missing)"
            )
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ExecutionLedgerReadError(
                f"execution ledger unreadable: {path}: {exc}"
            ) from exc
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ExecutionLedgerReadError(
                f"execution ledger corrupt (invalid JSON): {path}: {exc}"
            ) from exc
        _validate(data, path)

        key = data["key"]
        observation = data["observation"]
        source = f"{_SOURCE_PREFIX}{path.name}"
        if not source.startswith(OFFLINE_SOURCE_PREFIXES):
            raise ExecutionLedgerReadError(
                "ledger fact source escapes the offline-prefix contract"
            )
        observation_digest = _digest(
            _payload(
                run_id=key["run_id"],
                task_id=key["task_id"],
                attempt=key["attempt"],
                success=observation["success"],
                result=observation["result"],
                error=observation["error"],
                metadata=observation["metadata"],
                source=source,
            )
        )
        return ExecutionLedgerFactObservation(
            run_id=key["run_id"],
            task_id=key["task_id"],
            attempt=key["attempt"],
            success=observation["success"],
            result=observation["result"],
            error=observation["error"],
            metadata=observation["metadata"],
            source=source,
            artifact_digest=hashlib.sha256(raw).hexdigest(),
            observation_digest=observation_digest,
        )

    def inspect(
        self, observation: ExecutionLedgerFactObservation
    ) -> Mapping[str, Any]:
        """Return an immutable mapping view of an observation."""
        if not isinstance(observation, ExecutionLedgerFactObservation):
            raise ExecutionLedgerReadError(
                "inspect accepts an ExecutionLedgerFactObservation only"
            )
        return MappingProxyType(
            {
                "run_id": observation.run_id,
                "task_id": observation.task_id,
                "attempt": observation.attempt,
                "success": observation.success,
                "result": observation.result,
                "error": observation.error,
                "metadata": observation.metadata,
                "source": observation.source,
                "artifact_digest": observation.artifact_digest,
                "observation_digest": observation.observation_digest,
            }
        )

    def verify(
        self,
        observations,
        artifact_path,
    ) -> LedgerReadVerification:
        """Verify a ledger fact read (fails closed; reports, never repairs)."""
        errors: list[str] = []
        path = Path(artifact_path)
        store_present = path.is_file()
        artifact_digest = ""
        schema_intact = False
        data_intact = False
        digest_intact = False

        if not store_present:
            errors.append(
                f"execution ledger not observed (MISSING): {path}"
            )
        else:
            try:
                raw = path.read_bytes()
            except OSError as exc:
                raw = b""
                errors.append(f"execution ledger unreadable: {path}: {exc}")
            artifact_digest = hashlib.sha256(raw).hexdigest()
            try:
                _validate(json.loads(raw.decode("utf-8")), path)
                schema_intact = True
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                errors.append(f"execution ledger corrupt (invalid JSON): {exc}")
            except ExecutionLedgerReadError as exc:
                errors.append(str(exc))

            if schema_intact:
                try:
                    reread = self.read(path)
                except ExecutionLedgerReadError as exc:
                    reread = None
                    errors.append(str(exc))
                if isinstance(observations, ExecutionLedgerFactObservation):
                    items = (observations,)
                else:
                    items = tuple(observations)
                if len(items) != 1:
                    errors.append(
                        "expected exactly one execution ledger fact observation"
                    )
                elif reread is None:
                    errors.append("observation could not be re-read from store")
                elif not isinstance(items[0], ExecutionLedgerFactObservation):
                    errors.append(
                        "observation is not an ExecutionLedgerFactObservation"
                    )
                else:
                    expected = _digest(
                        _payload(
                            run_id=items[0].run_id,
                            task_id=items[0].task_id,
                            attempt=items[0].attempt,
                            success=items[0].success,
                            result=items[0].result,
                            error=items[0].error,
                            metadata=items[0].metadata,
                            source=items[0].source,
                        )
                    )
                    digest_intact = (
                        items[0].observation_digest == expected
                        and items[0].artifact_digest == artifact_digest
                    )
                    if not digest_intact:
                        errors.append(
                            "observation digest does not match the artifact"
                        )
                    data_intact = (
                        items[0].run_id == reread.run_id
                        and items[0].task_id == reread.task_id
                        and items[0].attempt == reread.attempt
                        and items[0].success == reread.success
                        and items[0].observation_digest == reread.observation_digest
                    )
                    if not data_intact:
                        errors.append(
                            "observation diverges from the artifact content"
                        )

        valid = (
            store_present
            and schema_intact
            and data_intact
            and digest_intact
            and not errors
        )
        return LedgerReadVerification(
            valid=valid,
            store_present=store_present,
            schema_intact=schema_intact,
            data_intact=data_intact,
            digest_intact=digest_intact,
            absence_of_digest=True,  # the ledger artifact schema carries no digest
            artifact_digest=artifact_digest,
            errors=tuple(errors),
        )


def to_observed_runtime_state(
    observation: ExecutionLedgerFactObservation,
) -> ObservedRuntimeState:
    """Map a ledger fact observation into the P5.1 observation model.

    Faithful mapping: the fact's ``success`` becomes the observed runtime
    activation verbatim (an observation fact about the external world).  The
    ledger artifact carries NO timestamp field, so ``observed_at`` carries the
    fact identity ``run_id@task_id#attempt`` as a faithful surrogate - never
    an invented ``now()``.  This is an OBSERVED fact - it is never upgraded to
    approved/authorized/activated/deployed and never rewrites a governance
    record.
    """
    if not isinstance(observation, ExecutionLedgerFactObservation):
        raise ExecutionLedgerReadError(
            "to_observed_runtime_state accepts an ExecutionLedgerFactObservation only"
        )
    return ObservedRuntimeState(
        subject_id=observation.run_id,
        runtime_activation="executed" if observation.success else "failed",
        execution_status="unknown",
        observed_at=f"{observation.run_id}@{observation.task_id}#{observation.attempt}",
        source=observation.source,
    )
