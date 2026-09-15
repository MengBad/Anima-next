"""P6.2 Runtime Artifact Evidence Integration - READ-ONLY - authoritative contract.

P6.2 consumes P6.1's WorkflowSnapshotAdapter output and integrates it into the
existing P5.5 / P5.6 evidence chain.  It adds NO runtime capability: it never
calls TaskExecutor.execute() / orchestrator.run_plan() / Provider.invoke() /
recovery / repair, never touches execution_ledger, never writes workflow_runs,
and never interprets a runtime fact as authorization.

    data/workflow_runs/<run_id>.json
        -> P6.1 WorkflowSnapshotAdapter.read()     (read-only)
        -> P6.1 to_observed_runtime_state()        (P5.1 ObservedRuntimeState)
        -> P5.2 RuntimeConsumptionAdapter.consume() (read-only)
        -> P5.5 ReconciliationEngine.reconcile()    (read-only, report-only)
        -> P6.2 WorkflowArtifactEvidence            (frozen, slots, digest-bound)
        -> P5.6 closure verification
        -> Evidence only

Hard boundary (this contract enforces it):

  * READ ONLY            - integrate()/verify()/inspect()/audit() never write
  * COMPOSE, NOT REDEFINE- P6.1 / P5.2 / P5.5 are imported read-only and composed;
                           no second observation model, no new runtime capability
  * NO EXECUTION         - no executor / orchestrator / provider / recovery /
                           execution_ledger / dispatch / activation / repair surface
  * FACT != AUTHORITY    - running / completed / failed / cancelled stay observed
                           facts; never upgraded to approved/authorized/activated/
                           executed/deployed
  * MISSING IS STATE     - a missing artifact is NOT_OBSERVED audit state, never
                           a synthetic success, never auto-created
  * ADDITIVE DIGEST      - evidence_digest is additive; P3/P4 governance digests
                           never change
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re

import pytest

from anima.capability_runtime_observation import (
    NOT_EXECUTED,
    OFFLINE_SOURCE_PREFIXES,
    ObservedRuntimeState,
)
from anima.capability_runtime_workflow_snapshot import (
    WorkflowSnapshotAdapter,
    WorkflowSnapshotReadError,
    WorkflowRunSnapshotObservation,
    to_observed_runtime_state,
)

NOW = "2026-08-11T11:40:00+08:00"


# --------------------------------------------------------------------------
# fixtures: REAL workflow run artifacts via the upstream builders
# --------------------------------------------------------------------------
def _real_run(**overrides):
    from anima.constitution.state import (
        PlanRef,
        RunStatus,
        TaskRunStatus,
        TaskState,
        WorkflowRun,
    )

    defaults = dict(
        id="run-p62-1",
        contract_version="1.0.0",
        plan_ref=PlanRef(plan_id="plan-a", contract_version="1.0.0"),
        goal_ref="goal/evolve",
        status=RunStatus.RUNNING,
        task_states=[
            TaskState(
                task_id="t1", status=TaskRunStatus.DONE, attempts=1,
                result={"ok": True}, error="",
            ),
            TaskState(
                task_id="t2", status=TaskRunStatus.PENDING, attempts=0,
                result={}, error="",
            ),
        ],
        current_task_id="t2",
        checkpoint_ref="ck/1",
        retry_policy={"max": 3},
        schedule_ref="",
        budget_ref="",
        context_ref="ctx/1",
        created_at="2026-08-11T10:00:00+00:00",
        completed_at="",
    )
    defaults.update(overrides)
    if isinstance(defaults["status"], str):
        defaults["status"] = RunStatus(defaults["status"])
    return WorkflowRun(**defaults)


def _save_real_run(tmp_path, run, *, run_id: str | None = None):
    from anima.core.persistence import PersistenceProvider
    from anima.runtime.persistence import WorkflowRunStore

    run_id = run_id or run.id
    store = WorkflowRunStore(PersistenceProvider(str(tmp_path)))
    store.save(run)
    artifact = pathlib.Path(tmp_path) / "workflow_runs" / f"{run_id}.json"
    return artifact


def _p4_chain(decision: str = "approved"):
    from anima.capability_evolution_contract import OfflineEvolutionPlanner

    planner = OfflineEvolutionPlanner()
    proposal = planner.propose(
        proposal_id="p62-evo", validation_rfc_id="RFC-0105",
        observed_contradiction="c", inconsistency_eliminated="e",
        change_class="contraction", forbids_expansion="true", proposed_at=NOW,
    )
    impact = planner.assess_impact(
        impact_id="p62-imp", proposal=proposal,
        affected_foundation_refs=("0100#S1",), assessed_at=NOW,
    )
    review = planner.review(
        review_id="p62-rev", proposal=proposal, impact=impact,
        decision=decision, reviewed_at=NOW,
    )
    gate = planner.record_gate(
        gate_id="p62-gate", proposal=proposal, review=review, decision=decision,
        reviewer="senior-developer", evidence_refs=("ev",),
        created_at=NOW, rationale="rationale",
    )
    record = planner.record_evolution(
        record_id="p62-rec", proposal=proposal, review=review, gate=gate,
        recorder="governance-agent", recorded_at=NOW,
    )
    return proposal, review, gate, record


# --------------------------------------------------------------------------
# A. Missing artifact -> NOT_OBSERVED audit state (never fabricated)
# --------------------------------------------------------------------------
def test_a_missing_artifact_is_not_observed(tmp_path):
    from anima.capability_runtime_evidence_integration import (  # RED
        NOT_OBSERVED,
        WorkflowEvidenceIntegrator,
    )

    missing = tmp_path / "workflow_runs" / "ghost.json"
    integrator = WorkflowEvidenceIntegrator()
    # audit() reports NOT_OBSERVED - an evidence state, no synthetic success
    audit = integrator.audit(missing)
    assert audit.state == NOT_OBSERVED
    assert audit.store_present is False
    assert not missing.exists(), "audit must not create a fake artifact"
    # integrate() fails closed - cannot fabricate evidence from nothing
    with pytest.raises(WorkflowSnapshotReadError):
        integrator.integrate(missing, integration_id="p62-a", integrated_at=NOW)


# --------------------------------------------------------------------------
# B. Valid artifact -> integrated evidence, verify round-trips clean
# --------------------------------------------------------------------------
def test_b_valid_artifact_integrates(tmp_path):
    from anima.capability_runtime_evidence_integration import (
        VALID,
        WorkflowEvidenceIntegrator,
    )

    artifact = _save_real_run(tmp_path, _real_run())
    integrator = WorkflowEvidenceIntegrator()
    audit = integrator.audit(artifact)
    assert audit.state == VALID

    evidence = integrator.integrate(
        artifact, integration_id="p62-b", integrated_at=NOW,
    )
    assert evidence.run_id == "run-p62-1"
    assert evidence.status == "running"            # observation fact, verbatim
    assert evidence.execution_status == "unknown"
    assert evidence.artifact_digest
    assert evidence.observation_digest
    assert evidence.consumption_digest
    assert evidence.reconciliation_digest
    assert evidence.evidence_digest
    assert evidence.source.startswith(OFFLINE_SOURCE_PREFIXES)
    assert integrator.verify(evidence, artifact).valid is True


# --------------------------------------------------------------------------
# C. Corrupt artifact -> SCHEMA_FAILURE audit state (no auto-repair)
# --------------------------------------------------------------------------
def test_c_corrupt_artifact_is_schema_failure(tmp_path):
    from anima.capability_runtime_evidence_integration import (
        SCHEMA_FAILURE,
        WorkflowEvidenceIntegrator,
    )

    p = tmp_path / "workflow_runs" / "broken.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not valid json", encoding="utf-8")
    integrator = WorkflowEvidenceIntegrator()
    audit = integrator.audit(p)
    assert audit.state == SCHEMA_FAILURE
    assert audit.schema_intact is False
    with pytest.raises(WorkflowSnapshotReadError):
        integrator.integrate(p, integration_id="p62-c", integrated_at=NOW)
    # nothing repaired
    assert p.read_text(encoding="utf-8") == "{not valid json"


def test_c2_schema_mismatch_audit_state(tmp_path):
    from anima.capability_runtime_evidence_integration import (
        SCHEMA_FAILURE,
        WorkflowEvidenceIntegrator,
    )

    bad = {
        "id": "run-bad", "contract_version": "1.0.0",
        "plan_ref": {"plan_id": "p", "contract_version": "1.0.0"},
        "goal_ref": "g", "status": "exploded", "task_states": [],
        "current_task_id": "", "checkpoint_ref": "", "retry_policy": {},
        "schedule_ref": "", "budget_ref": "", "context_ref": "",
        "created_at": "t", "completed_at": "",
    }
    p = tmp_path / "workflow_runs" / "bad.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(bad), encoding="utf-8")
    audit = WorkflowEvidenceIntegrator().audit(p)
    assert audit.state == SCHEMA_FAILURE
    assert audit.schema_intact is False


# --------------------------------------------------------------------------
# D. Digest mismatch -> verification fails closed (never overwritten)
# --------------------------------------------------------------------------
def test_d_evidence_digest_tamper_fails_closed(tmp_path):
    from anima.capability_runtime_evidence_integration import (
        WorkflowArtifactEvidence,
        WorkflowEvidenceIntegrator,
    )

    artifact = _save_real_run(tmp_path, _real_run())
    integrator = WorkflowEvidenceIntegrator()
    evidence = integrator.integrate(artifact, integration_id="p62-d", integrated_at=NOW)
    before = artifact.read_bytes()

    # tamper the evidence digest on a post-init-bypassed copy
    tampered = object.__new__(WorkflowArtifactEvidence)
    for field in WorkflowArtifactEvidence.__slots__:
        object.__setattr__(tampered, field, getattr(evidence, field))
    object.__setattr__(tampered, "evidence_digest", "0" * 64)

    verification = integrator.verify(tampered, artifact)
    assert verification.valid is False
    assert verification.evidence_digest_intact is False
    assert artifact.read_bytes() == before  # nothing repaired


def test_d2_artifact_change_between_reads_reported(tmp_path):
    """If the artifact changes between reads, divergence is reported - not fixed."""
    from anima.capability_runtime_evidence_integration import WorkflowEvidenceIntegrator

    artifact = _save_real_run(tmp_path, _real_run(status="running"))
    integrator = WorkflowEvidenceIntegrator()
    evidence = integrator.integrate(artifact, integration_id="p62-d2", integrated_at=NOW)

    # the runtime persisted a completed snapshot over the same run id
    artifact.write_text(
        artifact.read_text().replace('"running"', '"completed"'), encoding="utf-8"
    )
    verification = integrator.verify(evidence, artifact)
    assert verification.valid is False  # divergence reported
    # and a fresh integrate reflects the new fact faithfully
    fresh = integrator.integrate(artifact, integration_id="p62-d2b", integrated_at=NOW)
    assert fresh.status == "completed"


# --------------------------------------------------------------------------
# E. Repeated integrate leaves artifact bytes identical
# --------------------------------------------------------------------------
def test_e_repeated_integrate_leaves_artifact_untouched(tmp_path):
    from anima.capability_runtime_evidence_integration import WorkflowEvidenceIntegrator

    artifact = _save_real_run(tmp_path, _real_run())
    before = artifact.read_bytes()
    integrator = WorkflowEvidenceIntegrator()
    first = integrator.integrate(artifact, integration_id="p62-e1", integrated_at=NOW)
    second = integrator.integrate(artifact, integration_id="p62-e2", integrated_at=NOW)
    integrator.verify(second, artifact)
    integrator.inspect(second)
    assert artifact.read_bytes() == before
    assert first.status == second.status
    assert [f.name for f in artifact.parent.iterdir()] == [artifact.name]


# --------------------------------------------------------------------------
# F. THE key invariant: running=true + approved + not_executed coexist,
#    and nothing escalates into execution authorization
# --------------------------------------------------------------------------
def test_f_running_approved_not_executed_coexist_without_escalation(tmp_path):
    from anima.capability_runtime_evidence_integration import WorkflowEvidenceIntegrator

    # governance: approved gate, record stays not_executed
    _proposal, _review, gate, record = _p4_chain(decision="approved")
    assert gate.decision == "approved"
    assert record.execution_status == NOT_EXECUTED
    gate_before = gate.digest
    record_before = record.record_digest

    # runtime: real artifact with status running
    artifact = _save_real_run(tmp_path, _real_run(status="running"))
    evidence = WorkflowEvidenceIntegrator().integrate(
        artifact, integration_id="p62-f", integrated_at=NOW,
    )

    # the three states coexist - no cross-mutation, no escalation
    assert evidence.status == "running"                # observation fact
    assert gate.decision == "approved"                  # governance unchanged
    assert record.execution_status == NOT_EXECUTED      # not upgraded to executed
    for banned in ("approved", "authorized", "activated", "executed", "deployed"):
        assert banned not in evidence.status
        assert banned not in evidence.execution_status
        assert banned not in evidence.reconciliation_status.lower()
    # additive digest: P4 governance digests byte-identical
    assert gate.digest == gate_before
    assert record.record_digest == record_before


# --------------------------------------------------------------------------
# G. No runtime action surface (executor/orchestrator/provider/recovery/ledger)
# --------------------------------------------------------------------------
def test_g_integrator_exposes_no_runtime_action_surface(tmp_path):
    from anima.capability_runtime_evidence_integration import WorkflowEvidenceIntegrator

    integrator = WorkflowEvidenceIntegrator()
    banned = {
        "execute", "run", "dispatch", "activate", "apply", "repair",
        "save", "write", "update", "delete", "patch", "retry", "rerun",
        "deploy", "restart", "start", "stop", "commit", "recover",
        "reconcile_and_fix", "auto_heal",
    }
    surface = {name for name in dir(integrator) if not name.startswith("_")}
    assert surface & banned == set()
    assert surface == {"audit", "integrate", "verify", "inspect"}


# --------------------------------------------------------------------------
# H. P5 chain compatibility: P6.2 evidence enters P5.2/P5.5 consumption
# --------------------------------------------------------------------------
def test_h_p62_evidence_is_p5_consumable(tmp_path):
    from anima.capability_runtime_consumption import RuntimeConsumptionAdapter
    from anima.capability_runtime_evidence_integration import WorkflowEvidenceIntegrator
    from anima.capability_runtime_evidence_reconciliation import ReconciliationEngine

    artifact = _save_real_run(tmp_path, _real_run(status="running"))
    evidence = WorkflowEvidenceIntegrator().integrate(
        artifact, integration_id="p62-h", integrated_at=NOW,
    )
    # the observed state is a P5.1 ObservedRuntimeState - P5.2 can consume it
    from anima.capability_runtime_workflow_snapshot import to_observed_runtime_state

    mapped = to_observed_runtime_state(
        WorkflowSnapshotAdapter().read(artifact)
    )
    consumed = RuntimeConsumptionAdapter().consume(
        mapped, consumption_id="p62-hc", consumed_at=NOW,
    )
    assert consumed.runtime_activation == "running"
    # P5.2 verify() correctly rejects the running claim (scope unchanged)
    ver = RuntimeConsumptionAdapter().verify(consumed, mapped)
    assert ver.no_execution is False
    assert ver.valid is False
    # P5.5 reconciliation accepts the mapped state
    reconciliation = ReconciliationEngine().reconcile(
        (mapped,), reconciliation_id="p62-hr", reconciled_at=NOW,
    )
    assert reconciliation.runtime_state[0].runtime_activation == "running"
    assert ReconciliationEngine().verify(reconciliation, (mapped,)).valid is True


# --------------------------------------------------------------------------
# I/J/K. Static boundary + frozen import + execution_ledger forbidden
# --------------------------------------------------------------------------
_MODULES = (
    "capability_activation_contract",
    "capability_activation_proof",
    "capability_evolution_contract",
    "capability_runtime_observation",
    "capability_runtime_consumption",
    "capability_runtime_observation_store",
    "capability_runtime_evidence_reconciliation",
    "capability_runtime_workflow_snapshot",
    "capability_runtime_evidence_integration",
    "capability_staged_activation",
    "capability_suspend_revocation",
)

_TRANSPORT_NEEDLES = (
    "sub" + "process",
    r"\b" + "ssh" + r"\b",
    "para" + "miko",
    "doc" + "ker",
    "system" + "ctl",
    r"\b" + "requests" + r"\b",
    "htt" + "px",
    "dae" + "mon",
    "ti" + "mer",
    r"\b" + "poll" + r"\b",
    r"\b" + "schedule" + r"\b",
)

_MUTATION_NEEDLES = (
    r"def (apply|execute|activate|mutate|repair|dispatch|write|delete|update|patch|"
    r"start|stop|restart|reload|deploy|save|run|recover)\b",
    r"\." + "apply" + r"\(",
    r"\." + "execute" + r"\(",
    r"\." + "activate" + r"\(",
    r"\." + "mutate" + r"\(",
    r"\." + "write" + r"\(",
    r"\." + "delete" + r"\(",
    r"\." + "update" + r"\(",
    r"\." + "patch" + r"\(",
    r"\." + "repair" + r"\(",
    r"\." + "dispatch" + r"\(",
    r"\." + "start" + r"\(",
    r"\." + "stop" + r"\(",
    "active" + r"\s*=\s*True",
)


def test_i_forbidden_transport_tokens_zero_p3_p62():
    hits = []
    for name in _MODULES:
        source = pathlib.Path(f"anima/{name}.py").read_text(encoding="utf-8")
        for needle in _TRANSPORT_NEEDLES:
            if re.search(needle, source):
                hits.append((name, needle))
    assert hits == [], f"transport tokens present: {hits}"


def test_j_no_mutation_callables_p3_p62():
    hits = []
    for name in _MODULES:
        source = pathlib.Path(f"anima/{name}.py").read_text(encoding="utf-8")
        for needle in _MUTATION_NEEDLES:
            if re.search(needle, source):
                hits.append((name, needle))
    assert hits == [], f"mutation callables present: {hits}"


def test_k1_p62_module_imports_no_frozen_or_ledger_subsystem():
    """P6.2 must not import runtime/constitution/core/watchdog OR
    execution_ledger/executor/orchestrator/providers/recovery."""
    source = pathlib.Path(
        "anima/capability_runtime_evidence_integration.py"
    ).read_text(encoding="utf-8")
    frozen = re.compile(
        r"^\s*(?:from|import)\s+anima\.(runtime|constitution|core|watchdog|"
        r"lifecycle|foundation|memory|identity|capability_contract)\b",
        re.M,
    )
    hits = frozen.findall(source)
    assert hits == [], f"frozen subsystem imports in P6.2: {hits}"
    # execution_ledger / executor / orchestrator / providers / recovery forbidden
    for name in ("execution_ledger", "executor", "orchestrator", "providers", "recovery"):
        pattern = re.compile(r"^\s*(?:from|import)\s+.*" + name, re.M)
        assert pattern.findall(source) == [], f"forbidden {name} import in P6.2"


def test_k2_no_frozen_imports_across_p3_p62():
    frozen = re.compile(
        r"^\s*(?:from|import)\s+anima\."
        r"(?!capability_(?:activation_contract|activation_proof|staged_activation|"
        r"suspend_revocation|evolution_contract|runtime_observation|runtime_consumption|"
        r"runtime_observation_store|runtime_evidence_reconciliation|"
        r"runtime_workflow_snapshot|runtime_evidence_integration))"
        r"(lifecycle|constitution|foundation|memory|identity|runtime|capability_contract)\b",
        re.M,
    )
    hits = []
    for name in _MODULES:
        source = pathlib.Path(f"anima/{name}.py").read_text(encoding="utf-8")
        for match in frozen.findall(source):
            hits.append((name, match))
    assert hits == [], f"frozen subsystem imports present: {hits}"
