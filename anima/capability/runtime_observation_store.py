"""P5.4 Real Runtime Observation Store Adapter / Read-only Reader.

The first slice that touches a *real* runtime store - and only on the read
side.  It reads the real observation store persisted by the runtime
(``data/health_history.json``, owned by ``anima/core/health_history.py``) and
normalizes its samples into P5.1 ``ObservedRuntimeState`` values.  It never
writes, never wakes the runtime, never repairs.

Direction of information flow (single direction, never reversed)::

    Real Runtime Store (data/health_history.json)
        -> RealObservationStoreAdapter.read()
        -> P5.1 ObservedRuntimeState        (frozen, offline source)
        -> P5.2 RuntimeConsumptionAdapter   (read-only consumption)
        -> Evidence only

Properties held by construction and by test:

  * READ ONLY        - read()/inspect()/verify() perform no file write, no DB,
                       no timestamp write-back, no process spawn
  * FAIL CLOSED      - a missing or corrupt store raises
                       ``ObservationStoreReadError``; nothing is auto-created
                       or auto-repaired
  * NO WAKE-UP       - reading is a plain local file read; no child-process
                       spawn, no remote-shell, no socket, no network, no
                       runtime initialization
  * NO EXECUTION     - there is no apply / execute / activate / mutate /
                       repair / dispatch surface
  * P5.1 MODEL ONLY  - outputs are P5.1 ``ObservedRuntimeState`` values; no
                       second observation model is introduced
  * DIGEST HONESTY   - the real store carries no digest.  This adapter reports
                       ``absence_of_digest`` as an audit fact, and computes a
                       *locally computed verification digest* (sha256 of the
                       store bytes) which is clearly labelled as local - it is
                       never presented as if the store provided it.

Mapping (faithful, never upgraded, never invented):

  * subject_id        = "runtime/health"      (the store's subject identity)
  * runtime_activation= "running"|"stopped"   (from sample["running"], verbatim)
  * execution_status  = "unknown"             (health samples carry no single
                                               execution status; reported as-is)
  * observed_at       = UTC ISO-8601 of sample["ts"]
  * source            = "local://<store_path>" (inside P5.1 offline prefixes)
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from anima.capability_runtime_observation import (
    OFFLINE_SOURCE_PREFIXES,
    UNKNOWN,
    ObservedRuntimeState,
)

__all__ = [
    "ObservationStoreReadError",
    "RealObservationStoreAdapter",
    "StoreReadVerification",
    "STORE_SUBJECT",
]

#: The subject identity carried by observations read from the real store.
STORE_SUBJECT = "runtime/health"


class ObservationStoreReadError(ValueError):
    """Raised when the real observation store is missing or corrupt (fail closed)."""


@dataclass(frozen=True, slots=True)
class StoreReadVerification:
    """Integrity result of a store read.

    ``valid`` is the aggregate.  ``digest_present``/``absence_of_digest``
    report the audit fact about the *stored* digest; ``locally_computed_digest``
    is a sha256 of the store bytes computed by this adapter and is explicitly
    *not* a store-provided digest.
    """

    valid: bool
    store_present: bool
    schema_intact: bool
    data_intact: bool
    digest_present: bool
    absence_of_digest: bool
    locally_computed_digest: str
    samples_read: int
    errors: tuple = ()


def _is_offline_prefix(source: str) -> bool:
    return isinstance(source, str) and source.startswith(OFFLINE_SOURCE_PREFIXES)


class RealObservationStoreAdapter:
    """Read-only reader over the real runtime observation store.

    Public surface (exactly three side-effect-free operations):

    * ``read``    - parse the store into P5.1 ``ObservedRuntimeState`` values
    * ``inspect`` - immutable mapping views of the observations
    * ``verify``  - integrity verification (fails closed; never repairs)

    There is intentionally no ``snapshot()``: P5.1's ``ObservationSnapshot`` is
    defined over ``RuntimeObservation`` values, while this adapter produces
    ``ObservedRuntimeState`` values; forcing a snapshot would either bend P5.1
    semantics or introduce a second model.  Batching remains a P5.1 concern.
    """

    def read(self, store_path) -> tuple:
        """Read the real store into P5.1 ObservedRuntimeState values (read-only)."""
        data, _ = self._load(store_path)
        samples = data.get("samples", [])
        return tuple(
            self._to_observed(sample, store_path) for sample in samples
        )

    def inspect(self, observations: Iterable[ObservedRuntimeState]) -> tuple:
        """Return immutable mapping views of the observed runtime states."""
        views = []
        for observation in observations:
            if not isinstance(observation, ObservedRuntimeState):
                raise ObservationStoreReadError(
                    "inspect accepts ObservedRuntimeState values only"
                )
            views.append(
                MappingProxyType(
                    {
                        "subject_id": observation.subject_id,
                        "runtime_activation": observation.runtime_activation,
                        "execution_status": observation.execution_status,
                        "observed_at": observation.observed_at,
                        "source": observation.source,
                    }
                )
            )
        return tuple(views)

    def verify(
        self,
        observations: Iterable[ObservedRuntimeState],
        store_path,
    ) -> StoreReadVerification:
        """Verify a store read (fails closed; reports, never repairs)."""
        errors: list[str] = []
        path = Path(store_path)
        store_present = path.is_file()

        digest_present = False
        locally_computed_digest = ""
        schema_intact = False
        data_intact = False

        if not store_present:
            errors.append("observation store is missing")
        else:
            try:
                raw = path.read_bytes()
            except OSError as exc:
                raw = b""
                errors.append(f"observation store unreadable: {exc}")
            locally_computed_digest = hashlib.sha256(raw).hexdigest()
            try:
                data = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                data = None
                errors.append(f"observation store corrupt: {exc}")
            if data is not None:
                if not isinstance(data, dict):
                    errors.append("observation store corrupt: root is not an object")
                elif data.get("version") != 1:
                    errors.append(
                        f"observation store schema mismatch: version={data.get('version')!r}"
                    )
                elif not isinstance(data.get("samples"), list):
                    errors.append("observation store corrupt: samples is not a list")
                else:
                    schema_intact = True
                    digest_present = any(
                        "digest" in str(key).lower() for key in data.keys()
                    ) or any(
                        "digest" in str(key).lower()
                        for sample in data["samples"]
                        if isinstance(sample, dict)
                        for key in sample.keys()
                    )
                if schema_intact:
                    # data integrity: re-derive and compare (deterministic)
                    try:
                        reread = self.read(path)
                    except ObservationStoreReadError as exc:
                        reread = ()
                        errors.append(str(exc))
                    items = tuple(observations)
                    if len(reread) != len(items):
                        errors.append("observation count diverges from store")
                    elif any(
                        a.subject_id != b.subject_id
                        or a.runtime_activation != b.runtime_activation
                        or a.execution_status != b.execution_status
                        or a.observed_at != b.observed_at
                        or a.source != b.source
                        for a, b in zip(reread, items)
                    ):
                        errors.append("observations diverge from store content")
                    else:
                        data_intact = True

        valid = (
            store_present
            and schema_intact
            and data_intact
            and not errors
        )
        return StoreReadVerification(
            valid=valid,
            store_present=store_present,
            schema_intact=schema_intact,
            data_intact=data_intact,
            digest_present=digest_present,
            absence_of_digest=not digest_present,
            locally_computed_digest=locally_computed_digest,
            samples_read=len(tuple(observations)),
            errors=tuple(errors),
        )

    # ------------------------------------------------------------------
    # private read-only helpers (no side effects)
    # ------------------------------------------------------------------
    def _load(self, store_path) -> tuple[dict[str, Any], bytes]:
        path = Path(store_path)
        if not path.is_file():
            raise ObservationStoreReadError(
                f"observation store missing: {path}"
            )
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ObservationStoreReadError(
                f"observation store unreadable: {path}: {exc}"
            ) from exc
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ObservationStoreReadError(
                f"observation store corrupt (invalid JSON): {path}: {exc}"
            ) from exc
        if not isinstance(data, dict) or not isinstance(data.get("samples"), list):
            raise ObservationStoreReadError(
                f"observation store corrupt (schema violation): {path}"
            )
        for sample in data["samples"]:
            if not isinstance(sample, dict) or "ts" not in sample:
                raise ObservationStoreReadError(
                    f"observation store corrupt (malformed sample): {path}"
                )
        return data, raw

    def _to_observed(self, sample: Mapping[str, Any], store_path) -> ObservedRuntimeState:
        ts = float(sample["ts"])
        observed_at = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        running = bool(sample.get("running"))
        source = f"local://{store_path}"
        if not _is_offline_prefix(source):
            # defensive: the source must stay inside the P5.1 offline contract
            raise ObservationStoreReadError(
                "store source escapes the offline-prefix contract"
            )
        return ObservedRuntimeState(
            subject_id=STORE_SUBJECT,
            runtime_activation="running" if running else "stopped",
            execution_status=UNKNOWN,
            observed_at=observed_at,
            source=source,
        )
