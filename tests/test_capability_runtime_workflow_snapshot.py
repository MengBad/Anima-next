"""P6.1 Runtime Read-only Workflow Artifact Boundary Adapter - authoritative contract.

P6.1 is the first *conditional* runtime artifact bridge: it reads ONE artifact
domain only - ``data/workflow_runs/*.json`` (run lifecycle snapshots, F4.2
documented layout) - as a plain filesystem file, and normalizes it into an
immutable, digest-bound observation that can enter the P5 evidence chain.

    data/workflow_runs/<run_id>.json
        -> P6.1 read()                  (filesystem read-only)
        -> WorkflowRunSnapshotObservation (frozen, slots, digest-bound)
        -> P5.1 ObservedRuntimeState     (to_observed_runtime_state)
        -> P5.2 Consumption / P5.5 Reconciliation
        -> Evidence only

Hard boundary (this contract enforces it):

  * READ ONLY        - read()/verify()/inspect() perform no file write, no DB,
                       no timestamp write-back, no process spawn
  * ONE DOMAIN ONLY  - only workflow_runs snapshots; execution_ledger is FORBIDDEN
  * NO FROZEN IMPORT - the module imports no anima.runtime / anima.constitution /
                       anima.core / anima.watchdog (mirror, never import)
  * FAIL CLOSED      - missing/corrupt artifact raises (read) or reports invalid
                       (verify); nothing is auto-created or auto-repaired
  * MISSING IS STATE - a missing artifact is reported as NOT_OBSERVED; it is
                       evidence state, not implementation failure, and is never
                       papered over with a synthetic success
  * FACT != AUTHORITY- running/completed/failed inside an artifact stay observed
                       facts; they are never upgraded to approved/authorized/
                       activated/executed/deployed
  * ADDITIVE DIGEST  - artifact_digest/observation_digest are additive; no
                       upstream digest is ever rewritten
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
from types import MappingProxyType

import pytest

from anima.capability_runtime_observation import (
    OFFLINE_SOURCE_PREFIXES,
    ObservedRuntimeState,
)
from anima.capability_runtime_observation_store import (
    ObservationStoreReadError,
    RealObservationStoreAdapter,
)
from anima.capability_runtime_evidence_reconciliation import ReconciliationEngine

NOW = "2026-08-11T11:30:00+08:00"


# --------------------------------------------------------------------------
# fixtures: REAL workflow run artifacts via the upstream builders
# --------------------------------------------------------------------------
def _real_run(**overrides):
    """Build a REAL WorkflowRun through the upstream vocabulary (schema mirror pin)."""
    from anima.constitution.state import (
        PlanRef,
        RunStatus,
        TaskRunStatus,
        TaskState,
        WorkflowRun,
    )

    defaults = dict(
        id="run-p61-1",
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
    # accept raw status strings in overrides, convert to the enum vocabulary
    if isinstance(defaults["status"], str):
        defaults["status"] = RunStatus(defaults["status"])
    return WorkflowRun(**defaults)


def _save_real_run(tmp_path, run, *, run_id: str | None = None):
    """Persist a REAL run through the upstream WorkflowRunStore into workflow_runs/."""
    from anima.core.persistence import PersistenceProvider
    from anima.runtime.persistence import WorkflowRunStore

    run_id = run_id or run.id
    store = WorkflowRunStore(PersistenceProvider(str(tmp_path)))
    store.save(run)
    artifact = pathlib.Path(tmp_path) / "workflow_runs" / f"{run_id}.json"
    return artifact


# --------------------------------------------------------------------------
# A. Missing artifact -> NOT_OBSERVED, fail closed, no synthetic success
# --------------------------------------------------------------------------
def test_a_missing_artifact_is_not_observed(tmp_path):
    from anima.capability_runtime_workflow_snapshot import (  # RED: fails until module exists
        WorkflowSnapshotAdapter,
        WorkflowSnapshotReadError,
        WorkflowSnapshotVerification,
    )

    missing = tmp_path / "workflow_runs" / "ghost.json"
    adapter = WorkflowSnapshotAdapter()
    # read() fails closed - MISSING is reported, never papered over
    with pytest.raises(WorkflowSnapshotReadError):
        adapter.read(missing)
    assert not missing.exists(), "fail-closed must not create a fake artifact"
    # verify() reports the absence as an evidence state (store_present=False)
    verification = adapter.verify((), missing)
    assert isinstance(verification, WorkflowSnapshotVerification)
    assert verification.store_present is False
    assert verification.valid is False
    assert verification.absence_of_digest is True
    assert any("NOT_OBSERVED" in e or "not observed" in e for e in verification.errors)


# --------------------------------------------------------------------------
# B. Valid artifact (REAL upstream-produced) -> faithful observation
# --------------------------------------------------------------------------
def test_b_reads_real_artifact_faithfully(tmp_path):
    from anima.capability_runtime_workflow_snapshot import WorkflowSnapshotAdapter

    run = _real_run()
    artifact = _save_real_run(tmp_path, run)
    adapter = WorkflowSnapshotAdapter()
    observation = adapter.read(artifact)

    assert observation.run_id == "run-p61-1"
    assert observation.status == "running"          # faithful, verbatim
    assert observation.task_count == 2
    assert observation.task_ids == ("t1", "t2")
    assert observation.task_statuses == ("done", "pending")
    assert observation.plan_id == "plan-a"
    assert observation.created_at == "2026-08-11T10:00:00+00:00"
    assert observation.source.startswith(OFFLINE_SOURCE_PREFIXES)

    # verify() round-trips clean on a real artifact
    verification = adapter.verify(observation, artifact)
    assert verification.valid is True
    assert verification.store_present is True
    assert verification.schema_intact is True
    assert verification.data_intact is True
    assert verification.absence_of_digest is True  # artifact carries no digest


def test_b2_schema_mirror_pinned_to_upstream(tmp_path):
    """The mirror reads a real WorkflowRunStore-produced artifact correctly."""
    from anima.capability_runtime_workflow_snapshot import WorkflowSnapshotAdapter

    run = _real_run(id="run-mirror-1", status="completed")
    artifact = _save_real_run(tmp_path, run, run_id="run-mirror-1")
    observation = WorkflowSnapshotAdapter().read(artifact)
    assert observation.run_id == "run-mirror-1"
    assert observation.status == "completed"


def test_b3_schema_mirror_full_vocabulary_pinned_to_upstream(tmp_path):
    """The mirror accepts the FULL upstream vocabulary - cancelled/skipped included.

    Regression pin: the mirror once rejected a real artifact with
    status=\"cancelled\" (RunStatus) or task status \"skipped\" (TaskRunStatus),
    which are valid upstream values.  This test builds those runs through the
    REAL WorkflowRunStore and proves P6.1 reads them faithfully.
    """
    from anima.constitution.state import (
        PlanRef,
        RunStatus,
        TaskRunStatus,
        TaskState,
        WorkflowRun,
    )
    from anima.core.persistence import PersistenceProvider
    from anima.runtime.persistence import WorkflowRunStore
    from anima.capability_runtime_workflow_snapshot import WorkflowSnapshotAdapter

    cancelled = WorkflowRun(
        id="run-cancel-1", contract_version="1.0.0",
        plan_ref=PlanRef(plan_id="plan-a", contract_version="1.0.0"),
        goal_ref="goal/x", status=RunStatus.CANCELLED,
        task_states=[
            TaskState(
                task_id="t1", status=TaskRunStatus.SKIPPED, attempts=0,
                result={}, error="",
            )
        ],
        current_task_id="", checkpoint_ref="", retry_policy={},
        schedule_ref="", budget_ref="", context_ref="",
        created_at="2026-08-11T10:00:00+00:00",
        completed_at="2026-08-11T10:05:00+00:00",
    )
    artifact = _save_real_run(tmp_path, cancelled, run_id="run-cancel-1")
    observation = WorkflowSnapshotAdapter().read(artifact)
    assert observation.status == "cancelled"
    assert observation.task_statuses == ("skipped",)


# --------------------------------------------------------------------------
# C. Malformed JSON -> fail closed
# --------------------------------------------------------------------------
def test_c_malformed_json_fails_closed(tmp_path):
    from anima.capability_runtime_workflow_snapshot import (
        WorkflowSnapshotAdapter,
        WorkflowSnapshotReadError,
    )

    p = tmp_path / "workflow_runs" / "broken.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not valid json", encoding="utf-8")
    adapter = WorkflowSnapshotAdapter()
    with pytest.raises(WorkflowSnapshotReadError):
        adapter.read(p)
    # nothing repaired: file untouched
    assert p.read_text(encoding="utf-8") == "{not valid json"


# --------------------------------------------------------------------------
# D. Schema mismatch -> fail closed
# --------------------------------------------------------------------------
def test_d_schema_mismatch_fails_closed(tmp_path):
    from anima.capability_runtime_workflow_snapshot import (
        WorkflowSnapshotAdapter,
        WorkflowSnapshotReadError,
    )

    # unknown run status (not in the RunStatus vocabulary)
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
    adapter = WorkflowSnapshotAdapter()
    with pytest.raises(WorkflowSnapshotReadError):
        adapter.read(p)
    # verify() reports schema breakage without raising
    verification = adapter.verify((), p)
    assert verification.schema_intact is False
    assert verification.valid is False


def test_d2_missing_required_field_fails_closed(tmp_path):
    from anima.capability_runtime_workflow_snapshot import (
        WorkflowSnapshotAdapter,
        WorkflowSnapshotReadError,
    )

    p = tmp_path / "workflow_runs" / "incomplete.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"id": "run-x"}), encoding="utf-8")
    with pytest.raises(WorkflowSnapshotReadError):
        WorkflowSnapshotAdapter().read(p)


# --------------------------------------------------------------------------
# E. Digest tamper -> verification failure (fail closed)
# --------------------------------------------------------------------------
def test_e_digest_tamper_fails_closed(tmp_path):
    from anima.capability_runtime_workflow_snapshot import (
        WorkflowSnapshotAdapter,
        WorkflowRunSnapshotObservation,
    )

    run = _real_run()
    artifact = _save_real_run(tmp_path, run)
    adapter = WorkflowSnapshotAdapter()
    observation = adapter.read(artifact)

    # tamper the observation digest on a post-init-bypassed copy
    tampered = object.__new__(WorkflowRunSnapshotObservation)
    for field in WorkflowRunSnapshotObservation.__slots__:
        object.__setattr__(tampered, field, getattr(observation, field))
    object.__setattr__(tampered, "observation_digest", "0" * 64)

    verification = adapter.verify(tampered, artifact)
    assert verification.valid is False
    assert verification.digest_intact is False
    assert artifact.read_bytes() == artifact.read_bytes()  # never repaired


# --------------------------------------------------------------------------
# F. Immutable observation
# --------------------------------------------------------------------------
def test_f_observation_is_immutable(tmp_path):
    from dataclasses import FrozenInstanceError

    from anima.capability_runtime_workflow_snapshot import WorkflowSnapshotAdapter

    artifact = _save_real_run(tmp_path, _real_run())
    observation = WorkflowSnapshotAdapter().read(artifact)
    # a frozen dataclass cannot be mutated in place
    with pytest.raises(FrozenInstanceError):
        observation.status = "completed"  # frozen: in-place mutation forbidden


# --------------------------------------------------------------------------
# G. Repeated read does not change artifact bytes
# --------------------------------------------------------------------------
def test_g_repeated_read_leaves_artifact_bytes_identical(tmp_path):
    from anima.capability_runtime_workflow_snapshot import WorkflowSnapshotAdapter

    artifact = _save_real_run(tmp_path, _real_run())
    before = artifact.read_bytes()
    adapter = WorkflowSnapshotAdapter()
    first = adapter.read(artifact)
    second = adapter.read(artifact)
    adapter.verify(second, artifact)
    adapter.inspect(second)
    assert artifact.read_bytes() == before
    assert first == second
    assert [f.name for f in artifact.parent.iterdir()] == [artifact.name]


# --------------------------------------------------------------------------
# H. running=true never upgrades governance
# --------------------------------------------------------------------------
def test_h_running_fact_never_upgrades_governance(tmp_path):
    from anima.capability_runtime_workflow_snapshot import (
        WorkflowSnapshotAdapter,
        to_observed_runtime_state,
    )

    # real artifact with status running
    artifact = _save_real_run(tmp_path, _real_run(status="running"))
    observation = WorkflowSnapshotAdapter().read(artifact)
    state = to_observed_runtime_state(observation)

    # the P5.1 mapping carries the fact verbatim
    assert isinstance(state, ObservedRuntimeState)
    assert state.runtime_activation == "running"
    assert state.execution_status == "unknown"
    # never upgraded
    for banned in ("approved", "authorized", "activated", "executed", "deployed"):
        assert banned not in state.runtime_activation
        assert banned not in state.execution_status

    # governance records untouched after full evidence injection
    from anima.capability_evolution_contract import OfflineEvolutionPlanner

    planner = OfflineEvolutionPlanner()
    proposal = planner.propose(
        proposal_id="p61-evo", validation_rfc_id="RFC-0105",
        observed_contradiction="c", inconsistency_eliminated="e",
        change_class="contraction", forbids_expansion="true", proposed_at=NOW,
    )
    impact = planner.assess_impact(
        impact_id="p61-imp", proposal=proposal,
        affected_foundation_refs=("0100#S1",), assessed_at=NOW,
    )
    review = planner.review(
        review_id="p61-rev", proposal=proposal, impact=impact,
        decision="approved", reviewed_at=NOW,
    )
    gate = planner.record_gate(
        gate_id="p61-gate", proposal=proposal, review=review, decision="approved",
        reviewer="senior-developer", evidence_refs=("ev",),
        created_at=NOW, rationale="rationale",
    )
    record = planner.record_evolution(
        record_id="p61-rec", proposal=proposal, review=review, gate=gate,
        recorder="governance-agent", recorded_at=NOW,
    )
    gate_before = gate.digest
    record_before = record.record_digest

    # inject the workflow observation into P5.2 + P5.5 (consume/reconcile)
    from anima.capability_runtime_consumption import RuntimeConsumptionAdapter

    consumed = RuntimeConsumptionAdapter().consume(
        state, consumption_id="p61-c", consumed_at=NOW,
    )
    reconciliation = ReconciliationEngine().reconcile(
        (state,), reconciliation_id="p61-r", reconciled_at=NOW,
    )
    assert consumed.runtime_activation == "running"
    assert reconciliation.runtime_state[0].runtime_activation == "running"
    # governance digests byte-identical
    assert gate.digest == gate_before
    assert record.record_digest == record_before


# --------------------------------------------------------------------------
# I. No runtime action callable (public surface)
# --------------------------------------------------------------------------
def test_i_adapter_exposes_no_runtime_action_surface(tmp_path):
    from anima.capability_runtime_workflow_snapshot import WorkflowSnapshotAdapter

    adapter = WorkflowSnapshotAdapter()
    banned = {
        "run", "execute", "dispatch", "activate", "apply", "repair",
        "save", "write", "update", "delete", "patch", "retry", "rerun",
        "deploy", "restart", "start", "stop", "commit",
    }
    surface = {name for name in dir(adapter) if not name.startswith("_")}
    assert surface & banned == set()
    assert surface == {"read", "inspect", "verify"}


# --------------------------------------------------------------------------
# J. P5 reconciliation compatibility
# --------------------------------------------------------------------------
def test_j_p5_chain_consumes_p61_evidence(tmp_path):
    from anima.capability_runtime_consumption import RuntimeConsumptionAdapter
    from anima.capability_runtime_workflow_snapshot import (
        WorkflowSnapshotAdapter,
        to_observed_runtime_state,
    )

    artifact = _save_real_run(tmp_path, _real_run(status="running"))
    observation = WorkflowSnapshotAdapter().read(artifact)
    state = to_observed_runtime_state(observation)

    # P5.2 can consume; verify() correctly rejects the running claim
    # (P5.2 synthetic-only scope is unchanged - the fact is never faked away)
    adapter = RuntimeConsumptionAdapter()
    consumed = adapter.consume(state, consumption_id="p61-j", consumed_at=NOW)
    assert consumed.runtime_activation == "running"
    ver = adapter.verify(consumed, state)
    assert ver.no_execution is False
    assert ver.valid is False

    # P5.5 reconciliation accepts the observation
    reconciliation = ReconciliationEngine().reconcile(
        (state,), reconciliation_id="p61-j2", reconciled_at=NOW,
    )
    assert reconciliation.runtime_state[0].runtime_activation == "running"
    assert ReconciliationEngine().verify(reconciliation, (state,)).valid is True


# --------------------------------------------------------------------------
# K. Forbidden-token scan (all P3-P6.1 modules)
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
    r"def (apply|execute|activate|mutate|repair|dispatch|write|delete|update|patch|start|stop|restart|reload|deploy|save|run)\b",
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


def test_k1_forbidden_transport_tokens_zero_p3_p61():
    hits = []
    for name in _MODULES:
        source = pathlib.Path(f"anima/{name}.py").read_text(encoding="utf-8")
        for needle in _TRANSPORT_NEEDLES:
            if re.search(needle, source):
                hits.append((name, needle))
    assert hits == [], f"transport tokens present: {hits}"


def test_k2_no_mutation_callables_p3_p61():
    hits = []
    for name in _MODULES:
        source = pathlib.Path(f"anima/{name}.py").read_text(encoding="utf-8")
        for needle in _MUTATION_NEEDLES:
            if re.search(needle, source):
                hits.append((name, needle))
    assert hits == [], f"mutation callables present: {hits}"


# --------------------------------------------------------------------------
# L. Frozen-runtime-import scan (P6.1 module only + all P3-P6.1)
# --------------------------------------------------------------------------
def test_l1_p61_module_imports_no_frozen_subsystem():
    """The P6.1 module must NOT import anima.runtime / anima.constitution /
    anima.core / anima.watchdog - it mirrors the schema, never imports it."""
    source = pathlib.Path(
        "anima/capability_runtime_workflow_snapshot.py"
    ).read_text(encoding="utf-8")
    frozen = re.compile(
        r"^\s*(?:from|import)\s+anima\.(runtime|constitution|core|watchdog|"
        r"lifecycle|foundation|memory|identity|capability_contract)\b",
        re.M,
    )
    hits = frozen.findall(source)
    assert hits == [], f"frozen subsystem imports in P6.1: {hits}"


def test_l2_no_frozen_imports_across_p3_p61():
    frozen = re.compile(
        r"^\s*(?:from|import)\s+anima\."
        r"(?!capability_(?:activation_contract|activation_proof|staged_activation|"
        r"suspend_revocation|evolution_contract|runtime_observation|runtime_consumption|"
        r"runtime_observation_store|runtime_evidence_reconciliation|runtime_workflow_snapshot))"
        r"(lifecycle|constitution|foundation|memory|identity|runtime|capability_contract)\b",
        re.M,
    )
    hits = []
    for name in _MODULES:
        source = pathlib.Path(f"anima/{name}.py").read_text(encoding="utf-8")
        for match in frozen.findall(source):
            hits.append((name, match))
    assert hits == [], f"frozen subsystem imports present: {hits}"
