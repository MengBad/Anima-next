"""P5.3 Runtime Boundary Closure Audit - authoritative verification contract.

This is an **audit / verification** slice.  It adds no runtime capability; it
proves the closure of the existing read-only chain:

    Governance Evidence (P3.1-P3.4, P4.1-P4.4)
        -> Runtime Observation   (P5.1, read-only)
        -> Runtime Consumption   (P5.2, read-only)
        -> Verification / Reconciliation (report-only)
        -> Evidence only

The five closure properties audited (mirroring the P5.3 authorization):

  A. Observation produces no mutation   (P5.1 observe/snapshot/verify/drift)
  B. Consumption produces no mutation   (P5.2 consume/inspect/verify/reconcile)
  C. Governance state is never rewritten by runtime observation
  D. approval != activation != execution, never upgraded
  E. The full digest chain remains intact and new digests never modify upstream

Plus the section-4 static boundary audit over **all** P3-P5 capability modules
(not only P5.3) and the section-5 public-surface audit.

Reported counters (all must be 0):

    forbidden_hits           = 0
    frozen_module_imports    = 0
    runtime_mutation_paths   = 0

If any counter is non-zero the slice STOPS with a report; nothing is repaired
here.
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

from anima.capability_runtime_observation import (
    NOT_ACTIVATED,
    NOT_APPLICABLE,
    NOT_EXECUTED,
    OBSERVATION_SCOPE,
    ObservedRuntimeState,
    ReadOnlyRuntimeObservationAdapter,
    RuntimeDriftReport,
    RuntimeObservation,
    detect_runtime_drift,
    recompute_evidence_digest,
    verify_runtime_observation,
)
from anima.capability_runtime_consumption import (
    RuntimeConsumptionAdapter,
    RuntimeConsumptionVerification,
)


NOW = "2026-08-11T10:00:00+08:00"
SYNTHETIC = "synthetic-only"
OFFLINE_SOURCE = "synthetic://offline/fixture"


def _digest(payload) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# fixtures: real upstream builders only (no hand-rolled evidence)
# --------------------------------------------------------------------------
def _p3_chain(capability_id: str = "cap-audit-1"):
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
        review_id="rev-audit-1",
        proposal_id=capability_id,
        decision=ReviewDecision.APPROVE,
        reviewer="governance-agent",
        evidence_refs=("ev-audit-1",),
        reviewed_at=NOW,
    )
    plan = OfflineCapabilityActivationPlanner().plan(
        plan_id="plan-audit-1",
        proposal_id=capability_id,
        proposal=proposal,
        review=review,
        created_at=NOW,
    )
    builder = OfflineActivationProofBuilder()
    record = builder.record(
        record_id="rec-audit-1",
        plan=plan,
        proposal=proposal,
        review=review,
        requested_scope=SYNTHETIC,
        recorded_at=NOW,
    )
    proof = builder.proof(
        proof_id="proof-audit-1",
        record=record,
        result="verified",
        evidence_refs=("ev-audit-1",),
        created_at=NOW,
    )
    staged = OfflineStagedActivationPlanner().stage(
        stage_id="stage-audit-1",
        plan=plan,
        record=record,
        proof=proof,
        proposal=proposal,
        gate_refs=("gate-audit-1",),
        staged_at=NOW,
    )
    return proposal, review, plan, record, proof, staged


def _p3_suspend_revoke(record):
    planner = OfflineSuspendRevocationPlanner()
    suspension = planner.record_suspension(
        suspension_id="susp-audit-1",
        activation_record=record,
        authorization="governance-authorized",
        suspended_at=NOW,
    )
    revocation = planner.record_revocation(
        revocation_id="revo-audit-1",
        activation_record=record,
        suspension=suspension,
        authorization="governance-authorized",
        revoked_at=NOW,
    )
    return suspension, revocation


def _p4_chain(decision: str = "approved"):
    planner = OfflineEvolutionPlanner()
    proposal = planner.propose(
        proposal_id="evo-audit-1",
        validation_rfc_id="RFC-0105",
        observed_contradiction="observed contradiction in the field",
        inconsistency_eliminated="removes the dual-authority inconsistency",
        change_class="contraction",
        forbids_expansion="true",
        proposed_at=NOW,
    )
    impact = planner.assess_impact(
        impact_id="imp-audit-1",
        proposal=proposal,
        affected_foundation_refs=("0100#S1",),
        assessed_at=NOW,
    )
    review = planner.review(
        review_id="evrev-audit-1",
        proposal=proposal,
        impact=impact,
        decision=decision,
        reviewed_at=NOW,
    )
    gate = planner.record_gate(
        gate_id="evgate-audit-1",
        proposal=proposal,
        review=review,
        decision=decision,
        reviewer="senior-developer",
        evidence_refs=("ev-audit-2",),
        created_at=NOW,
        rationale="recorded rationale",
    )
    record = planner.record_evolution(
        record_id="evrec-audit-1",
        proposal=proposal,
        review=review,
        gate=gate,
        recorder="governance-agent",
        recorded_at=NOW,
    )
    return proposal, impact, review, gate, record


def _observe_adapter() -> ReadOnlyRuntimeObservationAdapter:
    return ReadOnlyRuntimeObservationAdapter()


def _consume_adapter() -> RuntimeConsumptionAdapter:
    return RuntimeConsumptionAdapter()


# ==========================================================================
# A. Observation produces no mutation (P5.1)
# ==========================================================================
def test_a1_observe_snapshot_verify_drift_do_not_mutate_evidence():
    """observe/snapshot/verify/drift over the whole evidence set -> digests unchanged."""
    proposal, _, _, record, _, staged = _p3_chain()
    suspension, revocation = _p3_suspend_revoke(record)
    evo_proposal, _, _, gate, evo_record = _p4_chain()

    before = {
        "record": (record.record_digest, recompute_evidence_digest(record)),
        "staged": (staged.stage_digest, recompute_evidence_digest(staged)),
        "suspension": (suspension.suspension_digest, recompute_evidence_digest(suspension)),
        "revocation": (revocation.revocation_digest, recompute_evidence_digest(revocation)),
        "gate": (gate.digest, recompute_evidence_digest(gate)),
        "evo_record": (evo_record.record_digest, recompute_evidence_digest(evo_record)),
    }

    adapter = _observe_adapter()
    observations = adapter.collect_observation(
        (record, staged, suspension, revocation),
        observation_id_prefix="audit-obs",
        observed_at=NOW,
    )
    observations += (
        adapter.observe(
            gate, observation_id="audit-gate", observed_at=NOW, proposal=evo_proposal
        ),
        adapter.observe(
            evo_record,
            observation_id="audit-evo",
            observed_at=NOW,
            proposal=evo_proposal,
        ),
    )
    snapshot = adapter.snapshot(
        observations, snapshot_id="audit-snap", collected_at=NOW
    )
    for item in observations:
        verify_runtime_observation(item, _evidence_for(item, locals()))
    detect_runtime_drift(
        observations[1],
        ObservedRuntimeState(
            subject_id=staged.capability_id,
            runtime_activation=NOT_ACTIVATED,
            execution_status=NOT_EXECUTED,
            observed_at=NOW,
            source=OFFLINE_SOURCE,
        ),
    )

    assert record.record_digest == before["record"][0]
    assert staged.stage_digest == before["staged"][0]
    assert suspension.suspension_digest == before["suspension"][0]
    assert revocation.revocation_digest == before["revocation"][0]
    assert gate.digest == before["gate"][0]
    assert evo_record.record_digest == before["evo_record"][0]
    # recomputation is still identical after the whole observation cycle
    assert recompute_evidence_digest(record) == before["record"][1]
    assert recompute_evidence_digest(staged) == before["staged"][1]
    assert recompute_evidence_digest(gate) == before["gate"][1]
    assert recompute_evidence_digest(evo_record) == before["evo_record"][1]


def _evidence_for(observation: RuntimeObservation, scope: dict):
    """Map an observation back to the evidence it was built from (audit-only)."""
    for name in (
        "record",
        "staged",
        "suspension",
        "revocation",
        "gate",
        "evo_record",
    ):
        candidate = scope.get(name)
        if candidate is not None and recompute_evidence_digest(candidate) == observation.evidence_digest:
            return candidate
    raise AssertionError(f"no evidence found for observation {observation.observation_id}")


def test_a2_p51_public_surface_is_exactly_read_only_five():
    adapter = _observe_adapter()
    banned = {
        "activate", "execute", "apply", "mutate", "commit", "deploy",
        "start", "stop", "enable", "disable", "approve", "revoke",
        "suspend", "resume", "provision", "restart", "repair", "dispatch",
    }
    surface = {name for name in dir(adapter) if not name.startswith("_")}
    assert surface & banned == set()
    assert surface == {
        "observe",
        "read_state",
        "collect_observation",
        "snapshot",
        "inspect",
    }


def test_a3_p51_drift_report_has_no_remediation_entry_points():
    _, _, _, _, _, staged = _p3_chain()
    observation = _observe_adapter().observe(
        staged, observation_id="audit-drift", observed_at=NOW
    )
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
    banned = ("fix", "repair", "remediate", "reconcile", "resolve", "sync")
    for name in dir(report):
        assert not any(name.startswith(prefix) for prefix in banned), name
    # the observed state itself is untouched by drift detection
    assert observed.runtime_activation == "activated"
    assert staged.runtime_activation == NOT_ACTIVATED


# ==========================================================================
# B. Consumption produces no mutation (P5.2)
# ==========================================================================
def test_b1_consume_inspect_verify_do_not_mutate_observed_state():
    adapter = _consume_adapter()
    state = ObservedRuntimeState(
        subject_id="cap-audit-1",
        runtime_activation=NOT_ACTIVATED,
        execution_status=NOT_EXECUTED,
        observed_at=NOW,
        source=OFFLINE_SOURCE,
    )
    consumed = adapter.consume(state, consumption_id="audit-c1", consumed_at=NOW)
    adapter.inspect(consumed)
    verification = adapter.verify(consumed, state)
    assert verification.valid is True
    # the observed state is unchanged by the whole consumption cycle
    assert state.runtime_activation == NOT_ACTIVATED
    assert state.execution_status == NOT_EXECUTED
    assert state.source == OFFLINE_SOURCE


def test_b2_reconcile_is_report_only_never_repairs():
    adapter = _consume_adapter()
    state = ObservedRuntimeState(
        subject_id="cap-audit-1",
        runtime_activation=NOT_ACTIVATED,
        execution_status=NOT_EXECUTED,
        observed_at=NOW,
        source=OFFLINE_SOURCE,
    )
    consumed = adapter.consume(state, consumption_id="audit-c2", consumed_at=NOW)
    report = adapter.reconcile(consumed, state)
    assert report.reconciled is True
    assert report.contradictions == ()
    # reconcile == report only: neither side changed, nothing was written back
    assert consumed.runtime_activation == NOT_ACTIVATED
    assert state.runtime_activation == NOT_ACTIVATED


def test_b2b_reconcile_reports_contradiction_without_repairing():
    adapter = _consume_adapter()
    state_a = ObservedRuntimeState(
        subject_id="cap-audit-1",
        runtime_activation=NOT_ACTIVATED,
        execution_status=NOT_EXECUTED,
        observed_at=NOW,
        source=OFFLINE_SOURCE,
    )
    state_b = ObservedRuntimeState(
        subject_id="cap-audit-1",
        runtime_activation="active",
        execution_status="executed",
        observed_at=NOW,
        source=OFFLINE_SOURCE,
    )
    consumed = adapter.consume(state_a, consumption_id="audit-c3", consumed_at=NOW)
    report = adapter.reconcile(consumed, state_b)
    assert report.reconciled is False
    assert len(report.contradictions) >= 1
    # the contradiction is reported, not resolved
    assert state_b.runtime_activation == "active"
    assert consumed.runtime_activation == NOT_ACTIVATED


def test_b3_p52_public_surface_is_exactly_read_only_four():
    adapter = _consume_adapter()
    banned = {
        "apply", "execute", "activate", "mutate", "repair", "dispatch",
        "commit", "deploy", "start", "stop", "enable", "disable",
    }
    surface = {name for name in dir(adapter) if not name.startswith("_")}
    assert surface & banned == set()
    assert surface == {"consume", "inspect", "verify", "reconcile"}


# ==========================================================================
# C. Governance state is never rewritten by runtime observation
# ==========================================================================
def test_c1_full_observation_consumption_cycle_leaves_governance_intact():
    """P3 evidence -> P5.1 observation -> P5.2 consumption: governance unchanged."""
    proposal, _, _, record, _, _ = _p3_chain()
    adapter = _observe_adapter()
    observation = adapter.observe(record, observation_id="audit-c1", observed_at=NOW)
    verification = verify_runtime_observation(observation, record)
    assert verification.valid is True

    state = ObservedRuntimeState(
        subject_id=observation.subject_id,
        runtime_activation=observation.runtime_activation_claim,
        execution_status=observation.execution_claim,
        observed_at=NOW,
        source=OFFLINE_SOURCE,
    )
    consumed = _consume_adapter().consume(
        state, consumption_id="audit-c1c", consumed_at=NOW
    )
    assert _consume_adapter().verify(consumed, state).valid is True
    # governance evidence still hashes to itself after the entire cycle
    assert recompute_evidence_digest(record) == record.record_digest


def test_c2_p3_p4_modules_have_no_runtime_or_frozen_subsystem_imports():
    p3p4_modules = (
        "capability_activation_contract",
        "capability_activation_proof",
        "capability_staged_activation",
        "capability_suspend_revocation",
        "capability_evolution_contract",
    )
    for name in p3p4_modules:
        source = pathlib.Path(f"anima/{name}.py").read_text(encoding="utf-8")
        # no frozen subsystem imports
        frozen = re.compile(
            r"^\s*(?:from|import)\s+anima\."
            r"(lifecycle|constitution|foundation|memory|identity|runtime|capability_contract)\b",
            re.M,
        )
        assert frozen.findall(source) == [], f"{name}: frozen subsystem import"
        # no transport token
        for token in ("subprocess", "socket", "httpx", "requests", "paramiko"):
            assert token not in source, f"{name}: transport token {token!r}"


# ==========================================================================
# D. approval != activation != execution
# ==========================================================================
def test_d1_approved_activation_never_reported_as_executed():
    """P3 approved record must stay not_activated / not_executed in observation."""
    _, _, _, record, _, _ = _p3_chain()
    observation = _observe_adapter().observe(
        record, observation_id="audit-d1", observed_at=NOW
    )
    assert observation.governance_state == NOT_ACTIVATED
    assert observation.runtime_activation_claim == NOT_ACTIVATED
    assert observation.execution_claim == NOT_EXECUTED
    for forbidden in ("executed", "active", "deployed", "running"):
        assert observation.execution_claim != forbidden
        assert observation.runtime_activation_claim != forbidden


def test_d2_approved_gate_never_reported_as_executed():
    """P4 approved gate must stay not_applicable / not_executed in observation."""
    proposal, _, _, gate, _ = _p4_chain(decision="approved")
    observation = _observe_adapter().observe(
        gate, observation_id="audit-d2", observed_at=NOW, proposal=proposal
    )
    assert observation.governance_state == "approved"
    assert observation.runtime_activation_claim == NOT_APPLICABLE
    assert observation.execution_claim == NOT_EXECUTED
    for forbidden in ("executed", "active", "deployed", "running"):
        assert observation.execution_claim != forbidden


def test_d3_not_activated_not_executed_survive_consumption():
    adapter = _consume_adapter()
    state = ObservedRuntimeState(
        subject_id="cap-audit-1",
        runtime_activation=NOT_ACTIVATED,
        execution_status=NOT_EXECUTED,
        observed_at=NOW,
        source=OFFLINE_SOURCE,
    )
    consumed = adapter.consume(state, consumption_id="audit-d3", consumed_at=NOW)
    verification = adapter.verify(consumed, state)
    assert isinstance(verification, RuntimeConsumptionVerification)
    assert verification.no_execution is True
    assert consumed.runtime_activation == NOT_ACTIVATED
    assert consumed.execution_status == NOT_EXECUTED
    # a consumption that claims execution fails closed
    lying = replace(consumed, execution_status="executed")
    assert adapter.verify(lying, state).no_execution is False


# ==========================================================================
# E. Digest chain
# ==========================================================================
def test_e1_digest_chain_proposal_review_gate_record_observation_consumption():
    """Every hop of the chain stays digest-bound and verifiable."""
    proposal, _, _, gate, evo_record = _p4_chain()
    # hop 1-2: gate is bound to its proposal by digest reference
    assert gate.proposal_digest == proposal.proposal_digest
    # hop 2-3: evolution record is bound to its gate by digest reference
    assert evo_record.gate_digest == gate.digest
    # hop 3-4: gate and record still hash to their carried digests (P5.1 mirror)
    assert recompute_evidence_digest(gate) == gate.digest
    assert recompute_evidence_digest(evo_record) == evo_record.record_digest

    # hop 4-5: observation is digest-bound to the record and verifies clean
    observation = _observe_adapter().observe(
        evo_record, observation_id="audit-e1", observed_at=NOW, proposal=proposal
    )
    verification = verify_runtime_observation(observation, evo_record)
    assert verification.valid is True
    assert verification.evidence_intact is True

    # hop 5-6: consumption is digest-bound to the observed state and verifies clean
    state = ObservedRuntimeState(
        subject_id=observation.subject_id,
        runtime_activation=observation.runtime_activation_claim,
        execution_status=observation.execution_claim,
        observed_at=NOW,
        source=OFFLINE_SOURCE,
    )
    consumed = _consume_adapter().consume(
        state, consumption_id="audit-e1c", consumed_at=NOW
    )
    assert _consume_adapter().verify(consumed, state).valid is True
    assert len(consumed.consumption_digest) == 64
    assert len(observation.observation_digest) == 64


def test_e2_new_digests_never_modify_upstream_records():
    """observation_digest / consumption_digest are additive; upstream digests stable."""
    proposal, _, _, gate, evo_record = _p4_chain()
    gate_digest_before = gate.digest
    record_digest_before = evo_record.record_digest

    observation = _observe_adapter().observe(
        evo_record, observation_id="audit-e2", observed_at=NOW, proposal=proposal
    )
    state = ObservedRuntimeState(
        subject_id=observation.subject_id,
        runtime_activation=observation.runtime_activation_claim,
        execution_status=observation.execution_claim,
        observed_at=NOW,
        source=OFFLINE_SOURCE,
    )
    _consume_adapter().consume(state, consumption_id="audit-e2c", consumed_at=NOW)

    assert gate.digest == gate_digest_before
    assert evo_record.record_digest == record_digest_before
    assert recompute_evidence_digest(gate) == gate_digest_before
    assert recompute_evidence_digest(evo_record) == record_digest_before


def test_e3_observation_digest_binding_fails_closed_on_tamper():
    """A tampered evidence binding cannot pass verification (fails closed)."""
    _, _, _, record, _, _ = _p3_chain()
    adapter = _observe_adapter()
    observation = adapter.observe(record, observation_id="audit-e3", observed_at=NOW)
    tampered = replace(record, plan_id="plan-tampered")
    verification = verify_runtime_observation(observation, tampered)
    assert verification.valid is False
    assert verification.evidence_intact is False


# ==========================================================================
# Section 4 - static boundary audit over ALL P3-P5 capability modules
# ==========================================================================
_AUDIT_MODULES = (
    "capability_activation_contract",
    "capability_activation_proof",
    "capability_evolution_contract",
    "capability_runtime_observation",
    "capability_runtime_consumption",
    "capability_staged_activation",
    "capability_suspend_revocation",
)

# needles are assembled from fragments so this audit file never self-triggers
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

_MUTATION_CALLABLE_NEEDLES = (
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


def test_s1_forbidden_transport_tokens_zero_across_p3_p5():
    hits = []
    for name in _AUDIT_MODULES:
        source = pathlib.Path(f"anima/{name}.py").read_text(encoding="utf-8")
        for needle in _TRANSPORT_NEEDLES:
            if re.search(needle, source):
                hits.append((name, needle))
    assert hits == [], f"transport tokens present: {hits}"


def test_s2_no_mutation_callables_across_p3_p5():
    hits = []
    for name in _AUDIT_MODULES:
        source = pathlib.Path(f"anima/{name}.py").read_text(encoding="utf-8")
        for needle in _MUTATION_CALLABLE_NEEDLES:
            if re.search(needle, source):
                hits.append((name, needle))
    assert hits == [], f"mutation callable patterns present: {hits}"


def test_s3_frozen_module_imports_zero_across_p3_p5():
    frozen = re.compile(
        r"^\s*(?:from|import)\s+anima\."
        r"(?!capability_(?:activation_contract|activation_proof|staged_activation|"
        r"suspend_revocation|evolution_contract|runtime_observation|runtime_consumption))"
        r"(lifecycle|constitution|foundation|memory|identity|runtime|capability_contract)\b",
        re.M,
    )
    hits = []
    for name in _AUDIT_MODULES:
        source = pathlib.Path(f"anima/{name}.py").read_text(encoding="utf-8")
        for match in frozen.findall(source):
            hits.append((name, match))
    assert hits == [], f"frozen subsystem imports present: {hits}"


def test_s4_no_runtime_mutation_paths_across_p3_p5():
    """Aggregate counter: runtime_mutation_paths must be 0."""
    paths = []
    for name in _AUDIT_MODULES:
        source = pathlib.Path(f"anima/{name}.py").read_text(encoding="utf-8")
        for needle in _MUTATION_CALLABLE_NEEDLES:
            if re.search(needle, source):
                paths.append((name, needle))
    assert paths == [], f"runtime mutation paths present: {paths}"
