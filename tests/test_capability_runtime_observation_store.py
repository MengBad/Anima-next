"""P5.4 Real Runtime Observation Store Adapter - authoritative contract.

P5.4 is the first slice that touches a **real** runtime store - but only on
the read side:

    REAL RUNTIME STORE (data/health_history.json)
        -> P5.4 READ-ONLY ADAPTER
        -> P5.1 Observation Model (ObservedRuntimeState)
        -> P5.2 Consumption
        -> Evidence only

Hard boundary (this contract enforces it):

  * READ ONLY        - reading never modifies the store file, never wakes the runtime
  * FAIL CLOSED      - missing/corrupt store raises; nothing is auto-created/repaired
  * NO SIDE EFFECT   - no file write, no DB, no process, no timestamp write-back
  * NO EXECUTION     - no apply/execute/activate/mutate/repair/dispatch surface
  * NO WAKE-UP       - reading requires no runtime, no subprocess/ssh/network
  * P5.1 MODEL ONLY  - outputs are P5.1 ObservedRuntimeState instances
  * P5.2 CONSUMABLE  - P5.2 consumption can continue on P5.4 evidence
  * GOVERNANCE ISOLATED - reading never touches P3/P4 governance records

Digest honesty (section 8): the real store carries NO digest.  P5.4 reports
``absence_of_digest`` as an audit fact and computes a *locally-computed
verification digest* (sha256 of the store bytes) which is clearly labelled as
local - it is never presented as if the store provided it.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
from types import MappingProxyType

import pytest

from anima.capability_runtime_observation import (
    NOT_ACTIVATED,
    OBSERVATION_SCOPE,
    OFFLINE_SOURCE_PREFIXES,
    ObservedRuntimeState,
)
from anima.capability_runtime_observation_store import (  # RED: fails until module exists
    ObservationStoreReadError,
    RealObservationStoreAdapter,
    StoreReadVerification,
)
from anima.capability_runtime_consumption import RuntimeConsumptionAdapter

NOW = "2026-08-11T10:00:00+08:00"
REAL_SAMPLE_TS = 1784200702.721  # real ts shape from data/health_history.json


# --------------------------------------------------------------------------
# fixtures: a store file in the *real* format (version + samples, float ts)
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


def _store_bytes(p: pathlib.Path) -> bytes:
    return p.read_bytes()


# --------------------------------------------------------------------------
# A. Read: real-format store -> observation
# --------------------------------------------------------------------------
def test_a_read_real_format_store_returns_observed_runtime_states(tmp_path):
    p = _write_store(tmp_path / "health_history.json", [_sample(ts=1.0), _sample(ts=2.0)])
    adapter = RealObservationStoreAdapter()
    observations = adapter.read(p)

    assert isinstance(observations, tuple)
    assert len(observations) == 2
    assert all(isinstance(item, ObservedRuntimeState) for item in observations)
    assert observations[0].runtime_activation == "running"
    assert observations[0].observed_at
    assert observations[0].source.startswith(OFFLINE_SOURCE_PREFIXES)


def test_a2_read_repo_real_store_when_present():
    """The actual production store (data/health_history.json) reads cleanly."""
    repo_store = pathlib.Path("data/health_history.json")
    if not repo_store.exists():
        pytest.skip("real observation store not present in this checkout")
    adapter = RealObservationStoreAdapter()
    observations = adapter.read(repo_store)
    assert len(observations) > 0
    verification = adapter.verify(observations, repo_store)
    assert verification.valid is True
    assert verification.absence_of_digest is True


# --------------------------------------------------------------------------
# B/C. Read-only + no mutation
# --------------------------------------------------------------------------
def test_b_read_does_not_modify_store_bytes(tmp_path):
    p = _write_store(tmp_path / "health_history.json", [_sample()])
    before = _store_bytes(p)
    mtime_before = p.stat().st_mtime

    RealObservationStoreAdapter().read(p)

    assert _store_bytes(p) == before
    assert p.stat().st_mtime == mtime_before


def test_c_adapter_call_chain_leaves_store_untouched(tmp_path):
    p = _write_store(tmp_path / "health_history.json", [_sample(), _sample(ts=3.0)])
    before = _store_bytes(p)

    adapter = RealObservationStoreAdapter()
    observations = adapter.read(p)
    adapter.inspect(observations)
    adapter.verify(observations, p)

    assert _store_bytes(p) == before
    # no sibling files created either
    siblings = [f.name for f in p.parent.iterdir()]
    assert siblings == [p.name]


# --------------------------------------------------------------------------
# D. No execution surface
# --------------------------------------------------------------------------
def test_d_adapter_exposes_no_execution_surface():
    adapter = RealObservationStoreAdapter()
    banned = {
        "write", "update", "delete", "patch", "apply", "execute",
        "activate", "mutate", "repair", "dispatch", "commit", "deploy",
        "start", "stop", "enable", "disable",
    }
    surface = {name for name in dir(adapter) if not name.startswith("_")}
    assert surface & banned == set()
    # read-only surface: exactly three operations
    assert surface == {"read", "inspect", "verify"}


# --------------------------------------------------------------------------
# E. Missing store -> fail closed (no auto-creation)
# --------------------------------------------------------------------------
def test_e_missing_store_fails_closed(tmp_path):
    missing = tmp_path / "does_not_exist.json"
    adapter = RealObservationStoreAdapter()
    with pytest.raises(ObservationStoreReadError):
        adapter.read(missing)
    assert not missing.exists(), "fail-closed must not create a fake store"


# --------------------------------------------------------------------------
# F. Corrupt store -> fail closed / verification failure (no auto-repair)
# --------------------------------------------------------------------------
def test_f1_corrupt_json_fails_closed(tmp_path):
    p = tmp_path / "health_history.json"
    p.write_text("{not valid json", encoding="utf-8")
    adapter = RealObservationStoreAdapter()
    with pytest.raises(ObservationStoreReadError):
        adapter.read(p)


def test_f2_verify_reports_schema_breakage(tmp_path):
    p = _write_store(tmp_path / "health_history.json", [_sample()], version=999)
    adapter = RealObservationStoreAdapter()
    verification = adapter.verify((), p)
    assert isinstance(verification, StoreReadVerification)
    assert verification.schema_intact is False
    assert verification.valid is False
    # nothing was repaired: file unchanged
    assert "999" in p.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# G. Governance isolation
# --------------------------------------------------------------------------
def test_g_read_does_not_touch_governance_evidence(tmp_path):
    from anima.capability_evolution_contract import OfflineEvolutionPlanner

    planner = OfflineEvolutionPlanner()
    proposal = planner.propose(
        proposal_id="p54-evo",
        validation_rfc_id="RFC-0105",
        observed_contradiction="observed contradiction",
        inconsistency_eliminated="removes inconsistency",
        change_class="contraction",
        forbids_expansion="true",
        proposed_at=NOW,
    )
    impact = planner.assess_impact(
        impact_id="p54-imp", proposal=proposal,
        affected_foundation_refs=("0100#S1",), assessed_at=NOW,
    )
    review = planner.review(
        review_id="p54-rev", proposal=proposal, impact=impact,
        decision="approved", reviewed_at=NOW,
    )
    gate = planner.record_gate(
        gate_id="p54-gate", proposal=proposal, review=review, decision="approved",
        reviewer="senior-developer", evidence_refs=("ev",),
        created_at=NOW, rationale="rationale",
    )
    record = planner.record_evolution(
        record_id="p54-rec", proposal=proposal, review=review, gate=gate,
        recorder="governance-agent", recorded_at=NOW,
    )
    gate_digest_before = gate.digest
    record_digest_before = record.record_digest

    p = _write_store(tmp_path / "health_history.json", [_sample()])
    RealObservationStoreAdapter().read(p)

    assert gate.digest == gate_digest_before
    assert record.record_digest == record_digest_before


# --------------------------------------------------------------------------
# H/I. P5.1 model + P5.2 consumability
# --------------------------------------------------------------------------
def test_h_outputs_are_p51_observed_runtime_state(tmp_path):
    p = _write_store(tmp_path / "health_history.json", [_sample()])
    observations = RealObservationStoreAdapter().read(p)
    assert isinstance(observations[0], ObservedRuntimeState)
    # source stays inside the P5.1 offline-prefix contract
    assert observations[0].source.startswith(OFFLINE_SOURCE_PREFIXES)


def test_i_p52_consumes_p54_evidence(tmp_path):
    """P5.2 can consume P5.4 evidence — consume succeeds; verify correctly
    reports the real-store observation's runtime claim.

    P5.2 verify() enforces no_execution for synthetic-only scope.  A real
    store observation with runtime_activation="running" legitimately fails
    this check — P5.2 semantics are NOT changed by P5.4.  The proof of
    compatibility is that consume() succeeds and the consumed state is
    digest-bound; verify() correctly identifies the runtime claim.
    """
    p = _write_store(tmp_path / "health_history.json", [_sample(ts=1.0)])
    observations = RealObservationStoreAdapter().read(p)
    adapter = RuntimeConsumptionAdapter()
    # consume succeeds — P5.2 can ingest P5.4 evidence
    consumed = adapter.consume(
        observations[0], consumption_id="p54-c1", consumed_at=NOW
    )
    assert consumed is not None
    assert consumed.consumption_digest  # digest-bound
    assert consumed.runtime_activation == "running"  # faithful carry
    # P5.2 verify() correctly rejects: real-store observation claims running
    verification = adapter.verify(consumed, observations[0])
    assert verification.no_execution is False
    assert verification.valid is False
    assert verification.no_mutation is True  # consume didn't mutate observed state
    assert verification.source_offline is True  # source stays offline


# --------------------------------------------------------------------------
# J. Repeatability
# --------------------------------------------------------------------------
def test_j_repeat_read_identical_when_store_unchanged(tmp_path):
    p = _write_store(tmp_path / "health_history.json", [_sample(ts=1.0), _sample(ts=2.0)])
    adapter = RealObservationStoreAdapter()
    first = adapter.read(p)
    second = adapter.read(p)
    assert first == second
    assert [item.observed_at for item in first] == [item.observed_at for item in second]


def test_j2_repeat_read_reports_difference_not_mutation(tmp_path):
    """If the store changes between reads, P5.4 reports the difference - never repairs."""
    p = _write_store(tmp_path / "health_history.json", [_sample(ts=1.0)])
    adapter = RealObservationStoreAdapter()
    first = adapter.read(p)
    _write_store(p, [_sample(ts=1.0), _sample(ts=2.0)])  # runtime appended a sample
    second = adapter.read(p)
    assert len(second) == 2
    assert len(first) == 1
    # the store file was NOT modified by us - it only reflects the runtime append
    assert len(json.loads(p.read_text(encoding="utf-8"))["samples"]) == 2


# --------------------------------------------------------------------------
# Section 12 - static boundary scan of the new module (+ all P3-P5.4 modules)
# --------------------------------------------------------------------------
_MODULES = (
    "capability_activation_contract",
    "capability_activation_proof",
    "capability_evolution_contract",
    "capability_runtime_observation",
    "capability_runtime_consumption",
    "capability_runtime_observation_store",
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
    r"def (apply|execute|activate|mutate|repair|dispatch|write|delete|update|patch)\b",
    r"\." + "apply" + r"\(",
    r"\." + "execute" + r"\(",
    r"\." + "activate" + r"\(",
    r"\." + "mutate" + r"\(",
    r"\." + "write" + r"\(",
    r"\." + "delete" + r"\(",
    r"\." + "update" + r"\(",
    r"\." + "patch" + r"\(",
    "active" + r"\s*=\s*True",
)


def test_s1_forbidden_transport_tokens_zero_p3_p54():
    hits = []
    for name in _MODULES:
        source = pathlib.Path(f"anima/{name}.py").read_text(encoding="utf-8")
        for needle in _TRANSPORT_NEEDLES:
            if re.search(needle, source):
                hits.append((name, needle))
    assert hits == [], f"transport tokens present: {hits}"


def test_s2_no_mutation_callables_p3_p54():
    hits = []
    for name in _MODULES:
        source = pathlib.Path(f"anima/{name}.py").read_text(encoding="utf-8")
        for needle in _MUTATION_NEEDLES:
            if re.search(needle, source):
                hits.append((name, needle))
    assert hits == [], f"mutation callables present: {hits}"


def test_s3_no_frozen_subsystem_imports_p3_p54():
    frozen = re.compile(
        r"^\s*(?:from|import)\s+anima\."
        r"(?!capability_(?:activation_contract|activation_proof|staged_activation|"
        r"suspend_revocation|evolution_contract|runtime_observation|runtime_consumption|"
        r"runtime_observation_store))"
        r"(lifecycle|constitution|foundation|memory|identity|runtime|capability_contract)\b",
        re.M,
    )
    hits = []
    for name in _MODULES:
        source = pathlib.Path(f"anima/{name}.py").read_text(encoding="utf-8")
        for match in frozen.findall(source):
            hits.append((name, match))
    assert hits == [], f"frozen subsystem imports present: {hits}"
