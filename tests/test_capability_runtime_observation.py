"""RED tests for the P5.1 Runtime Observation Bridge / Read-only Adapter.

P5.1 is an *observation* layer, never an executor.  It consumes the immutable
governance evidence produced by P3 (capability activation) and P4 (foundation
evolution) and normalizes it into read-only observations that can be verified
independently.

The properties proven here:

  * read-only        - observing never mutates the evidence (digest unchanged)
  * digest-bound     - every observation carries and re-verifies the evidence digest
  * provenance-bound - evolution evidence must be bound to its proposal
  * scope-bound      - synthetic-only, offline sources only
  * approval != execution   - an ``approved`` gate is never reported as executed
  * not_activated / not_executed survive normalization
  * drift is *reported*, never repaired
  * no mutating / executing / networking entry point exists

The forbidden-token scan (``test_module_source_contains_no_forbidden_runtime_tokens``)
assembles its needles from fragments on purpose, so that a repository-wide
static boundary scan does not report this assertion file as a false positive.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
from dataclasses import FrozenInstanceError, replace

import pytest

from anima.capability_activation_contract import (
    CapabilityActivationReview,
    OfflineCapabilityActivationPlanner,
    ReviewDecision,
    SyntheticCapabilityProposal,
)
from anima.capability_activation_proof import OfflineActivationProofBuilder
from anima.capability_staged_activation import OfflineStagedActivationPlanner
from anima.capability_suspend_revocation import OfflineSuspendRevocationPlanner
from anima.capability_evolution_contract import OfflineEvolutionPlanner

from anima.capability_runtime_observation import (  # RED: fails until module exists
    NOT_ACTIVATED,
    NOT_APPLICABLE,
    NOT_EXECUTED,
    OBSERVATION_SCOPE,
    EvidenceKind,
    ObservationSnapshot,
    ObservedRuntimeState,
    ReadOnlyRuntimeObservationAdapter,
    RuntimeDriftReport,
    RuntimeObservation,
    RuntimeObservationError,
    RuntimeObservationVerification,
    detect_runtime_drift,
    recompute_evidence_digest,
    verify_runtime_observation,
)


NOW = "2026-08-11T09:00:00+08:00"
SYNTHETIC = "synthetic-only"
OFFLINE_SOURCE = "synthetic://offline/fixture"


def _digest(payload) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# fixtures: real upstream builders only (no hand-rolled evidence)
# --------------------------------------------------------------------------
def _p3_chain(capability_id: str = "cap-observe-1"):
    proposal = SyntheticCapabilityProposal(
        capability_id=capability_id,
        version="0.1.0",
        provider_ref="synthetic://test/harness",
        contract_digest="a" * 64,
        risk_level="low",
        scope=SYNTHETIC,
        proposed_at=NOW,
    )
    review = CapabilityActivationReview(
        review_id="rev-obs-1",
        proposal_id=capability_id,
        decision=ReviewDecision.APPROVE,
        reviewer="governance-agent",
        evidence_refs=("ev-obs-1",),
        reviewed_at=NOW,
    )
    plan = OfflineCapabilityActivationPlanner().plan(
        plan_id="plan-obs-1",
        proposal_id=capability_id,
        proposal=proposal,
        review=review,
        created_at=NOW,
    )
    builder = OfflineActivationProofBuilder()
    record = builder.record(
        record_id="rec-obs-1",
        plan=plan,
        proposal=proposal,
        review=review,
        requested_scope=SYNTHETIC,
        recorded_at=NOW,
    )
    proof = builder.proof(
        proof_id="proof-obs-1",
        record=record,
        result="verified",
        evidence_refs=("ev-obs-1",),
        created_at=NOW,
    )
    staged = OfflineStagedActivationPlanner().stage(
        stage_id="stage-obs-1",
        plan=plan,
        record=record,
        proof=proof,
        proposal=proposal,
        gate_refs=("gate-obs-1",),
        staged_at=NOW,
    )
    return proposal, review, plan, record, proof, staged


def _p3_suspend_revoke(record):
    planner = OfflineSuspendRevocationPlanner()
    suspension = planner.record_suspension(
        suspension_id="susp-obs-1",
        activation_record=record,
        authorization="governance-authorized",
        suspended_at=NOW,
    )
    revocation = planner.record_revocation(
        revocation_id="revo-obs-1",
        activation_record=record,
        suspension=suspension,
        authorization="governance-authorized",
        revoked_at=NOW,
    )
    return suspension, revocation


def _p4_chain(decision: str = "approved"):
    planner = OfflineEvolutionPlanner()
    proposal = planner.propose(
        proposal_id="evo-obs-1",
        validation_rfc_id="RFC-0105",
        observed_contradiction="observed contradiction in the field",
        inconsistency_eliminated="removes the dual-authority inconsistency",
        change_class="contraction",
        forbids_expansion="true",
        proposed_at=NOW,
    )
    impact = planner.assess_impact(
        impact_id="imp-obs-1",
        proposal=proposal,
        affected_foundation_refs=("0100#S1",),
        assessed_at=NOW,
    )
    review = planner.review(
        review_id="evrev-obs-1",
        proposal=proposal,
        impact=impact,
        decision=decision,
        reviewed_at=NOW,
    )
    gate = planner.record_gate(
        gate_id="evgate-obs-1",
        proposal=proposal,
        review=review,
        decision=decision,
        reviewer="senior-developer",
        evidence_refs=("ev-obs-2",),
        created_at=NOW,
        rationale="recorded rationale",
    )
    record = planner.record_evolution(
        record_id="evrec-obs-1",
        proposal=proposal,
        review=review,
        gate=gate,
        recorder="governance-agent",
        recorded_at=NOW,
    )
    return proposal, impact, review, gate, record


def _adapter() -> ReadOnlyRuntimeObservationAdapter:
    return ReadOnlyRuntimeObservationAdapter()


# --------------------------------------------------------------------------
# normalization
# --------------------------------------------------------------------------
def test_observe_activation_record_normalizes_evidence():
    _, _, plan, record, _, _ = _p3_chain()
    observation = _adapter().observe(
        record, observation_id="obs-1", observed_at=NOW
    )
    assert isinstance(observation, RuntimeObservation)
    assert observation.evidence_kind == EvidenceKind.ACTIVATION_RECORD
    assert observation.evidence_id == record.record_id
    assert observation.evidence_digest == record.record_digest
    assert observation.subject_id == record.proposal_id
    assert observation.scope == OBSERVATION_SCOPE == SYNTHETIC
    assert observation.governance_state == NOT_ACTIVATED
    assert observation.runtime_activation_claim == NOT_ACTIVATED
    assert observation.execution_claim == NOT_EXECUTED
    assert plan.plan_digest in observation.provenance_refs


def test_observation_is_immutable():
    _, _, _, record, _, _ = _p3_chain()
    observation = _adapter().observe(record, observation_id="obs-2", observed_at=NOW)
    with pytest.raises(FrozenInstanceError):
        observation.governance_state = "active"  # type: ignore[misc]


def test_observation_digest_is_deterministic():
    _, _, _, record, _, _ = _p3_chain()
    first = _adapter().observe(record, observation_id="obs-3", observed_at=NOW)
    second = _adapter().observe(record, observation_id="obs-3", observed_at=NOW)
    assert first.observation_digest == second.observation_digest
    assert len(first.observation_digest) == 64


def test_observe_rejects_unsupported_evidence():
    with pytest.raises(RuntimeObservationError):
        _adapter().observe({"not": "evidence"}, observation_id="obs-4", observed_at=NOW)


def test_observe_staged_activation_reports_not_activated():
    _, _, _, _, _, staged = _p3_chain()
    observation = _adapter().observe(staged, observation_id="obs-5", observed_at=NOW)
    assert observation.evidence_kind == EvidenceKind.STAGED_ACTIVATION
    assert observation.governance_state == "staged"
    assert observation.runtime_activation_claim == NOT_ACTIVATED
    assert observation.execution_claim == NOT_EXECUTED


def test_observe_suspension_and_revocation():
    _, _, _, record, _, _ = _p3_chain()
    suspension, revocation = _p3_suspend_revoke(record)
    adapter = _adapter()
    susp_obs = adapter.observe(suspension, observation_id="obs-6", observed_at=NOW)
    revo_obs = adapter.observe(revocation, observation_id="obs-7", observed_at=NOW)
    assert susp_obs.evidence_kind == EvidenceKind.SUSPENSION
    assert susp_obs.governance_state == "suspended"
    assert susp_obs.runtime_activation_claim == NOT_ACTIVATED
    assert revo_obs.evidence_kind == EvidenceKind.REVOCATION
    assert revo_obs.governance_state == "revoked"
    assert revo_obs.runtime_activation_claim == NOT_ACTIVATED


def test_observe_requires_bound_proposal_for_evolution_evidence():
    _, _, _, _, evolution_record = _p4_chain()
    with pytest.raises(RuntimeObservationError):
        _adapter().observe(
            evolution_record, observation_id="obs-8", observed_at=NOW
        )


def test_observe_rejects_proposal_with_broken_digest_binding():
    proposal, _, _, _, evolution_record = _p4_chain()
    tampered_proposal = replace(proposal, proposal_digest="b" * 64)
    with pytest.raises(RuntimeObservationError):
        _adapter().observe(
            evolution_record,
            observation_id="obs-9",
            observed_at=NOW,
            proposal=tampered_proposal,
        )


def test_observe_evolution_record_reports_lifecycle_without_execution():
    proposal, _, _, _, evolution_record = _p4_chain()
    observation = _adapter().observe(
        evolution_record, observation_id="obs-10", observed_at=NOW, proposal=proposal
    )
    assert observation.evidence_kind == EvidenceKind.EVOLUTION_RECORD
    assert observation.governance_state == "recorded"
    assert observation.execution_claim == NOT_EXECUTED
    assert observation.runtime_activation_claim == NOT_APPLICABLE
    assert observation.scope == SYNTHETIC


def test_approved_gate_is_never_reported_as_executed():
    """approval != execution must survive the observation layer."""
    proposal, _, _, gate, _ = _p4_chain(decision="approved")
    observation = _adapter().observe(
        gate, observation_id="obs-11", observed_at=NOW, proposal=proposal
    )
    assert observation.governance_state == "approved"
    assert observation.execution_claim == NOT_EXECUTED
    assert observation.runtime_activation_claim == NOT_APPLICABLE
    for forbidden in ("executed", "active", "deployed", "running"):
        assert observation.execution_claim != forbidden
        assert observation.runtime_activation_claim != forbidden


# --------------------------------------------------------------------------
# verification (fails closed)
# --------------------------------------------------------------------------
def test_verify_runtime_observation_valid_for_intact_evidence():
    _, _, _, record, _, _ = _p3_chain()
    observation = _adapter().observe(record, observation_id="obs-12", observed_at=NOW)
    verification = verify_runtime_observation(observation, record)
    assert isinstance(verification, RuntimeObservationVerification)
    assert verification.valid is True
    assert verification.evidence_intact is True
    assert verification.governance_binding_intact is True
    assert verification.scope_intact is True
    assert verification.no_mutation is True
    assert verification.no_execution is True
    assert verification.drift_detected is False
    assert verification.errors == ()


def test_verify_detects_tampered_evidence():
    _, _, _, record, _, _ = _p3_chain()
    observation = _adapter().observe(record, observation_id="obs-13", observed_at=NOW)
    tampered = replace(record, scope=SYNTHETIC, plan_id="plan-tampered")
    verification = verify_runtime_observation(observation, tampered)
    assert verification.valid is False
    assert verification.evidence_intact is False
    assert verification.errors


def test_verify_detects_broken_governance_binding():
    _, _, _, record, _, _ = _p3_chain()
    observation = _adapter().observe(record, observation_id="obs-14", observed_at=NOW)
    broken = replace(observation, evidence_digest="c" * 64)
    verification = verify_runtime_observation(broken, record)
    assert verification.valid is False
    assert verification.governance_binding_intact is False


def test_verify_detects_scope_outside_synthetic_only():
    _, _, _, record, _, _ = _p3_chain()
    observation = _adapter().observe(record, observation_id="obs-15", observed_at=NOW)
    escaped = replace(observation, scope="production")
    verification = verify_runtime_observation(escaped, record)
    assert verification.valid is False
    assert verification.scope_intact is False


def test_verify_detects_runtime_activation_invariant_violation():
    _, _, _, _, _, staged = _p3_chain()
    observation = _adapter().observe(staged, observation_id="obs-16", observed_at=NOW)
    lying = replace(observation, runtime_activation_claim="activated")
    verification = verify_runtime_observation(lying, staged)
    assert verification.valid is False
    assert verification.no_execution is False or verification.errors


def test_observation_does_not_mutate_evidence():
    _, _, _, record, _, _ = _p3_chain()
    before = record.record_digest
    adapter = _adapter()
    observation = adapter.observe(record, observation_id="obs-17", observed_at=NOW)
    adapter.inspect(observation)
    verify_runtime_observation(observation, record)
    assert record.record_digest == before
    assert recompute_evidence_digest(record) == before


# --------------------------------------------------------------------------
# drift: report only, never repair
# --------------------------------------------------------------------------
def test_detect_runtime_drift_reports_contradiction_without_fixing():
    _, _, _, _, _, staged = _p3_chain()
    observation = _adapter().observe(staged, observation_id="obs-18", observed_at=NOW)
    observed = ObservedRuntimeState(
        subject_id=observation.subject_id,
        runtime_activation="activated",
        execution_status="executed",
        observed_at=NOW,
        source=OFFLINE_SOURCE,
    )
    report = detect_runtime_drift(observation, observed)
    assert isinstance(report, RuntimeDriftReport)
    assert report.drift_detected is True
    assert report.contradictions
    # the governance evidence is untouched by drift detection
    assert staged.runtime_activation == NOT_ACTIVATED
    assert recompute_evidence_digest(staged) == staged.stage_digest


def test_detect_runtime_drift_clean_when_consistent():
    _, _, _, _, _, staged = _p3_chain()
    observation = _adapter().observe(staged, observation_id="obs-19", observed_at=NOW)
    observed = ObservedRuntimeState(
        subject_id=observation.subject_id,
        runtime_activation=NOT_ACTIVATED,
        execution_status=NOT_EXECUTED,
        observed_at=NOW,
        source=OFFLINE_SOURCE,
    )
    report = detect_runtime_drift(observation, observed)
    assert report.drift_detected is False
    assert report.contradictions == ()


def test_drift_report_has_no_remediation_entry_points():
    _, _, _, _, _, staged = _p3_chain()
    observation = _adapter().observe(staged, observation_id="obs-20", observed_at=NOW)
    observed = ObservedRuntimeState(
        subject_id=observation.subject_id,
        runtime_activation="activated",
        execution_status="executed",
        observed_at=NOW,
        source=OFFLINE_SOURCE,
    )
    report = detect_runtime_drift(observation, observed)
    banned = ("fix", "repair", "remediate", "reconcile", "resolve", "sync")
    for name in dir(report):
        assert not any(name.startswith(prefix) for prefix in banned), name


def test_observed_runtime_state_rejects_non_offline_source():
    with pytest.raises(RuntimeObservationError):
        ObservedRuntimeState(
            subject_id="cap-observe-1",
            runtime_activation=NOT_ACTIVATED,
            execution_status=NOT_EXECUTED,
            observed_at=NOW,
            source="https://production.example.com/api",
        )


# --------------------------------------------------------------------------
# collection surface
# --------------------------------------------------------------------------
def test_collect_observation_returns_readonly_tuple():
    _, _, _, record, _, staged = _p3_chain()
    observations = _adapter().collect_observation(
        (record, staged), observation_id_prefix="batch", observed_at=NOW
    )
    assert isinstance(observations, tuple)
    assert len(observations) == 2
    assert all(isinstance(item, RuntimeObservation) for item in observations)


def test_snapshot_is_immutable_and_digest_bound():
    _, _, _, record, _, staged = _p3_chain()
    adapter = _adapter()
    observations = adapter.collect_observation(
        (record, staged), observation_id_prefix="snap", observed_at=NOW
    )
    snapshot = adapter.snapshot(
        observations, snapshot_id="snapshot-1", collected_at=NOW
    )
    assert isinstance(snapshot, ObservationSnapshot)
    assert len(snapshot.observations) == 2
    expected = _digest(
        {
            "snapshot_id": "snapshot-1",
            "observation_digests": [item.observation_digest for item in observations],
            "collected_at": NOW,
        }
    )
    assert snapshot.snapshot_digest == expected
    with pytest.raises(FrozenInstanceError):
        snapshot.snapshot_id = "other"  # type: ignore[misc]


def test_inspect_returns_readonly_mapping():
    _, _, _, record, _, _ = _p3_chain()
    adapter = _adapter()
    observation = adapter.observe(record, observation_id="obs-21", observed_at=NOW)
    view = adapter.inspect(observation)
    assert view["evidence_digest"] == record.record_digest
    with pytest.raises(TypeError):
        view["evidence_digest"] = "x"  # type: ignore[index]


def test_read_state_returns_governance_state_only():
    _, _, _, record, _, _ = _p3_chain()
    assert _adapter().read_state(record) == NOT_ACTIVATED


# --------------------------------------------------------------------------
# structural boundary
# --------------------------------------------------------------------------
def test_adapter_exposes_no_mutating_entry_points():
    adapter = _adapter()
    banned_exact = {
        "act" + "ivate",
        "exec" + "ute",
        "app" + "ly",
        "mut" + "ate",
        "com" + "mit",
        "dep" + "loy",
        "start",
        "stop",
        "enable",
        "disable",
        "approve",
        "revoke",
        "suspend",
        "resume",
        "provision",
        "restart",
    }
    surface = {name for name in dir(adapter) if not name.startswith("_")}
    assert surface & banned_exact == set()
    assert surface == {"observe", "read_state", "snapshot", "inspect", "collect_observation"}


def test_module_source_contains_no_forbidden_runtime_tokens():
    """Same strictness as the P4.4 closure scan, applied to the P5.1 module.

    P4.4 pins its own ``ALL_MODULES`` list; extending that list would mutate a
    frozen P4.4 artifact, so P5.1 carries an equivalent (superset) scan here.
    """
    module_path = pathlib.Path("anima/capability_runtime_observation.py")
    source = module_path.read_text(encoding="utf-8")
    patterns = (
        "sub" + "process",
        r"\b" + "ssh" + r"\b",
        "system" + "ctl",
        "doc" + "ker",
        r"\b" + "requests" + r"\b",
        "htt" + "px",
        "aio" + "http",
        "url" + "lib",
        r"http\." + "client",
        "import" + "lib",
        "__" + "import__",
        "para" + "miko",
        r"\b" + "fabric" + r"\b",
        r"\b" + "socket" + r"\b",
        r"\." + "activate" + r"\(",
        r"\." + "enable" + r"\(",
        r"\." + "start" + r"\(",
        r"\." + "execute" + r"\(",
        r"\." + "apply" + r"\(",
        r"\." + "mutate" + r"\(",
        r"\." + "run" + r"\(",
        "active" + r"\s*=\s*True",
        "enabled" + r"\s*=\s*True",
    )
    hits = [needle for needle in patterns if re.search(needle, source)]
    assert hits == [], f"forbidden runtime pattern(s) present: {hits}"


def test_module_does_not_import_frozen_subsystems():
    module_path = pathlib.Path("anima/capability_runtime_observation.py")
    source = module_path.read_text(encoding="utf-8")
    frozen = re.compile(
        r"^\s*(?:from|import)\s+anima\."
        r"(?!capability_(?:activation_contract|activation_proof|staged_activation|"
        r"suspend_revocation|evolution_contract|runtime_observation))"
        r"(lifecycle|constitution|foundation|memory|identity|runtime|capability_contract)\b",
        re.M,
    )
    assert frozen.findall(source) == []


def test_evidence_digest_mirror_matches_upstream_builders():
    """The read-only digest mirror must track the real upstream payloads."""
    proposal, _, _, record, _, staged = _p3_chain()
    suspension, revocation = _p3_suspend_revoke(record)
    evo_proposal, _, _, gate, evo_record = _p4_chain()
    assert recompute_evidence_digest(record) == record.record_digest
    assert recompute_evidence_digest(staged) == staged.stage_digest
    assert recompute_evidence_digest(suspension) == suspension.suspension_digest
    assert recompute_evidence_digest(revocation) == revocation.revocation_digest
    assert recompute_evidence_digest(gate) == gate.digest
    assert recompute_evidence_digest(evo_record) == evo_record.record_digest
