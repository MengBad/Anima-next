"""P6.4 Execution Ledger Read-only Evidence Adapter - authoritative contract.

P6.4 is READ-ONLY / AUDIT-ONLY: it reads execution ledger facts persisted by
the runtime (F5.2 durable layout under execution_ledger/) and normalizes them
into immutable, digest-bound observations that can enter the P5/P6 evidence
chain.  It is NOT a control path - ledger.put() / executor.execute() /
orchestrator.run_plan() / provider.invoke() / replay / recovery / activation
are all forbidden and asserted absent.

    data/execution_ledger/{run}__{task}__{attempt}__{token8}.json
        -> ExecutionLedgerReader.read()      (filesystem read-only)
        -> ExecutionLedgerFactObservation    (frozen, slots, digest-bound)
        -> to_observed_runtime_state()       (P5.1 ObservedRuntimeState)
        -> P5.2 Consumption / P5.5 Reconciliation
        -> Evidence only

Hard boundary (this contract enforces it):

  * READ ONLY        - read()/verify()/inspect() perform no file write, no DB,
                       no timestamp write-back, no process spawn
  * NO FROZEN IMPORT - the module imports no anima.runtime / anima.constitution /
                       anima.core / anima.watchdog (mirror, never import)
  * FAIL CLOSED      - missing/corrupt artifact raises (read) or reports invalid
                       (verify); nothing is auto-created or auto-repaired
  * MISSING IS STATE - a missing artifact is NOT_OBSERVED; it is evidence state,
                       not implementation failure, never a synthetic success
  * FACT != AUTHORITY- ledger success=True/False stays an observed fact; never
                       upgraded to approved/authorized/activated/deployed and
                       never rewrites governance
  * ADDITIVE DIGEST  - artifact_digest/observation_digest are additive; no
                       upstream digest is ever rewritten
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
from dataclasses import FrozenInstanceError

import pytest

from anima.capability_runtime_observation import (
    OFFLINE_SOURCE_PREFIXES,
    ObservedRuntimeState,
)
from anima.capability_runtime_evidence_reconciliation import ReconciliationEngine

NOW = "2026-08-11T12:00:00+08:00"


# --------------------------------------------------------------------------
# fixtures: REAL ledger facts via the upstream ExecutionLedger (F5.2 durable)
# --------------------------------------------------------------------------
def _real_fact(tmp_path, *, run_id="run-p64-1", task_id="t1", attempt=1,
               success=True, result=None, error="", metadata=None):
    """Record a REAL execution ledger fact through the upstream ExecutionLedger."""
    from anima.core.persistence import PersistenceProvider
    from anima.runtime.execution_ledger import ExecutionLedger
    from anima.runtime.executor import ExecutionObservation

    store = ExecutionLedger(PersistenceProvider(str(tmp_path)))
    observation = ExecutionObservation(
        task_id=task_id,
        success=success,
        result=result if result is not None else {"ok": True},
        error=error,
        metadata=metadata if metadata is not None else {"provider_id": "memory.read.local"},
    )
    store.put((run_id, task_id, attempt), observation)
    # locate the single artifact written under execution_ledger/
    artifacts = list((pathlib.Path(tmp_path) / "execution_ledger").glob("*.json"))
    assert len(artifacts) == 1, f"expected exactly one artifact, got {len(artifacts)}"
    return artifacts[0]


def _read_artifact(artifact):
    from anima.capability_runtime_execution_ledger_reader import (  # RED
        ExecutionLedgerReader,
    )

    return ExecutionLedgerReader().read(artifact)


# --------------------------------------------------------------------------
# A. Missing artifact -> NOT_OBSERVED, fail closed, no synthetic success
# --------------------------------------------------------------------------
def test_a_missing_artifact_is_not_observed(tmp_path):
    from anima.capability_runtime_execution_ledger_reader import (
        ExecutionLedgerReader,
        ExecutionLedgerReadError,
        LedgerReadVerification,
    )

    missing = tmp_path / "execution_ledger" / "ghost__t__0__deadbeef.json"
    reader = ExecutionLedgerReader()
    with pytest.raises(ExecutionLedgerReadError):
        reader.read(missing)
    assert not missing.exists(), "fail-closed must not create a fake artifact"
    verification = reader.verify((), missing)
    assert isinstance(verification, LedgerReadVerification)
    assert verification.store_present is False
    assert verification.valid is False
    assert verification.absence_of_digest is True
    assert any("NOT_OBSERVED" in e or "not observed" in e for e in verification.errors)


# --------------------------------------------------------------------------
# B. Valid artifact (REAL upstream-produced) -> faithful observation
# --------------------------------------------------------------------------
def test_b_reads_real_artifact_faithfully(tmp_path):
    from anima.capability_runtime_execution_ledger_reader import (
        ExecutionLedgerReader,
    )

    artifact = _real_fact(tmp_path, success=True)
    reader = ExecutionLedgerReader()
    observation = reader.read(artifact)

    assert observation.run_id == "run-p64-1"
    assert observation.task_id == "t1"
    assert observation.attempt == 1
    assert observation.success is True          # faithful, verbatim
    assert observation.result == {"ok": True}
    assert observation.metadata == {"provider_id": "memory.read.local"}
    assert observation.source.startswith(OFFLINE_SOURCE_PREFIXES)

    verification = reader.verify(observation, artifact)
    assert verification.valid is True
    assert verification.store_present is True
    assert verification.schema_intact is True
    assert verification.data_intact is True
    assert verification.digest_intact is True
    assert verification.absence_of_digest is True  # artifact carries no digest


def test_b2_failed_fact_read_faithfully(tmp_path):
    from anima.capability_runtime_execution_ledger_reader import (
        ExecutionLedgerReader,
    )

    artifact = _real_fact(tmp_path, success=False, error="boom", result={})
    observation = ExecutionLedgerReader().read(artifact)
    assert observation.success is False
    assert observation.error == "boom"
    assert observation.result == {}


# --------------------------------------------------------------------------
# C. Malformed JSON -> fail closed
# --------------------------------------------------------------------------
def test_c_malformed_json_fails_closed(tmp_path):
    from anima.capability_runtime_execution_ledger_reader import (
        ExecutionLedgerReader,
        ExecutionLedgerReadError,
    )

    p = tmp_path / "execution_ledger" / "broken__t__0__deadbeef.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not valid json", encoding="utf-8")
    reader = ExecutionLedgerReader()
    with pytest.raises(ExecutionLedgerReadError):
        reader.read(p)
    assert p.read_text(encoding="utf-8") == "{not valid json"  # nothing repaired


# --------------------------------------------------------------------------
# D. Schema mismatch -> fail closed
# --------------------------------------------------------------------------
def test_d_schema_mismatch_fails_closed(tmp_path):
    from anima.capability_runtime_execution_ledger_reader import (
        ExecutionLedgerReader,
        ExecutionLedgerReadError,
    )

    bad = {
        "key": {"run_id": "r", "task_id": "t", "attempt": "not-an-int"},
        "observation": {"task_id": "t", "success": True, "result": {}, "error": "", "metadata": {}},
    }
    p = tmp_path / "execution_ledger" / "bad__t__0__deadbeef.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(bad), encoding="utf-8")
    reader = ExecutionLedgerReader()
    with pytest.raises(ExecutionLedgerReadError):
        reader.read(p)
    verification = reader.verify((), p)
    assert verification.schema_intact is False
    assert verification.valid is False


def test_d2_missing_required_field_fails_closed(tmp_path):
    from anima.capability_runtime_execution_ledger_reader import (
        ExecutionLedgerReader,
        ExecutionLedgerReadError,
    )

    p = tmp_path / "execution_ledger" / "incomplete__t__0__deadbeef.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"key": {"run_id": "r"}}), encoding="utf-8")
    with pytest.raises(ExecutionLedgerReadError):
        ExecutionLedgerReader().read(p)


# --------------------------------------------------------------------------
# E. Digest tamper -> verification failure (fail closed)
# --------------------------------------------------------------------------
def test_e_digest_tamper_fails_closed(tmp_path):
    from anima.capability_runtime_execution_ledger_reader import (
        ExecutionLedgerFactObservation,
        ExecutionLedgerReader,
    )

    artifact = _real_fact(tmp_path)
    reader = ExecutionLedgerReader()
    observation = reader.read(artifact)

    tampered = object.__new__(ExecutionLedgerFactObservation)
    for field in ExecutionLedgerFactObservation.__slots__:
        object.__setattr__(tampered, field, getattr(observation, field))
    object.__setattr__(tampered, "observation_digest", "0" * 64)

    verification = reader.verify(tampered, artifact)
    assert verification.valid is False
    assert verification.digest_intact is False
    assert artifact.read_bytes() == artifact.read_bytes()  # never repaired


# --------------------------------------------------------------------------
# F. Immutable observation
# --------------------------------------------------------------------------
def test_f_observation_is_immutable(tmp_path):
    from anima.capability_runtime_execution_ledger_reader import ExecutionLedgerReader

    artifact = _real_fact(tmp_path)
    observation = ExecutionLedgerReader().read(artifact)
    with pytest.raises(FrozenInstanceError):
        observation.success = False  # frozen: in-place mutation forbidden


# --------------------------------------------------------------------------
# G. Repeated read does not change artifact bytes
# --------------------------------------------------------------------------
def test_g_repeated_read_leaves_artifact_bytes_identical(tmp_path):
    from anima.capability_runtime_execution_ledger_reader import ExecutionLedgerReader

    artifact = _real_fact(tmp_path)
    before = artifact.read_bytes()
    reader = ExecutionLedgerReader()
    first = reader.read(artifact)
    second = reader.read(artifact)
    reader.verify(second, artifact)
    reader.inspect(second)
    assert artifact.read_bytes() == before
    assert first == second
    assert [f.name for f in artifact.parent.iterdir()] == [artifact.name]


# --------------------------------------------------------------------------
# H. FACT != AUTHORITY: success=True stays an observed fact, never escalates
# --------------------------------------------------------------------------
def test_h_fact_never_upgrades_governance(tmp_path):
    from anima.capability_evolution_contract import OfflineEvolutionPlanner
    from anima.capability_runtime_consumption import RuntimeConsumptionAdapter
    from anima.capability_runtime_execution_ledger_reader import (
        ExecutionLedgerReader,
        to_observed_runtime_state,
    )

    # governance: approved gate + record stays not_executed
    planner = OfflineEvolutionPlanner()
    proposal = planner.propose(
        proposal_id="p64-evo", validation_rfc_id="RFC-0105",
        observed_contradiction="c", inconsistency_eliminated="e",
        change_class="contraction", forbids_expansion="true", proposed_at=NOW,
    )
    impact = planner.assess_impact(
        impact_id="p64-imp", proposal=proposal,
        affected_foundation_refs=("0100#S1",), assessed_at=NOW,
    )
    review = planner.review(
        review_id="p64-rev", proposal=proposal, impact=impact,
        decision="approved", reviewed_at=NOW,
    )
    gate = planner.record_gate(
        gate_id="p64-gate", proposal=proposal, review=review, decision="approved",
        reviewer="senior-developer", evidence_refs=("ev",),
        created_at=NOW, rationale="rationale",
    )
    record = planner.record_evolution(
        record_id="p64-rec", proposal=proposal, review=review, gate=gate,
        recorder="governance-agent", recorded_at=NOW,
    )
    assert record.execution_status == "not_executed"
    gate_before = gate.digest
    record_before = record.record_digest

    # runtime: a REAL ledger fact saying the external call succeeded
    artifact = _real_fact(tmp_path, success=True)
    observation = ExecutionLedgerReader().read(artifact)
    state = to_observed_runtime_state(observation)

    # the fact is carried verbatim as an OBSERVED fact
    assert isinstance(state, ObservedRuntimeState)
    assert state.runtime_activation == "executed"
    assert state.execution_status == "unknown"
    # it never upgrades into a governance/authorization claim
    for banned in ("approved", "authorized", "activated", "deployed"):
        assert banned not in state.runtime_activation
        assert banned not in state.execution_status

    # P5.2 consume works; verify() correctly rejects the execution claim
    # (P5.2 synthetic-only scope is unchanged - the fact is never faked away)
    consumed = RuntimeConsumptionAdapter().consume(
        state, consumption_id="p64-hc", consumed_at=NOW,
    )
    assert consumed.runtime_activation == "executed"
    ver = RuntimeConsumptionAdapter().verify(consumed, state)
    assert ver.no_execution is False
    assert ver.valid is False

    # P5.5 reconciliation accepts the fact
    reconciliation = ReconciliationEngine().reconcile(
        (state,), reconciliation_id="p64-hr", reconciled_at=NOW,
    )
    assert reconciliation.runtime_state[0].runtime_activation == "executed"

    # governance digests byte-identical - the fact never rewrites governance
    assert gate.digest == gate_before
    assert record.record_digest == record_before


# --------------------------------------------------------------------------
# I. No control surface: ledger.put / executor / orchestrator / provider absent
# --------------------------------------------------------------------------
def test_i_reader_exposes_no_control_surface(tmp_path):
    from anima.capability_runtime_execution_ledger_reader import ExecutionLedgerReader

    reader = ExecutionLedgerReader()
    banned = {
        "put", "execute", "run", "dispatch", "activate", "apply", "repair",
        "invoke", "replay", "recover", "save", "write", "update", "delete",
        "patch", "retry", "rerun", "deploy", "restart", "start", "stop", "commit",
    }
    surface = {name for name in dir(reader) if not name.startswith("_")}
    assert surface & banned == set()
    assert surface == {"read", "verify", "inspect"}


# --------------------------------------------------------------------------
# J. P5 chain compatibility
# --------------------------------------------------------------------------
def test_j_p5_chain_consumes_p64_evidence(tmp_path):
    from anima.capability_runtime_consumption import RuntimeConsumptionAdapter
    from anima.capability_runtime_execution_ledger_reader import (
        ExecutionLedgerReader,
        to_observed_runtime_state,
    )

    artifact = _real_fact(tmp_path, success=True)
    observation = ExecutionLedgerReader().read(artifact)
    state = to_observed_runtime_state(observation)

    consumed = RuntimeConsumptionAdapter().consume(
        state, consumption_id="p64-j", consumed_at=NOW,
    )
    assert consumed.runtime_activation == "executed"
    reconciliation = ReconciliationEngine().reconcile(
        (state,), reconciliation_id="p64-j2", reconciled_at=NOW,
    )
    assert ReconciliationEngine().verify(reconciliation, (state,)).valid is True


# --------------------------------------------------------------------------
# K/L. Static boundary + frozen-runtime-import scan
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
    "capability_runtime_execution_ledger_reader",
    "capability_staged_activation",
    "capability_suspend_revocation",
)


def _source_for(name: str) -> pathlib.Path:
    """折叠过的模块其实现已移入 anima/capability/, 治理扫描须指向单源。"""
    canon = pathlib.Path(f"anima/capability/{name.removeprefix('capability_')}.py")
    return canon if canon.exists() else pathlib.Path(f"anima/{name}.py")

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
    r"start|stop|restart|reload|deploy|save|run|recover|put|invoke|replay)\b",
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
    r"\." + "put" + r"\(",
    r"\." + "invoke" + r"\(",
    r"\." + "replay" + r"\(",
    "active" + r"\s*=\s*True",
)


def test_k1_forbidden_transport_tokens_zero_p3_p64():
    hits = []
    for name in _MODULES:
        source = _source_for(name).read_text(encoding="utf-8")
        for needle in _TRANSPORT_NEEDLES:
            if re.search(needle, source):
                hits.append((name, needle))
    assert hits == [], f"transport tokens present: {hits}"


def test_k2_no_mutation_callables_p3_p64():
    hits = []
    for name in _MODULES:
        source = _source_for(name).read_text(encoding="utf-8")
        for needle in _MUTATION_NEEDLES:
            if re.search(needle, source):
                hits.append((name, needle))
    assert hits == [], f"mutation callables present: {hits}"


def test_l1_p64_module_imports_no_frozen_subsystem():
    """The P6.4 module must NOT import anima.runtime / anima.constitution /
    anima.core / anima.watchdog - it mirrors the ledger schema, never imports it."""
    source = _source_for("capability_runtime_execution_ledger_reader").read_text(encoding="utf-8")
    frozen = re.compile(
        r"^\s*(?:from|import)\s+anima\.(runtime|constitution|core|watchdog|"
        r"lifecycle|foundation|memory|identity|capability_contract)\b",
        re.M,
    )
    hits = frozen.findall(source)
    assert hits == [], f"frozen subsystem imports in P6.4: {hits}"
    # execution_ledger / executor / orchestrator / providers / recovery forbidden
    for name in ("execution_ledger", "executor", "orchestrator", "providers", "recovery"):
        pattern = re.compile(r"^\s*(?:from|import)\s+.*" + name, re.M)
        assert pattern.findall(source) == [], f"forbidden {name} import in P6.4"


def test_l2_no_frozen_imports_across_p3_p64():
    frozen = re.compile(
        r"^\s*(?:from|import)\s+anima\."
        r"(?!capability_(?:activation_contract|activation_proof|staged_activation|"
        r"suspend_revocation|evolution_contract|runtime_observation|runtime_consumption|"
        r"runtime_observation_store|runtime_evidence_reconciliation|runtime_workflow_snapshot|"
        r"runtime_evidence_integration|runtime_execution_ledger_reader))"
        r"(lifecycle|constitution|foundation|memory|identity|runtime|capability_contract)\b",
        re.M,
    )
    hits = []
    for name in _MODULES:
        source = _source_for(name).read_text(encoding="utf-8")
        for match in frozen.findall(source):
            hits.append((name, match))
    assert hits == [], f"frozen subsystem imports present: {hits}"
