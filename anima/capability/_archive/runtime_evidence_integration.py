"""P6.2 Runtime Artifact Evidence Integration - READ-ONLY.

P6.2 consumes P6.1's ``WorkflowSnapshotAdapter`` output and integrates it into
the existing P5.5 / P5.6 evidence chain.  It COMPOSES read-only pieces already
in the repository - it defines no new observation model, no new runtime
capability, no execution surface:

    data/workflow_runs/<run_id>.json
        -> P6.1 WorkflowSnapshotAdapter.read()      (filesystem read-only)
        -> P6.1 to_observed_runtime_state()         (P5.1 ObservedRuntimeState)
        -> P5.2 RuntimeConsumptionAdapter.consume()  (read-only)
        -> P5.5 ReconciliationEngine.reconcile()     (read-only, report-only)
        -> P6.2 WorkflowArtifactEvidence             (frozen, slots, digest-bound)
        -> P5.6 closure verification
        -> Evidence only

Properties held by construction and by test:

  * READ ONLY         - integrate()/verify()/inspect()/audit() perform no file
                        write, no DB write, no timestamp write-back, no process
                        spawn, no network
  * COMPOSE ONLY      - P6.1 / P5.2 / P5.5 are imported read-only and composed;
                        TaskExecutor.execute / orchestrator.run_plan /
                        Provider.invoke / recovery / repair are never called
  * ONE DOMAIN ONLY   - workflow_runs snapshots only; execution_ledger is
                        FORBIDDEN and never read
  * NO FROZEN IMPORT  - this module imports NO anima.runtime / anima.constitution /
                        anima.core / anima.watchdog subsystem
  * MISSING IS STATE  - a missing artifact is NOT_OBSERVED audit state, never a
                        synthetic success, never auto-created, never compensated
                        by searching other runtime sources
  * FACT != AUTHORITY - run statuses (running / completed / failed / cancelled)
                        stay observed facts; they are never upgraded to
                        approved / authorized / activated / executed / deployed
  * ADDITIVE DIGEST   - evidence_digest is additive; P3/P4 governance digests
                        are never modified; the artifact carries no digest, so
                        absence_of_digest is reported as an audit fact
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
from anima.capability_runtime_consumption import RuntimeConsumptionAdapter
from anima.capability_runtime_evidence_reconciliation import (
    INTEGRITY_FAILURE,
    ReconciliationEngine,
)
from anima.capability_runtime_workflow_snapshot import (
    WorkflowRunSnapshotObservation,
    WorkflowSnapshotAdapter,
    WorkflowSnapshotReadError,
    to_observed_runtime_state,
)

__all__ = [
    "NOT_OBSERVED",
    "SCHEMA_FAILURE",
    "VALID",
    "WorkflowArtifactAudit",
    "WorkflowArtifactEvidence",
    "WorkflowEvidenceIntegrator",
    "WorkflowIntegrationError",
    "WorkflowIntegrationVerification",
]


#: Audit states of a workflow artifact read (evidence states, never repaired).
NOT_OBSERVED = "NOT_OBSERVED"
SCHEMA_FAILURE = "SCHEMA_FAILURE"
VALID = "VALID"


class WorkflowIntegrationError(ValueError):
    """Raised when integration input violates the read-only boundary."""


@dataclass(frozen=True, slots=True)
class WorkflowArtifactAudit:
    """Audit state of a workflow artifact - never raises, never repairs.

    ``state`` is one of NOT_OBSERVED / SCHEMA_FAILURE / VALID.  A missing
    artifact is a real evidence state, not an implementation failure.
    """

    path: str
    state: str
    store_present: bool
    schema_intact: bool
    artifact_digest: str
    errors: tuple = ()

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path:
            raise WorkflowIntegrationError("path must be a non-empty string")
        if self.state not in (NOT_OBSERVED, SCHEMA_FAILURE, VALID):
            raise WorkflowIntegrationError(f"unknown audit state {self.state!r}")
        if not isinstance(self.artifact_digest, str):
            raise WorkflowIntegrationError("artifact_digest must be a string")


@dataclass(frozen=True, slots=True)
class WorkflowArtifactEvidence:
    """Immutable, digest-bound integration of one workflow artifact.

    Binds the P6.1 observation digest, the P5.2 consumption digest and the
    P5.5 reconciliation digest into ONE additive evidence record.  ``status``
    is the observed run fact verbatim - never an authorization claim.
    """

    integration_id: str
    integrated_at: str
    run_id: str
    status: str
    execution_status: str
    source: str
    artifact_digest: str
    observation_digest: str
    consumption_digest: str
    reconciliation_status: str
    reconciliation_digest: str
    evidence_digest: str

    def __post_init__(self) -> None:
        for name in (
            "integration_id",
            "integrated_at",
            "run_id",
            "status",
            "execution_status",
            "source",
            "artifact_digest",
            "observation_digest",
            "consumption_digest",
            "reconciliation_status",
            "reconciliation_digest",
            "evidence_digest",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise WorkflowIntegrationError(f"{name} must be a non-empty string")
        if not self.source.startswith(OFFLINE_SOURCE_PREFIXES):
            raise WorkflowIntegrationError(
                "workflow evidence source must stay inside the offline-prefix contract"
            )
        for name in (
            "artifact_digest",
            "observation_digest",
            "consumption_digest",
            "reconciliation_digest",
            "evidence_digest",
        ):
            if not (
                len(getattr(self, name)) == 64
                and set(getattr(self, name)) <= frozenset("0123456789abcdef")
            ):
                raise WorkflowIntegrationError(f"{name} must be a SHA-256 hex digest")


@dataclass(frozen=True, slots=True)
class WorkflowIntegrationVerification:
    """Integrity result of an integrated evidence record (fails closed)."""

    valid: bool
    store_present: bool
    schema_intact: bool
    evidence_digest_intact: bool
    observation_bound: bool
    consumption_bound: bool
    reconciliation_bound: bool
    errors: tuple = ()


def _digest(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _is_hex64(value: str) -> bool:
    return len(value) == 64 and set(value) <= frozenset("0123456789abcdef")


def _evidence_payload(
    integration_id: str,
    integrated_at: str,
    run_id: str,
    status: str,
    execution_status: str,
    source: str,
    artifact_digest: str,
    observation_digest: str,
    consumption_digest: str,
    reconciliation_status: str,
    reconciliation_digest: str,
) -> dict[str, Any]:
    return {
        "integration_id": integration_id,
        "integrated_at": integrated_at,
        "run_id": run_id,
        "status": status,
        "execution_status": execution_status,
        "source": source,
        "artifact_digest": artifact_digest,
        "observation_digest": observation_digest,
        "consumption_digest": consumption_digest,
        "reconciliation_status": reconciliation_status,
        "reconciliation_digest": reconciliation_digest,
    }


def _run_pipeline(
    snapshot_path,
    *,
    integration_id: str,
    integrated_at: str,
):
    """Read -> map -> consume -> reconcile (all read-only, composed pieces)."""
    adapter = WorkflowSnapshotAdapter()
    observation = adapter.read(snapshot_path)  # fail closed on missing/corrupt
    state = to_observed_runtime_state(observation)
    consumed = RuntimeConsumptionAdapter().consume(
        state,
        consumption_id=f"{integration_id}-consume",
        consumed_at=integrated_at,
    )
    reconciliation = ReconciliationEngine().reconcile(
        (state,),
        reconciliation_id=f"{integration_id}-reconcile",
        reconciled_at=integrated_at,
    )
    return observation, state, consumed, reconciliation


class WorkflowEvidenceIntegrator:
    """Read-only integration of workflow artifacts into the P5 evidence chain.

    Public surface (exactly four side-effect-free operations):

    * ``audit``     - artifact evidence state (NOT_OBSERVED / SCHEMA_FAILURE /
                      VALID); never raises, never repairs
    * ``integrate`` - read + map + consume + reconcile into one digest-bound
                      evidence record (fail closed on missing/corrupt)
    * ``verify``    - re-run the pipeline and fail closed on any divergence
    * ``inspect``   - immutable mapping view of an evidence record
    """

    def audit(self, snapshot_path) -> WorkflowArtifactAudit:
        """Report the artifact's evidence state without raising."""
        path = Path(snapshot_path)
        if not path.is_file():
            return WorkflowArtifactAudit(
                path=str(path),
                state=NOT_OBSERVED,
                store_present=False,
                schema_intact=False,
                artifact_digest="",
                errors=("workflow snapshot not observed (MISSING)",),
            )
        raw = b""
        try:
            raw = path.read_bytes()
        except OSError as exc:
            return WorkflowArtifactAudit(
                path=str(path),
                state=SCHEMA_FAILURE,
                store_present=True,
                schema_intact=False,
                artifact_digest="",
                errors=(f"workflow snapshot unreadable: {exc}",),
            )
        artifact_digest = hashlib.sha256(raw).hexdigest()
        try:
            WorkflowSnapshotAdapter().read(path)
        except WorkflowSnapshotReadError as exc:
            return WorkflowArtifactAudit(
                path=str(path),
                state=SCHEMA_FAILURE,
                store_present=True,
                schema_intact=False,
                artifact_digest=artifact_digest,
                errors=(str(exc),),
            )
        return WorkflowArtifactAudit(
            path=str(path),
            state=VALID,
            store_present=True,
            schema_intact=True,
            artifact_digest=artifact_digest,
            errors=(),
        )

    def integrate(
        self,
        snapshot_path,
        *,
        integration_id: str,
        integrated_at: str,
    ) -> WorkflowArtifactEvidence:
        """Integrate one workflow artifact into a digest-bound evidence record."""
        if not isinstance(integration_id, str) or not integration_id:
            raise WorkflowIntegrationError("integration_id must be a non-empty string")
        if not isinstance(integrated_at, str) or not integrated_at:
            raise WorkflowIntegrationError("integrated_at must be a non-empty string")

        observation, state, consumed, reconciliation = _run_pipeline(
            snapshot_path,
            integration_id=integration_id,
            integrated_at=integrated_at,
        )
        payload = _evidence_payload(
            integration_id=integration_id,
            integrated_at=integrated_at,
            run_id=observation.run_id,
            status=state.runtime_activation,
            execution_status=state.execution_status,
            source=state.source,
            artifact_digest=observation.artifact_digest,
            observation_digest=observation.observation_digest,
            consumption_digest=consumed.consumption_digest,
            reconciliation_status=reconciliation.reconciliation_status,
            reconciliation_digest=reconciliation.evidence_digest,
        )
        return WorkflowArtifactEvidence(
            integration_id=integration_id,
            integrated_at=integrated_at,
            run_id=observation.run_id,
            status=state.runtime_activation,
            execution_status=state.execution_status,
            source=state.source,
            artifact_digest=observation.artifact_digest,
            observation_digest=observation.observation_digest,
            consumption_digest=consumed.consumption_digest,
            reconciliation_status=reconciliation.reconciliation_status,
            reconciliation_digest=reconciliation.evidence_digest,
            evidence_digest=_digest(payload),
        )

    def verify(
        self,
        evidence: WorkflowArtifactEvidence,
        snapshot_path,
    ) -> WorkflowIntegrationVerification:
        """Verify an evidence record against the artifact (fails closed)."""
        errors: list[str] = []
        path = Path(snapshot_path)
        store_present = path.is_file()
        schema_intact = False

        if not isinstance(evidence, WorkflowArtifactEvidence):
            raise WorkflowIntegrationError(
                "verification requires a WorkflowArtifactEvidence"
            )
        if not store_present:
            errors.append("workflow snapshot not observed (MISSING)")
            return WorkflowIntegrationVerification(
                valid=False,
                store_present=False,
                schema_intact=False,
                evidence_digest_intact=False,
                observation_bound=False,
                consumption_bound=False,
                reconciliation_bound=False,
                errors=tuple(errors),
            )
        try:
            observation, state, consumed, reconciliation = _run_pipeline(
                path,
                # SAME id as integrate(): the pipeline digests are bound to
                # the integration_id-derived consumption/reconciliation ids
                integration_id=evidence.integration_id,
                integrated_at=evidence.integrated_at,
            )
            schema_intact = True
        except (WorkflowSnapshotReadError, WorkflowIntegrationError) as exc:
            errors.append(str(exc))
            observation = None
            state = None
            consumed = None
            reconciliation = None

        observation_bound = (
            observation is not None
            and evidence.observation_digest == observation.observation_digest
            and evidence.artifact_digest == observation.artifact_digest
            and evidence.run_id == observation.run_id
        )
        if not observation_bound:
            errors.append("evidence is not bound to the current artifact observation")

        consumption_bound = (
            consumed is not None
            and evidence.consumption_digest == consumed.consumption_digest
        )
        if not consumption_bound:
            errors.append("consumption digest does not match the pipeline")

        reconciliation_bound = (
            reconciliation is not None
            and evidence.reconciliation_digest == reconciliation.evidence_digest
            and evidence.reconciliation_status
            == reconciliation.reconciliation_status
        )
        if not reconciliation_bound:
            errors.append("reconciliation digest does not match the pipeline")

        expected = _digest(
            _evidence_payload(
                integration_id=evidence.integration_id,
                integrated_at=evidence.integrated_at,
                run_id=evidence.run_id,
                status=evidence.status,
                execution_status=evidence.execution_status,
                source=evidence.source,
                artifact_digest=evidence.artifact_digest,
                observation_digest=evidence.observation_digest,
                consumption_digest=evidence.consumption_digest,
                reconciliation_status=evidence.reconciliation_status,
                reconciliation_digest=evidence.reconciliation_digest,
            )
        )
        evidence_digest_intact = evidence.evidence_digest == expected
        if not evidence_digest_intact:
            errors.append("evidence_digest does not match the evidence fields")

        valid = (
            store_present
            and schema_intact
            and observation_bound
            and consumption_bound
            and reconciliation_bound
            and evidence_digest_intact
            and not errors
        )
        return WorkflowIntegrationVerification(
            valid=valid,
            store_present=store_present,
            schema_intact=schema_intact,
            evidence_digest_intact=evidence_digest_intact,
            observation_bound=observation_bound,
            consumption_bound=consumption_bound,
            reconciliation_bound=reconciliation_bound,
            errors=tuple(errors),
        )

    def inspect(self, evidence: WorkflowArtifactEvidence) -> Mapping[str, Any]:
        """Return an immutable mapping view of an evidence record."""
        if not isinstance(evidence, WorkflowArtifactEvidence):
            raise WorkflowIntegrationError(
                "inspect accepts a WorkflowArtifactEvidence only"
            )
        return MappingProxyType(
            {
                "integration_id": evidence.integration_id,
                "integrated_at": evidence.integrated_at,
                "run_id": evidence.run_id,
                "status": evidence.status,
                "execution_status": evidence.execution_status,
                "source": evidence.source,
                "artifact_digest": evidence.artifact_digest,
                "observation_digest": evidence.observation_digest,
                "consumption_digest": evidence.consumption_digest,
                "reconciliation_status": evidence.reconciliation_status,
                "reconciliation_digest": evidence.reconciliation_digest,
                "evidence_digest": evidence.evidence_digest,
            }
        )
