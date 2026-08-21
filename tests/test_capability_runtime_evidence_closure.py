"""P5.6 Runtime Evidence Chain Final Closure Audit - authoritative contract.

P5.6 is an **audit-only** slice.  It adds no runtime capability, no adapter,
no activation/execution/dispatch, no governance re-decision.  It proves that
the already-existing P5.1 -> P5.2 -> P5.4 -> P5.5 read-only runtime evidence
chain is closed:

    P3/P4 Governance Evidence
        -> P5.1 Observation           (read-only)
        -> P5.2 Consumption           (read-only)
        -> P5.4 Real Store READ       (read-only)
        -> P5.5 Reconciliation        (read-only, report-only drift)
        -> P5.6 Final Closure Audit   (this file, verification only)

The ten closure questions answered here (A-J):

    A  digest continuity across every hop (tamper fails closed)
    B  tamper detection (observation / store / reconciliation / upstream)
    C  governance immutability (digest equality before/after)
    D  runtime fact non-escalation (running=true stays an observation fact)
    E  reconcile report-only (store byte-identical before/after)
    F  drift classification (NO_DRIFT / EXPECTED_DRIFT / UNEXPECTED_DRIFT /
       INTEGRITY_FAILURE / SCHEMA_FAILURE)
    G  store integrity fail-closed
    H  public surface audit (read-only APIs only)
    I  static boundary (no executable transport / mutation path)
    J  final closure invariant (read-only half CLOSED)

Audit PASS does NOT equal runtime authorization.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re

import pytest

from anima.capability_evolution_contract import OfflineEvolutionPlanner
from anima.capability_runtime_observation import (
    NOT_ACTIVATED,
    NOT_APPLICABLE,
    NOT_EXECUTED,
    OBSERVATION_SCOPE,
    ObservedRuntimeState,
    ReadOnlyRuntimeObservationAdapter,
    RuntimeObservation,
    verify_runtime_observation,
)
from anima.capability_runtime_consumption import (
    ConsumedRuntimeState,
    RuntimeConsumptionAdapter,
)
from anima.capability_runtime_observation_store import (
    RealObservationStoreAdapter,
    StoreReadVerification,
)
from anima.capability_runtime_evidence_reconciliation import (
    EXPECTED_DRIFT,
    INTEGRITY_FAILURE,
    NO_DRIFT,
    SCHEMA_FAILURE,
    UNEXPECTED_DRIFT,
    ReconciliationEngine,
    RuntimeEvidenceReconciliation,
)

NOW = "2026-08-11T11:00:00+08:00"
REAL_SAMPLE_TS = 1784200702.721


# --------------------------------------------------------------------------
# real builders only (no hand-rolled evidence)
# --------------------------------------------------------------------------
def _p4_chain(decision: str = "approved"):
    planner = OfflineEvolutionPlanner()
    proposal = planner.propose(
        proposal_id="p56-evo",
        validation_rfc_id="RFC-0105",
        observed_contradiction="observed contradiction",
        inconsistency_eliminated="removes inconsistency",
        change_class="contraction",
        forbids_expansion="true",
        proposed_at=NOW,
    )
    impact = planner.assess_impact(
        impact_id="p56-imp", proposal=proposal,
        affected_foundation_refs=("0100#S1",), assessed_at=NOW,
    )
    review = planner.review(
        review_id="p56-rev", proposal=proposal, impact=impact,
        decision=decision, reviewed_at=NOW,
    )
    gate = planner.record_gate(
        gate_id="p56-gate", proposal=proposal, review=review, decision=decision,
        reviewer="senior-developer", evidence_refs=("ev",),
        created_at=NOW, rationale="rationale",
    )
    record = planner.record_evolution(
        record_id="p56-rec", proposal=proposal, review=review, gate=gate,
        recorder="governance-agent", recorded_at=NOW,
    )
    return proposal, impact, review, gate, record


def _write_store(store_path, samples, *, version: int = 1) -> pathlib.Path:
    p = pathlib.Path(store_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"version": version, "samples": samples}, ensure_ascii=False),
        encoding="utf-8",
    )
    return p


def _sample(ts: float = REAL_SAMPLE_TS, running: bool = True) -> dict:
    return {
        "ts": ts,
        "running": running,
        "channel_connected": False,
        "heartbeat_running": False,
        "watchdog_running": False,
        "watchdog_lag_sec": 0.0,
        "watchdog_max_lag_sec": 0.0,
        "watchdog_late_ticks": 0,
        "llm_requests": 0,
        "llm_failures": 0,
        "sylann_observations": 0,
        "sylann_ticks": 0,
        "sylann_errors": 0,
        "sylann_responses": 0,
        "sylann_expression_hints": 0,
        "agenda_pending": 0,
        "approvals_pending": 0,
    }


def _read_real(p: pathlib.Path):
    adapter = RealObservationStoreAdapter()
    observations = adapter.read(p)
    verification = adapter.verify(observations, p)
    return observations, verification


def _digest(payload) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _craft_tampered(base: RuntimeEvidenceReconciliation, **overrides):
    """Bypass __post_init__ guards to build a tampered reconciliation."""
    inst = object.__new__(RuntimeEvidenceReconciliation)
    for field in RuntimeEvidenceReconciliation.__slots__:
        object.__setattr__(inst, field, getattr(base, field))
    for key, value in overrides.items():
        object.__setattr__(inst, key, value)
    return inst


# --------------------------------------------------------------------------
# A. Digest continuity across every hop
# --------------------------------------------------------------------------
def test_a_digest_continuity_full_chain(tmp_path):
    """Proposal -> Gate -> Record -> Observation -> Consumption -> Store -> Reconciliation."""
    proposal, _impact, _review, gate, record = _p4_chain()

    # governance hops: each artifact carries the upstream digest
    assert record.gate_digest == gate.digest
    assert gate.proposal_digest == proposal.proposal_digest

    # P5.1 observe: observation digest binds to the record
    observation = ReadOnlyRuntimeObservationAdapter().observe(
        record,
        observation_id="p56-obs",
        observed_at=NOW,
        proposal=proposal,
    )
    assert isinstance(observation, RuntimeObservation)
    assert observation.evidence_digest == record.record_digest
    ver = verify_runtime_observation(observation, record, None)
    assert ver.valid is True
    assert ver.no_execution is True  # approved is never executed

    # P5.2 consume: consumption digest binds to the observed state
    observed_state = ObservedRuntimeState(
        subject_id="runtime/health",
        runtime_activation=NOT_ACTIVATED,
        execution_status=NOT_EXECUTED,
        observed_at=NOW,
        source="synthetic://offline/closure",
    )
    consumed = RuntimeConsumptionAdapter().consume(
        observed_state, consumption_id="p56-cons", consumed_at=NOW
    )
    assert isinstance(consumed, ConsumedRuntimeState)
    assert RuntimeConsumptionAdapter().verify(consumed, observed_state).valid is True

    # P5.4 store read + verification
    p = _write_store(tmp_path / "health_history.json", [_sample()])
    store_observations, store_verification = _read_real(p)
    assert store_verification.valid is True
    assert store_verification.absence_of_digest is True  # honest audit fact

    # P5.5 reconcile binds to the store verification digest
    reconciliation = ReconciliationEngine().reconcile(
        store_observations,
        reconciliation_id="p56-rec",
        reconciled_at=NOW,
        store_verification=store_verification,
    )
    assert isinstance(reconciliation, RuntimeEvidenceReconciliation)
    assert reconciliation.store_digest == store_verification.locally_computed_digest
    assert reconciliation.schema_valid is True
    assert reconciliation.integrity_valid is True
    assert ReconciliationEngine().verify(
        reconciliation, store_observations
    ).valid is True


def test_a2_upstream_governance_digest_never_modified(tmp_path):
    """Observing/consuming/reconciling never rewrites an upstream digest."""
    proposal, _impact, _review, gate, record = _p4_chain()
    proposal_digest_before = proposal.proposal_digest
    gate_digest_before = gate.digest
    record_digest_before = record.record_digest

    p = _write_store(tmp_path / "health_history.json", [_sample()])
    observations, verification = _read_real(p)
    ReconciliationEngine().reconcile(
        observations, reconciliation_id="p56-a2", reconciled_at=NOW,
        store_verification=verification,
    )

    assert proposal.proposal_digest == proposal_digest_before
    assert gate.digest == gate_digest_before
    assert record.record_digest == record_digest_before


# --------------------------------------------------------------------------
# B. Tamper detection (every hop fails closed)
# --------------------------------------------------------------------------
def test_b1_observation_digest_tamper_fails_closed(tmp_path):
    proposal, _impact, _review, _gate, record = _p4_chain()
    observation = ReadOnlyRuntimeObservationAdapter().observe(
        record, observation_id="p56-b1", observed_at=NOW, proposal=proposal,
    )
    tampered = object.__new__(type(observation))
    for field in type(observation).__slots__:
        object.__setattr__(tampered, field, getattr(observation, field))
    object.__setattr__(tampered, "observation_digest", "0" * 64)
    ver = verify_runtime_observation(tampered, record, None)
    assert ver.valid is False
    assert ver.governance_binding_intact is False


def test_b2_store_digest_change_fails_closed(tmp_path):
    """When the store bytes change between reads, P5.4 reports divergence."""
    p = _write_store(tmp_path / "health_history.json", [_sample(ts=1.0)])
    observations1, _ = _read_real(p)
    _write_store(p, [_sample(ts=1.0), _sample(ts=2.0)])  # runtime appended
    observations2, verification2 = _read_real(p)
    # the store itself carries no digest (absence_of_digest); the divergence is
    # observable through P5.4 data_intact against a stale observation set
    stale_verification = RealObservationStoreAdapter().verify(observations1, p)
    assert stale_verification.data_intact is False  # observations diverge
    assert len(observations2) == 2  # new read reflects the append


def test_b3_reconciliation_evidence_digest_tamper_fails_closed(tmp_path):
    p = _write_store(tmp_path / "health_history.json", [_sample()])
    observations, verification = _read_real(p)
    engine = ReconciliationEngine()
    reconciliation = engine.reconcile(
        observations, reconciliation_id="p56-b3", reconciled_at=NOW,
        store_verification=verification,
    )
    tampered = _craft_tampered(reconciliation, evidence_digest="f" * 64)
    result = engine.verify(tampered, observations)
    assert result.valid is False
    assert result.evidence_digest_intact is False


def test_b4_upstream_governance_digest_tamper_fails_closed(tmp_path):
    """A lying record (tampered record_digest) is rejected at verification.

    P5.1's observe() is faithful: it normalizes what the evidence *says*
    without sanitizing.  The tamper is detected by
    verify_runtime_observation(), which recomputes the digest and fails
    closed.
    """
    proposal, _impact, review, gate, _record = _p4_chain()
    planner = OfflineEvolutionPlanner()
    forged = planner.record_evolution(
        record_id="p56-forged", proposal=proposal,
        review=review, gate=gate,
        recorder="governance-agent", recorded_at=NOW,
    )
    # simulate an upstream tamper by swapping the carried digest on the record
    tampered_record = object.__new__(type(forged))
    for field in type(forged).__slots__:
        object.__setattr__(tampered_record, field, getattr(forged, field))
    object.__setattr__(tampered_record, "record_digest", "0" * 64)

    observation = ReadOnlyRuntimeObservationAdapter().observe(
        tampered_record, observation_id="p56-b4", observed_at=NOW,
        proposal=proposal,
    )
    # faithful observation carries the tampered digest...
    assert observation.evidence_digest == "0" * 64
    # ...and verification fails closed on the mismatch
    ver = verify_runtime_observation(observation, tampered_record, None)
    assert ver.valid is False
    assert ver.evidence_intact is False


# --------------------------------------------------------------------------
# C. Governance immutability (digest equality, not just object equality)
# --------------------------------------------------------------------------
def test_c_governance_digests_unchanged_after_runtime_evidence(tmp_path):
    proposal, _impact, review, gate, record = _p4_chain()
    before = {
        "proposal": proposal.proposal_digest,
        "review": review.review_digest,
        "gate": gate.digest,
        "record": record.record_digest,
    }

    # inject real runtime evidence: store read + reconcile with running=true
    p = _write_store(tmp_path / "health_history.json", [_sample(running=True)])
    observations, verification = _read_real(p)
    ReconciliationEngine().reconcile(
        observations, reconciliation_id="p56-c", reconciled_at=NOW,
        store_verification=verification,
    )

    after = {
        "proposal": proposal.proposal_digest,
        "review": review.review_digest,
        "gate": gate.digest,
        "record": record.record_digest,
    }
    assert before == after


# --------------------------------------------------------------------------
# D. Runtime fact non-escalation
# --------------------------------------------------------------------------
def test_d_running_fact_never_escalated(tmp_path):
    p = _write_store(tmp_path / "health_history.json", [_sample(running=True)])
    observations, verification = _read_real(p)
    reconciliation = ReconciliationEngine().reconcile(
        observations, reconciliation_id="p56-d", reconciled_at=NOW,
        store_verification=verification,
    )
    assert reconciliation.runtime_state[0].runtime_activation == "running"
    assert reconciliation.runtime_state[0].execution_status == "unknown"
    # never upgraded
    for banned in ("approved", "authorized", "activated", "executed", "deployed"):
        assert banned not in reconciliation.reconciliation_status.lower()
        assert banned not in reconciliation.runtime_state[0].execution_status.lower()
    assert reconciliation.reconciliation_status == NO_DRIFT


# --------------------------------------------------------------------------
# E. Reconcile report-only (byte-level comparison)
# --------------------------------------------------------------------------
def test_e_reconcile_is_report_only(tmp_path):
    p = _write_store(tmp_path / "health_history.json", [_sample(), _sample(ts=3.0)])
    observations, verification = _read_real(p)
    before = p.read_bytes()
    engine = ReconciliationEngine()
    reconciliation = engine.reconcile(
        observations, reconciliation_id="p56-e", reconciled_at=NOW,
        store_verification=verification,
    )
    engine.verify(reconciliation, observations)
    engine.inspect(reconciliation)
    assert p.read_bytes() == before
    assert [f.name for f in p.parent.iterdir()] == [p.name]


# --------------------------------------------------------------------------
# F. Drift classification (report-only, all five states)
# --------------------------------------------------------------------------
def test_f1_no_drift_identical():
    state = ObservedRuntimeState(
        subject_id="runtime/health", runtime_activation="running",
        execution_status="unknown", observed_at=NOW,
        source="local://test.json",
    )
    engine = ReconciliationEngine()
    reconciliation = engine.reconcile(
        (state,), reconciliation_id="p56-f1", reconciled_at=NOW, previous=(state,),
    )
    assert reconciliation.reconciliation_status == NO_DRIFT
    assert reconciliation.drift_detected is False


def test_f2_expected_drift_timestamp_only():
    import datetime as _dt
    ts1 = 1.0
    ts2 = 2.0
    s1 = ObservedRuntimeState(
        subject_id="runtime/health", runtime_activation="running",
        execution_status="unknown",
        observed_at=_dt.datetime.fromtimestamp(ts1, tz=_dt.timezone.utc).isoformat(),
        source="local://test.json",
    )
    s2 = ObservedRuntimeState(
        subject_id="runtime/health", runtime_activation="running",
        execution_status="unknown",
        observed_at=_dt.datetime.fromtimestamp(ts2, tz=_dt.timezone.utc).isoformat(),
        source="local://test.json",
    )
    reconciliation = ReconciliationEngine().reconcile(
        (s2,), reconciliation_id="p56-f2", reconciled_at=NOW, previous=(s1,),
    )
    assert reconciliation.reconciliation_status == EXPECTED_DRIFT
    assert reconciliation.drift_detected is True
    assert reconciliation.drift_fields == ("observed_at",)


def test_f3_unexpected_drift_activation_change():
    import datetime as _dt
    s1 = ObservedRuntimeState(
        subject_id="runtime/health", runtime_activation="running",
        execution_status="unknown",
        observed_at=_dt.datetime.fromtimestamp(1.0, tz=_dt.timezone.utc).isoformat(),
        source="local://test.json",
    )
    s2 = ObservedRuntimeState(
        subject_id="runtime/health", runtime_activation="stopped",
        execution_status="unknown",
        observed_at=_dt.datetime.fromtimestamp(2.0, tz=_dt.timezone.utc).isoformat(),
        source="local://test.json",
    )
    reconciliation = ReconciliationEngine().reconcile(
        (s2,), reconciliation_id="p56-f3", reconciled_at=NOW, previous=(s1,),
    )
    assert reconciliation.reconciliation_status == UNEXPECTED_DRIFT
    assert reconciliation.drift_detected is True
    assert "runtime_activation" in reconciliation.drift_fields


def test_f4_integrity_failure_fails_closed(tmp_path):
    p = _write_store(tmp_path / "health_history.json", [_sample()])
    observations, verification = _read_real(p)
    engine = ReconciliationEngine()
    reconciliation = engine.reconcile(
        observations, reconciliation_id="p56-f4", reconciled_at=NOW,
        store_verification=verification,
    )
    tampered = _craft_tampered(reconciliation, observation_digest="0" * 64)
    assert engine.verify(tampered, observations).valid is False


def test_f5_schema_failure_fails_closed(tmp_path):
    p = _write_store(tmp_path / "health_history.json", [_sample()], version=999)
    observations, verification = _read_real(p)
    reconciliation = ReconciliationEngine().reconcile(
        observations, reconciliation_id="p56-f5", reconciled_at=NOW,
        store_verification=verification,
    )
    assert reconciliation.schema_valid is False
    assert reconciliation.reconciliation_status == SCHEMA_FAILURE
    assert reconciliation.integrity_valid is False
    assert "999" in p.read_text(encoding="utf-8")  # nothing repaired


# --------------------------------------------------------------------------
# G. Store integrity fail-closed
# --------------------------------------------------------------------------
def test_g_missing_store_fails_closed(tmp_path):
    missing = tmp_path / "does_not_exist.json"
    with pytest.raises(ValueError):
        RealObservationStoreAdapter().read(missing)
    assert not missing.exists()


def test_g2_corrupt_store_fails_closed(tmp_path):
    p = tmp_path / "health_history.json"
    p.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ValueError):
        RealObservationStoreAdapter().read(p)
    # no auto-repair
    assert p.read_text(encoding="utf-8") == "{not valid json"


# --------------------------------------------------------------------------
# H. Public surface audit (introspection)
# --------------------------------------------------------------------------
def test_h_public_surfaces_are_read_only():
    p51 = {n for n in dir(ReadOnlyRuntimeObservationAdapter()) if not n.startswith("_")}
    p52 = {n for n in dir(RuntimeConsumptionAdapter()) if not n.startswith("_")}
    p54 = {n for n in dir(RealObservationStoreAdapter()) if not n.startswith("_")}
    p55 = {n for n in dir(ReconciliationEngine()) if not n.startswith("_")}

    banned = {
        "apply", "execute", "activate", "mutate", "deploy", "repair", "dispatch",
        "write", "update", "delete", "patch", "commit", "start", "stop",
        "restart", "reload", "enable", "disable",
    }
    for surface in (p51, p52, p54, p55):
        assert surface & banned == set(), f"runtime action surface: {surface & banned}"

    assert p51 == {"observe", "read_state", "collect_observation", "snapshot", "inspect"}
    assert p52 == {"consume", "inspect", "verify", "reconcile"}
    assert p54 == {"read", "inspect", "verify"}
    assert p55 == {"reconcile", "verify", "inspect"}


# --------------------------------------------------------------------------
# I. Static boundary (no executable transport / mutation path)
# --------------------------------------------------------------------------
_MODULES = (
    "capability_activation_contract",
    "capability_activation_proof",
    "capability_evolution_contract",
    "capability_runtime_observation",
    "capability_runtime_consumption",
    "capability_runtime_observation_store",
    "capability_runtime_evidence_reconciliation",
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
    r"def (apply|execute|activate|mutate|repair|dispatch|write|delete|update|patch|start|stop|restart|reload|deploy)\b",
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


def test_i1_forbidden_transport_tokens_zero_p3_p56():
    hits = []
    for name in _MODULES:
        source = pathlib.Path(f"anima/{name}.py").read_text(encoding="utf-8")
        for needle in _TRANSPORT_NEEDLES:
            if re.search(needle, source):
                hits.append((name, needle))
    assert hits == [], f"transport tokens present: {hits}"


def test_i2_no_mutation_callables_p3_p56():
    hits = []
    for name in _MODULES:
        source = pathlib.Path(f"anima/{name}.py").read_text(encoding="utf-8")
        for needle in _MUTATION_NEEDLES:
            if re.search(needle, source):
                hits.append((name, needle))
    assert hits == [], f"mutation callables present: {hits}"


def test_i3_no_frozen_subsystem_imports_p3_p56():
    frozen = re.compile(
        r"^\s*(?:from|import)\s+anima\."
        r"(?!capability_(?:activation_contract|activation_proof|staged_activation|"
        r"suspend_revocation|evolution_contract|runtime_observation|runtime_consumption|"
        r"runtime_observation_store|runtime_evidence_reconciliation))"
        r"(lifecycle|constitution|foundation|memory|identity|runtime|capability_contract)\b",
        re.M,
    )
    hits = []
    for name in _MODULES:
        source = pathlib.Path(f"anima/{name}.py").read_text(encoding="utf-8")
        for match in frozen.findall(source):
            hits.append((name, match))
    assert hits == [], f"frozen subsystem imports present: {hits}"


# --------------------------------------------------------------------------
# J. Final closure invariant
# --------------------------------------------------------------------------
def test_j_final_closure_invariant(tmp_path):
    """The full chain is immutable, digest-bound, non-escalating, isolated.

    approved (governance) + running (runtime observation) can coexist while
    execution_status stays not_executed - and neither direction mutates the
    other.
    """
    proposal, _impact, _review, gate, record = _p4_chain(decision="approved")

    # governance decision = approved (stays approved)
    assert gate.decision == "approved"
    assert record.execution_status == NOT_EXECUTED  # governance never executes

    # runtime observation = running (stays an observation fact)
    p = _write_store(tmp_path / "health_history.json", [_sample(running=True)])
    observations, verification = _read_real(p)
    reconciliation = ReconciliationEngine().reconcile(
        observations, reconciliation_id="p56-j", reconciled_at=NOW,
        store_verification=verification,
    )
    assert reconciliation.runtime_state[0].runtime_activation == "running"

    # the three states coexist without cross-mutation
    assert gate.decision == "approved"
    assert record.execution_status == NOT_EXECUTED
    assert reconciliation.runtime_state[0].runtime_activation == "running"

    # digests unchanged end to end
    assert gate.digest == _digest(
        {
            "gate_id": gate.gate_id,
            "proposal_id": gate.proposal_id,
            "proposal_digest": gate.proposal_digest,
            "review_id": gate.review_id,
            "decision": gate.decision,
            "reviewer": gate.reviewer,
            "evidence_refs": list(gate.evidence_refs),
            "created_at": gate.created_at,
            "rationale": gate.rationale,
        }
    )
    assert ReconciliationEngine().verify(reconciliation, observations).valid is True
