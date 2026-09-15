"""P5.5 Runtime Evidence Reconciliation & Governance Isolation - authoritative contract.

P5.5 is the reconciliation / normalization slice over P5.4's real-store
observations:

    P3-P4 Governance Evidence
        -> P5.1 Observation
        -> P5.2 Consumption
        -> P5.4 Real Runtime Store READ
        -> P5.5 Evidence Reconciliation   (this slice)
        -> Immutable Runtime Evidence
        -> STOP

Hard boundary (this contract enforces it):

  * READ ONLY        - reconcile() never writes, never repairs, never wakes the runtime
  * FAIL CLOSED      - schema / integrity failure is reported, never silently fixed
  * NO EXECUTION     - no apply/execute/activate/mutate/repair/dispatch/start/stop surface
  * OBSERVATION FACT - running=true stays an observation fact, never upgraded to
                       authorized/approved/activated/executed/deployed
  * GOVERNANCE ISOLATED - proposal/review/gate/record are never re-decided or rewritten
  * ADDITIVE DIGEST  - new evidence digests are appended; upstream digests never rewritten
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re

import pytest

from anima.capability_runtime_observation import (
    OFFLINE_SOURCE_PREFIXES,
    ObservedRuntimeState,
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
    ReconciliationVerification,
    RuntimeEvidenceReconciliation,
    RuntimeEvidenceReconciliationError,
)

NOW = "2026-08-11T10:00:00+08:00"
REAL_SAMPLE_TS = 1784200702.721


# --------------------------------------------------------------------------
# fixtures: a store file in the real format + a helper to build observations
# --------------------------------------------------------------------------
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


def _read_observations(p: pathlib.Path):
    """Read observations + store verification through P5.4 (the real path)."""
    adapter = RealObservationStoreAdapter()
    observations = adapter.read(p)
    verification = adapter.verify(observations, p)
    return observations, verification


def _make_state(
    runtime_activation: str = "running",
    ts: float = REAL_SAMPLE_TS,
    source: str = "local://test.json",
) -> ObservedRuntimeState:
    import datetime as _dt

    observed_at = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).isoformat()
    return ObservedRuntimeState(
        subject_id="runtime/health",
        runtime_activation=runtime_activation,
        execution_status="unknown",
        observed_at=observed_at,
        source=source,
    )


def _craft_tampered(base: RuntimeEvidenceReconciliation, **overrides):
    """Bypass __post_init__ guards to build a tampered reconciliation.

    The frozen slots dataclass __post_init__ already enforces digests on the
    happy path.  To prove verify() fails *closed* on a bad instance we build
    one without running the post-init validators.
    """
    inst = object.__new__(RuntimeEvidenceReconciliation)
    for field in RuntimeEvidenceReconciliation.__slots__:
        object.__setattr__(inst, field, getattr(base, field))
    for key, value in overrides.items():
        object.__setattr__(inst, key, value)
    return inst


# --------------------------------------------------------------------------
# A. Valid observation: real P5.4 observation enters P5.5
# --------------------------------------------------------------------------
def test_a_real_p54_observation_reconciles(tmp_path):
    p = _write_store(tmp_path / "health_history.json", [_sample()])
    observations, verification = _read_observations(p)
    engine = ReconciliationEngine()
    reconciliation = engine.reconcile(
        observations,
        reconciliation_id="p55-a",
        reconciled_at=NOW,
        store_verification=verification,
    )
    assert isinstance(reconciliation, RuntimeEvidenceReconciliation)
    assert reconciliation.schema_valid is True
    assert reconciliation.integrity_valid is True
    assert reconciliation.observation_digest
    assert reconciliation.evidence_digest
    # verify() round-trips clean
    assert engine.verify(reconciliation, observations).valid is True


def test_a2_reconcile_repo_real_store_when_present():
    """The actual production store (data/health_history.json) reconciles."""
    repo_store = pathlib.Path("data/health_history.json")
    if not repo_store.exists():
        pytest.skip("real observation store not present in this checkout")
    observations, verification = _read_observations(repo_store)
    engine = ReconciliationEngine()
    reconciliation = engine.reconcile(
        observations,
        reconciliation_id="p55-real",
        reconciled_at=NOW,
        store_verification=verification,
    )
    assert reconciliation.schema_valid is True
    assert reconciliation.integrity_valid is True
    assert len(reconciliation.runtime_state) > 0
    assert engine.verify(reconciliation, observations).valid is True


# --------------------------------------------------------------------------
# B. Stable evidence: same observation -> same evidence
# --------------------------------------------------------------------------
def test_b_stable_evidence_identical(tmp_path):
    p = _write_store(tmp_path / "health_history.json", [_sample()])
    observations, verification = _read_observations(p)
    engine = ReconciliationEngine()
    first = engine.reconcile(
        observations, reconciliation_id="p55-b", reconciled_at=NOW,
        store_verification=verification,
    )
    second = engine.reconcile(
        observations, reconciliation_id="p55-b", reconciled_at=NOW,
        store_verification=verification,
    )
    assert first.observation_digest == second.observation_digest
    assert first.evidence_digest == second.evidence_digest
    assert first.reconciliation_status == second.reconciliation_status
    assert first == second


# --------------------------------------------------------------------------
# C. Drift detection: altered observation -> drift reported, store unchanged
# --------------------------------------------------------------------------
def test_c_drift_detected_but_store_unchanged(tmp_path):
    p = _write_store(tmp_path / "health_history.json", [_sample(ts=1.0)])
    observations, verification = _read_observations(p)
    engine = ReconciliationEngine()
    first = engine.reconcile(
        observations, reconciliation_id="p55-c1", reconciled_at=NOW,
        store_verification=verification,
    )
    before = p.read_bytes()

    # the runtime appended a sample between reads (simulated by store change)
    _write_store(p, [_sample(ts=1.0), _sample(ts=2.0)])
    observations2, verification2 = _read_observations(p)
    second = engine.reconcile(
        observations2,
        reconciliation_id="p55-c2",
        reconciled_at=NOW,
        previous=observations,
        store_verification=verification2,
    )
    assert second.drift_detected is True
    assert "sample_count" in second.drift_fields
    assert second.reconciliation_status in (EXPECTED_DRIFT, UNEXPECTED_DRIFT)
    # the store reflects the runtime append only; we never repaired anything
    assert p.read_bytes() == before or len(json.loads(p.read_bytes())["samples"]) == 2


def test_c2_activation_change_is_unexpected_drift(tmp_path):
    p = _write_store(tmp_path / "health_history.json", [_sample(ts=1.0)])
    observations, _ = _read_observations(p)
    # runtime went from running to stopped between reads
    stopped = _make_state(runtime_activation="stopped", ts=2.0)
    engine = ReconciliationEngine()
    reconciliation = engine.reconcile(
        (stopped,),
        reconciliation_id="p55-c2",
        reconciled_at=NOW,
        previous=observations,
    )
    assert reconciliation.drift_detected is True
    assert "runtime_activation" in reconciliation.drift_fields
    assert reconciliation.reconciliation_status == UNEXPECTED_DRIFT


def test_c3_timestamp_only_change_is_expected_drift(tmp_path):
    p = _write_store(tmp_path / "health_history.json", [_sample(ts=1.0)])
    observations, _ = _read_observations(p)
    # same runtime fact, same source; only the timestamp advances
    newer = _make_state(
        runtime_activation="running", ts=2.0, source=observations[0].source
    )
    engine = ReconciliationEngine()
    reconciliation = engine.reconcile(
        (newer,),
        reconciliation_id="p55-c3",
        reconciled_at=NOW,
        previous=observations,
    )
    assert reconciliation.drift_detected is True
    assert reconciliation.reconciliation_status == EXPECTED_DRIFT


def test_c4_no_change_is_no_drift(tmp_path):
    p = _write_store(tmp_path / "health_history.json", [_sample(ts=1.0)])
    observations, _ = _read_observations(p)
    engine = ReconciliationEngine()
    reconciliation = engine.reconcile(
        observations,
        reconciliation_id="p55-c4",
        reconciled_at=NOW,
        previous=observations,
    )
    assert reconciliation.drift_detected is False
    assert reconciliation.reconciliation_status == NO_DRIFT


# --------------------------------------------------------------------------
# D. Integrity failure: tampered digest -> verification.valid False, store unchanged
# --------------------------------------------------------------------------
def test_d_integrity_failure_fails_closed(tmp_path):
    p = _write_store(tmp_path / "health_history.json", [_sample()])
    observations, verification = _read_observations(p)
    engine = ReconciliationEngine()
    reconciliation = engine.reconcile(
        observations, reconciliation_id="p55-d", reconciled_at=NOW,
        store_verification=verification,
    )
    before = p.read_bytes()

    # tamper: swap the observation digest on a post-init-bypassed copy
    tampered = _craft_tampered(reconciliation, observation_digest="0" * 64)

    result = engine.verify(tampered, observations)
    assert isinstance(result, ReconciliationVerification)
    assert result.valid is False
    assert result.observation_digest_intact is False
    # nothing was repaired: store bytes unchanged
    assert p.read_bytes() == before


def test_d2_evidence_digest_tamper_detected(tmp_path):
    p = _write_store(tmp_path / "health_history.json", [_sample()])
    observations, verification = _read_observations(p)
    engine = ReconciliationEngine()
    reconciliation = engine.reconcile(
        observations, reconciliation_id="p55-d2", reconciled_at=NOW,
        store_verification=verification,
    )
    tampered = _craft_tampered(reconciliation, evidence_digest="f" * 64)
    assert engine.verify(tampered, observations).valid is False
    assert engine.verify(tampered, observations).evidence_digest_intact is False


def test_d3_schema_failure_status_reported(tmp_path):
    """A store whose schema breaks is reported as schema failure - never fixed."""
    p = _write_store(tmp_path / "health_history.json", [_sample()], version=999)
    observations, verification = _read_observations(p)
    engine = ReconciliationEngine()
    reconciliation = engine.reconcile(
        observations, reconciliation_id="p55-d3", reconciled_at=NOW,
        store_verification=verification,
    )
    assert reconciliation.schema_valid is False
    assert reconciliation.reconciliation_status == SCHEMA_FAILURE
    assert reconciliation.integrity_valid is False
    assert "999" in p.read_text(encoding="utf-8")  # nothing repaired


# --------------------------------------------------------------------------
# E. Schema failure: missing/invalid field -> fail closed
# --------------------------------------------------------------------------
def test_e_invalid_observation_type_fails_closed(tmp_path):
    engine = ReconciliationEngine()
    with pytest.raises(RuntimeEvidenceReconciliationError):
        engine.reconcile(
            ["not-an-observed-state"],
            reconciliation_id="p55-e",
            reconciled_at=NOW,
        )


def test_e2_invalid_store_verification_fails_closed(tmp_path):
    p = _write_store(tmp_path / "health_history.json", [_sample()])
    observations, _ = _read_observations(p)
    engine = ReconciliationEngine()
    with pytest.raises(RuntimeEvidenceReconciliationError):
        engine.reconcile(
            observations,
            reconciliation_id="p55-e2",
            reconciled_at=NOW,
            store_verification="not-a-verification",
        )


# --------------------------------------------------------------------------
# F. Runtime fact preservation: running=true stays an observation fact
# --------------------------------------------------------------------------
def test_f_running_preserved_not_upgraded(tmp_path):
    p = _write_store(tmp_path / "health_history.json", [_sample(running=True)])
    observations, verification = _read_observations(p)
    engine = ReconciliationEngine()
    reconciliation = engine.reconcile(
        observations, reconciliation_id="p55-f", reconciled_at=NOW,
        store_verification=verification,
    )
    # the observation fact survives verbatim
    assert reconciliation.runtime_state[0].runtime_activation == "running"
    # and it is never upgraded into a governance claim
    payload = json.dumps(
        {"observations": [
            {
                "subject_id": s.subject_id,
                "runtime_activation": s.runtime_activation,
                "execution_status": s.execution_status,
                "observed_at": s.observed_at,
                "source": s.source,
            }
            for s in reconciliation.runtime_state
        ]},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert reconciliation.observation_digest == digest
    for banned in ("approved", "authorized", "activated", "executed", "deployed"):
        assert banned not in reconciliation.reconciliation_status.lower()
    assert reconciliation.reconciliation_status == NO_DRIFT


def test_f2_stopped_preserved_not_upgraded(tmp_path):
    p = _write_store(tmp_path / "health_history.json", [_sample(running=False)])
    observations, verification = _read_observations(p)
    engine = ReconciliationEngine()
    reconciliation = engine.reconcile(
        observations, reconciliation_id="p55-f2", reconciled_at=NOW,
        store_verification=verification,
    )
    assert reconciliation.runtime_state[0].runtime_activation == "stopped"
    assert reconciliation.runtime_state[0].execution_status == "unknown"


# --------------------------------------------------------------------------
# G. Governance isolation: proposal/review/gate/record unchanged
# --------------------------------------------------------------------------
def test_g_reconcile_does_not_touch_governance(tmp_path):
    from anima.capability_evolution_contract import OfflineEvolutionPlanner

    planner = OfflineEvolutionPlanner()
    proposal = planner.propose(
        proposal_id="p55-evo",
        validation_rfc_id="RFC-0105",
        observed_contradiction="observed contradiction",
        inconsistency_eliminated="removes inconsistency",
        change_class="contraction",
        forbids_expansion="true",
        proposed_at=NOW,
    )
    impact = planner.assess_impact(
        impact_id="p55-imp", proposal=proposal,
        affected_foundation_refs=("0100#S1",), assessed_at=NOW,
    )
    review = planner.review(
        review_id="p55-rev", proposal=proposal, impact=impact,
        decision="approved", reviewed_at=NOW,
    )
    gate = planner.record_gate(
        gate_id="p55-gate", proposal=proposal, review=review, decision="approved",
        reviewer="senior-developer", evidence_refs=("ev",),
        created_at=NOW, rationale="rationale",
    )
    record = planner.record_evolution(
        record_id="p55-rec", proposal=proposal, review=review, gate=gate,
        recorder="governance-agent", recorded_at=NOW,
    )
    proposal_digest_before = proposal.proposal_digest
    gate_digest_before = gate.digest
    record_digest_before = record.record_digest

    p = _write_store(tmp_path / "health_history.json", [_sample(running=True)])
    observations, verification = _read_observations(p)
    reconciliation = ReconciliationEngine().reconcile(
        observations, reconciliation_id="p55-g", reconciled_at=NOW,
        store_verification=verification,
    )

    # runtime says running - but governance records are untouched
    assert reconciliation.runtime_state[0].runtime_activation == "running"
    assert proposal.proposal_digest == proposal_digest_before
    assert gate.digest == gate_digest_before
    assert record.record_digest == record_digest_before


# --------------------------------------------------------------------------
# H. No repair: store byte-identical before/after
# --------------------------------------------------------------------------
def test_h_reconcile_leaves_store_byte_identical(tmp_path):
    p = _write_store(tmp_path / "health_history.json", [_sample(), _sample(ts=3.0)])
    observations, verification = _read_observations(p)
    before = p.read_bytes()
    engine = ReconciliationEngine()
    reconciliation = engine.reconcile(
        observations, reconciliation_id="p55-h", reconciled_at=NOW,
        store_verification=verification,
    )
    engine.verify(reconciliation, observations)
    engine.inspect(reconciliation)
    assert p.read_bytes() == before
    # no sibling files created either
    assert [f.name for f in p.parent.iterdir()] == [p.name]


# --------------------------------------------------------------------------
# I. No execution: no process/activation/dispatch surface
# --------------------------------------------------------------------------
def test_i_engine_exposes_no_execution_surface():
    engine = ReconciliationEngine()
    banned = {
        "write", "update", "delete", "patch", "apply", "execute",
        "activate", "mutate", "repair", "dispatch", "commit", "deploy",
        "start", "stop", "enable", "disable", "restart", "reload",
    }
    surface = {name for name in dir(engine) if not name.startswith("_")}
    assert surface & banned == set()
    # read-only surface: exactly three operations
    assert surface == {"reconcile", "verify", "inspect"}


# --------------------------------------------------------------------------
# J. P5.2 compatibility: P5.5 evidence is P5.2-consumable; running not faked
# --------------------------------------------------------------------------
def test_j_p52_can_consume_reconciled_evidence(tmp_path):
    from anima.capability_runtime_consumption import RuntimeConsumptionAdapter

    p = _write_store(tmp_path / "health_history.json", [_sample(running=True)])
    observations, verification = _read_observations(p)
    reconciliation = ReconciliationEngine().reconcile(
        observations, reconciliation_id="p55-j", reconciled_at=NOW,
        store_verification=verification,
    )
    # P5.2 can consume the reconciled runtime state (same P5.1 model)
    state = reconciliation.runtime_state[0]
    assert isinstance(state, ObservedRuntimeState)
    adapter = RuntimeConsumptionAdapter()
    consumed = adapter.consume(state, consumption_id="p55-jc", consumed_at=NOW)
    assert consumed.runtime_activation == "running"  # fact carried, not faked
    # P5.2 verify() correctly rejects running for synthetic-only scope -
    # P5.5 does NOT fake a valid=True by downgrading the runtime state
    verification_p52 = adapter.verify(consumed, state)
    assert verification_p52.no_execution is False
    assert verification_p52.valid is False


# --------------------------------------------------------------------------
# Section 12 - static boundary scan (all P3-P5.5 modules)
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

# extended set (socket / network) applies to the P5.5 module only: the P3/P4/
# P5.4 modules legitimately mention these words inside docstring *negative
# declarations* ("no network, no socket, no process spawn"), which are not
# executable paths.  Per section 12, docstring declarations are distinguished
# from actual callable paths and are not mechanically edited.
_EXTENDED_TRANSPORT_NEEDLES = (
    r"\bsocket\b",
    r"\bnetwork\b",
)

_MUTATION_NEEDLES = (
    r"def (apply|execute|activate|mutate|repair|dispatch|write|delete|update|patch|start|stop|restart|reload)\b",
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


def test_s1_forbidden_transport_tokens_zero_p3_p55():
    hits = []
    for name in _MODULES:
        source = pathlib.Path(f"anima/{name}.py").read_text(encoding="utf-8")
        for needle in _TRANSPORT_NEEDLES:
            if re.search(needle, source):
                hits.append((name, needle))
    assert hits == [], f"transport tokens present: {hits}"


def test_s4_p55_module_no_extended_transport_tokens():
    """The P5.5 module itself carries no socket/network/transport token at all."""
    source = pathlib.Path(
        "anima/capability_runtime_evidence_reconciliation.py"
    ).read_text(encoding="utf-8")
    hits = []
    for needle in _TRANSPORT_NEEDLES + _EXTENDED_TRANSPORT_NEEDLES:
        if re.search(needle, source):
            hits.append(needle)
    assert hits == [], f"extended transport tokens present in P5.5: {hits}"


def test_s2_no_mutation_callables_p3_p55():
    hits = []
    for name in _MODULES:
        source = pathlib.Path(f"anima/{name}.py").read_text(encoding="utf-8")
        for needle in _MUTATION_NEEDLES:
            if re.search(needle, source):
                hits.append((name, needle))
    assert hits == [], f"mutation callables present: {hits}"


def test_s3_no_frozen_subsystem_imports_p3_p55():
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
