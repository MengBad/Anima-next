"""P6.1 Runtime Read-only Workflow Artifact Boundary Adapter.

The first *conditional* runtime artifact bridge: it reads ONE artifact domain
only - ``data/workflow_runs/<run_id>.json`` (run lifecycle snapshots, F4.2
documented layout) - as a plain filesystem file, and normalizes it into an
immutable, digest-bound observation that can enter the P5 evidence chain.

    data/workflow_runs/<run_id>.json
        -> WorkflowSnapshotAdapter.read()
        -> WorkflowRunSnapshotObservation   (frozen, slots, digest-bound)
        -> to_observed_runtime_state()     (P5.1 ObservedRuntimeState)
        -> P5.2 Consumption / P5.5 Reconciliation
        -> Evidence only

Properties held by construction and by test:

  * READ ONLY        - read()/inspect()/verify() perform no file write, no DB,
                       no timestamp write-back, no process spawn
  * ONE DOMAIN ONLY  - workflow_runs snapshots only; execution_ledger is out of
                       scope and never read
  * NO FROZEN IMPORT - this module imports NO anima.runtime / anima.constitution /
                       anima.core / anima.watchdog subsystem; the artifact schema
                       is MIRRORED here and pinned by a contract test that builds
                       real runs through the upstream WorkflowRunStore
  * FAIL CLOSED      - a missing or corrupt artifact raises
                       ``WorkflowSnapshotReadError`` from read(); verify() reports
                       store_present/schema_intact/data_intact instead.  Nothing
                       is auto-created or auto-repaired.
  * MISSING IS STATE - a missing artifact is NOT_OBSERVED: evidence state, not
                       implementation failure.  Never papered over with a
                       synthetic success and never compensated by searching other
                       runtime sources.
  * FACT != AUTHORITY- status values (running / completed / failed / ...) inside
                       an artifact stay observed facts.  They are never upgraded
                       to approved / authorized / activated / executed / deployed.
  * ADDITIVE DIGEST  - artifact_digest (sha256 of the raw file bytes) and
                       observation_digest (sha256 of the normalized payload) are
                       additive; no upstream digest is ever rewritten.  The
                       artifact carries no digest - absence_of_digest is reported
                       as an audit fact.

Schema mirror (read-only reconstruction of the upstream serialization in
``anima/runtime/persistence.py::WorkflowRunStore._run_to_dict``)::

    {
      "id": str,
      "contract_version": str,
      "plan_ref": {"plan_id": str, "contract_version": str},
      "goal_ref": str,
      "status": "pending"|"running"|"suspended"|"completed"|"failed",
      "task_states": [
        {"task_id": str, "status": "pending"|"running"|"done"|"failed",
         "attempts": int, "result": dict, "error": str}
      ],
      "current_task_id": str,
      "checkpoint_ref": str,
      "retry_policy": dict,
      "schedule_ref": str,
      "budget_ref": str,
      "context_ref": str,
      "created_at": str,
      "completed_at": str
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
    "RUN_STATUSES",
    "TASK_RUN_STATUSES",
    "WorkflowRunSnapshotObservation",
    "WorkflowSnapshotAdapter",
    "WorkflowSnapshotReadError",
    "WorkflowSnapshotVerification",
    "to_observed_runtime_state",
]

#: RunStatus vocabulary (mirror of anima/constitution/state.py RunStatus).
RUN_STATUSES = frozenset(
    {"pending", "running", "suspended", "completed", "failed", "cancelled"}
)

#: TaskRunStatus vocabulary (mirror of anima/constitution/state.py TaskRunStatus).
TASK_RUN_STATUSES = frozenset(
    {"pending", "running", "done", "failed", "skipped"}
)

#: The offline-source prefix contract is satisfied by local:// paths.
_SOURCE_PREFIX = "local://workflow_runs/"


class WorkflowSnapshotReadError(ValueError):
    """Raised when a workflow run snapshot is missing or corrupt (fail closed)."""


@dataclass(frozen=True, slots=True)
class WorkflowRunSnapshotObservation:
    """An immutable, digest-bound, read-only view of ONE workflow run snapshot.

    Carries the run facts verbatim (status / task statuses / timestamps).  The
    status is an *observed fact* - it is never upgraded to a governance or
    authorization claim.
    """

    run_id: str
    contract_version: str
    plan_id: str
    goal_ref: str
    status: str
    task_count: int
    task_ids: tuple
    task_statuses: tuple
    current_task_id: str
    created_at: str
    completed_at: str
    source: str
    artifact_digest: str
    observation_digest: str

    def __post_init__(self) -> None:
        # required non-empty (a real artifact always carries these)
        for name in (
            "run_id",
            "contract_version",
            "plan_id",
            "status",
            "created_at",
            "source",
            "artifact_digest",
            "observation_digest",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise WorkflowSnapshotReadError(f"{name} must be a non-empty string")
        # string-typed but may legitimately be empty in a real artifact
        # (e.g. completed_at="" for a run that has not completed)
        for name in ("goal_ref", "current_task_id", "completed_at"):
            if not isinstance(getattr(self, name), str):
                raise WorkflowSnapshotReadError(f"{name} must be a string")
        if not isinstance(self.task_ids, tuple) or not isinstance(
            self.task_statuses, tuple
        ):
            raise WorkflowSnapshotReadError("task_ids/task_statuses must be tuples")
        if not self.source.startswith(OFFLINE_SOURCE_PREFIXES):
            raise WorkflowSnapshotReadError(
                "workflow snapshot source must stay inside the offline-prefix contract"
            )
        if self.status not in RUN_STATUSES:
            raise WorkflowSnapshotReadError(
                f"unknown run status {self.status!r} (not in the RunStatus vocabulary)"
            )
        for status in self.task_statuses:
            if status not in TASK_RUN_STATUSES:
                raise WorkflowSnapshotReadError(
                    f"unknown task status {status!r} (not in the TaskRunStatus vocabulary)"
                )
        for name in ("artifact_digest", "observation_digest"):
            if not (
                len(getattr(self, name)) == 64
                and set(getattr(self, name)) <= frozenset("0123456789abcdef")
            ):
                raise WorkflowSnapshotReadError(
                    f"{name} must be a SHA-256 hex digest"
                )


@dataclass(frozen=True, slots=True)
class WorkflowSnapshotVerification:
    """Integrity result of a workflow snapshot read (fails closed, never repairs).

    ``valid`` is the aggregate.  ``absence_of_digest`` reports the audit fact
    that the artifact itself carries no digest; ``artifact_digest`` is the
    locally computed sha256 of the raw file bytes and is never presented as if
    the artifact provided it.
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
# read-only schema validation (mirror of the upstream serialization)
# ---------------------------------------------------------------------------
def _validate(data: Any, path: Path) -> None:
    """Fail closed unless the artifact matches the workflow_runs schema."""
    if not isinstance(data, dict):
        raise WorkflowSnapshotReadError(
            f"workflow snapshot corrupt: root is not an object: {path}"
        )
    for field in (
        "id",
        "contract_version",
        "goal_ref",
        "status",
        "current_task_id",
        "checkpoint_ref",
        "schedule_ref",
        "budget_ref",
        "context_ref",
        "created_at",
        "completed_at",
    ):
        if not isinstance(data.get(field), str):
            raise WorkflowSnapshotReadError(
                f"workflow snapshot corrupt: {field!r} must be a string: {path}"
            )
    plan_ref = data.get("plan_ref")
    if not isinstance(plan_ref, dict) or not isinstance(
        plan_ref.get("plan_id"), str
    ) or not isinstance(plan_ref.get("contract_version"), str):
        raise WorkflowSnapshotReadError(
            f"workflow snapshot corrupt: plan_ref must be {{plan_id, contract_version}}: {path}"
        )
    if data["status"] not in RUN_STATUSES:
        raise WorkflowSnapshotReadError(
            f"workflow snapshot corrupt: unknown status {data['status']!r}: {path}"
        )
    task_states = data.get("task_states")
    if not isinstance(task_states, list):
        raise WorkflowSnapshotReadError(
            f"workflow snapshot corrupt: task_states must be a list: {path}"
        )
    for state in task_states:
        if not isinstance(state, dict):
            raise WorkflowSnapshotReadError(
                f"workflow snapshot corrupt: task_state must be an object: {path}"
            )
        if not isinstance(state.get("task_id"), str):
            raise WorkflowSnapshotReadError(
                f"workflow snapshot corrupt: task_id must be a string: {path}"
            )
        if state.get("status") not in TASK_RUN_STATUSES:
            raise WorkflowSnapshotReadError(
                f"workflow snapshot corrupt: unknown task status "
                f"{state.get('status')!r}: {path}"
            )
        if not isinstance(state.get("attempts"), int):
            raise WorkflowSnapshotReadError(
                f"workflow snapshot corrupt: attempts must be an int: {path}"
            )
        if not isinstance(state.get("result"), dict):
            raise WorkflowSnapshotReadError(
                f"workflow snapshot corrupt: result must be an object: {path}"
            )
        if not isinstance(state.get("error"), str):
            raise WorkflowSnapshotReadError(
                f"workflow snapshot corrupt: error must be a string: {path}"
            )
    if not isinstance(data.get("retry_policy"), dict):
        raise WorkflowSnapshotReadError(
            f"workflow snapshot corrupt: retry_policy must be an object: {path}"
        )


def _payload(
    run_id: str,
    contract_version: str,
    plan_id: str,
    goal_ref: str,
    status: str,
    task_ids: tuple,
    task_statuses: tuple,
    current_task_id: str,
    created_at: str,
    completed_at: str,
    source: str,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "contract_version": contract_version,
        "plan_id": plan_id,
        "goal_ref": goal_ref,
        "status": status,
        "task_ids": list(task_ids),
        "task_statuses": list(task_statuses),
        "current_task_id": current_task_id,
        "created_at": created_at,
        "completed_at": completed_at,
        "source": source,
    }


class WorkflowSnapshotAdapter:
    """Read-only reader over ``data/workflow_runs/<run_id>.json``.

    Public surface (exactly three side-effect-free operations):

    * ``read``    - parse one snapshot into a ``WorkflowRunSnapshotObservation``
    * ``inspect`` - immutable mapping view of an observation
    * ``verify``  - integrity verification (fails closed; never repairs)
    """

    def read(self, snapshot_path) -> WorkflowRunSnapshotObservation:
        """Read one workflow run snapshot (filesystem read-only, fail closed)."""
        path = Path(snapshot_path)
        if not path.is_file():
            raise WorkflowSnapshotReadError(
                f"workflow snapshot NOT_OBSERVED: {path} (missing)"
            )
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise WorkflowSnapshotReadError(
                f"workflow snapshot unreadable: {path}: {exc}"
            ) from exc
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise WorkflowSnapshotReadError(
                f"workflow snapshot corrupt (invalid JSON): {path}: {exc}"
            ) from exc
        _validate(data, path)

        task_ids = tuple(
            state["task_id"] for state in data["task_states"]
        )
        task_statuses = tuple(
            state["status"] for state in data["task_states"]
        )
        run_id = data["id"]
        source = f"{_SOURCE_PREFIX}{run_id}.json"
        if not source.startswith(OFFLINE_SOURCE_PREFIXES):
            raise WorkflowSnapshotReadError(
                "workflow snapshot source escapes the offline-prefix contract"
            )
        observation_digest = _digest(
            _payload(
                run_id=run_id,
                contract_version=data["contract_version"],
                plan_id=data["plan_ref"]["plan_id"],
                goal_ref=data["goal_ref"],
                status=data["status"],
                task_ids=task_ids,
                task_statuses=task_statuses,
                current_task_id=data["current_task_id"],
                created_at=data["created_at"],
                completed_at=data["completed_at"],
                source=source,
            )
        )
        return WorkflowRunSnapshotObservation(
            run_id=run_id,
            contract_version=data["contract_version"],
            plan_id=data["plan_ref"]["plan_id"],
            goal_ref=data["goal_ref"],
            status=data["status"],
            task_count=len(task_ids),
            task_ids=task_ids,
            task_statuses=task_statuses,
            current_task_id=data["current_task_id"],
            created_at=data["created_at"],
            completed_at=data["completed_at"],
            source=source,
            artifact_digest=hashlib.sha256(raw).hexdigest(),
            observation_digest=observation_digest,
        )

    def inspect(
        self, observation: WorkflowRunSnapshotObservation
    ) -> Mapping[str, Any]:
        """Return an immutable mapping view of an observation."""
        if not isinstance(observation, WorkflowRunSnapshotObservation):
            raise WorkflowSnapshotReadError(
                "inspect accepts a WorkflowRunSnapshotObservation only"
            )
        return MappingProxyType(
            {
                "run_id": observation.run_id,
                "contract_version": observation.contract_version,
                "plan_id": observation.plan_id,
                "goal_ref": observation.goal_ref,
                "status": observation.status,
                "task_count": observation.task_count,
                "task_ids": observation.task_ids,
                "task_statuses": observation.task_statuses,
                "current_task_id": observation.current_task_id,
                "created_at": observation.created_at,
                "completed_at": observation.completed_at,
                "source": observation.source,
                "artifact_digest": observation.artifact_digest,
                "observation_digest": observation.observation_digest,
            }
        )

    def verify(
        self,
        observations,
        snapshot_path,
    ) -> WorkflowSnapshotVerification:
        """Verify a snapshot read (fails closed; reports, never repairs)."""
        errors: list[str] = []
        path = Path(snapshot_path)
        store_present = path.is_file()
        artifact_digest = ""
        schema_intact = False
        data_intact = False
        digest_intact = False

        if not store_present:
            errors.append(
                f"workflow snapshot not observed (MISSING): {path}"
            )
        else:
            try:
                raw = path.read_bytes()
            except OSError as exc:
                raw = b""
                errors.append(f"workflow snapshot unreadable: {path}: {exc}")
            artifact_digest = hashlib.sha256(raw).hexdigest()
            try:
                _validate(json.loads(raw.decode("utf-8")), path)
                schema_intact = True
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                errors.append(f"workflow snapshot corrupt (invalid JSON): {exc}")
            except WorkflowSnapshotReadError as exc:
                errors.append(str(exc))

            if schema_intact:
                try:
                    reread = self.read(path)
                except WorkflowSnapshotReadError as exc:
                    reread = None
                    errors.append(str(exc))
                if isinstance(observations, WorkflowRunSnapshotObservation):
                    items = (observations,)
                else:
                    items = tuple(observations)
                if len(items) != 1:
                    errors.append("expected exactly one workflow snapshot observation")
                elif reread is None:
                    errors.append("observation could not be re-read from store")
                elif not isinstance(items[0], WorkflowRunSnapshotObservation):
                    errors.append("observation is not a WorkflowRunSnapshotObservation")
                else:
                    expected = _digest(
                        _payload(
                            run_id=items[0].run_id,
                            contract_version=items[0].contract_version,
                            plan_id=items[0].plan_id,
                            goal_ref=items[0].goal_ref,
                            status=items[0].status,
                            task_ids=items[0].task_ids,
                            task_statuses=items[0].task_statuses,
                            current_task_id=items[0].current_task_id,
                            created_at=items[0].created_at,
                            completed_at=items[0].completed_at,
                            source=items[0].source,
                        )
                    )
                    digest_intact = (
                        items[0].observation_digest == expected
                        and items[0].artifact_digest == artifact_digest
                    )
                    if not digest_intact:
                        errors.append("observation digest does not match the artifact")
                    data_intact = (
                        items[0].run_id == reread.run_id
                        and items[0].status == reread.status
                        and items[0].task_ids == reread.task_ids
                        and items[0].observation_digest == reread.observation_digest
                    )
                    if not data_intact:
                        errors.append("observation diverges from the artifact content")

        valid = (
            store_present
            and schema_intact
            and data_intact
            and digest_intact
            and not errors
        )
        return WorkflowSnapshotVerification(
            valid=valid,
            store_present=store_present,
            schema_intact=schema_intact,
            data_intact=data_intact,
            digest_intact=digest_intact,
            absence_of_digest=True,  # the artifact schema carries no digest
            artifact_digest=artifact_digest,
            errors=tuple(errors),
        )


def to_observed_runtime_state(
    observation: WorkflowRunSnapshotObservation,
) -> ObservedRuntimeState:
    """Map a workflow snapshot observation into the P5.1 observation model.

    Faithful mapping: the run status becomes the observed runtime activation
    verbatim (an observation fact), execution status stays ``unknown`` (the
    snapshot carries per-task statuses, not a single execution field), and the
    observation timestamp is the artifact's own ``created_at`` - never an
    invented ``now()``.
    """
    if not isinstance(observation, WorkflowRunSnapshotObservation):
        raise WorkflowSnapshotReadError(
            "to_observed_runtime_state accepts a WorkflowRunSnapshotObservation only"
        )
    return ObservedRuntimeState(
        subject_id=observation.run_id,
        runtime_activation=observation.status,
        execution_status="unknown",
        observed_at=observation.created_at,
        source=observation.source,
    )
