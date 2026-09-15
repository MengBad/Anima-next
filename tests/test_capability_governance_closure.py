"""P4.4 Cross-Slice Governance Closure / Consistency Audit.

This is a READ-ONLY verification harness.  It does NOT introduce any new
capability object, executor, persistence or runtime bridge.  It imports the
already-implemented P3.1-P3.4 and P4.1-P4.3 artifacts and asserts the
consistency guarantees that the governance closure depends on:

* transitive digest binding across each chain (tamper-evident);
* no runtime / executor / forbidden-module entry points;
* all terminal states are irreversible;
* approval is never execution (execution_status / runtime_activation frozen);
* Foundation / Lifecycle / CapabilityContract / Memory / Identity are untouched.

Every assertion here is evidence for ``P4.4_GOVERNANCE_CLOSURE_AUDIT.md``.

P4 evolution-chain slice retired 2026-09-12 together with ``evolution_contract``
(archived at ``anima/capability/_archive/`` with its dedicated tests); the
activation-chain guarantees below cover the modules that stay in production.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from anima.capability.activation_contract import (
    CapabilityActivationPlan,
    CapabilityActivationReview,
    OfflineCapabilityActivationPlanner,
    PlanStatus,
    ReviewDecision,
    SyntheticCapabilityProposal,
    verify_activation_plan,
)
from anima.capability.activation_proof import (
    ActivationProof,
    ActivationRecord,
    OfflineActivationProofBuilder,
    verify_activation_proof,
)
from anima.capability.staged_activation import (
    OfflineStagedActivationPlanner,
    StageStatus,
    StagedActivationRecord,
    verify_staged_activation,
)
from anima.capability.suspend_revocation import (
    OfflineSuspendRevocationPlanner,
    SuspensionRecord,
    verify_suspend_revocation,
)

NOW = "2026-08-11T00:00:00+08:00"
HEX64 = "a" * 64

# --- chain builders (use only the existing planners; synthetic-only) ----------

def _build_capability_chain():
    planner = OfflineCapabilityActivationPlanner()
    proposal = SyntheticCapabilityProposal(
        capability_id="cap-x-1", version="0.1.0",
        provider_ref="synthetic://mock/cap-x", contract_digest=HEX64,
        risk_level="low", scope="synthetic-only", proposed_at=NOW,
    )
    review = CapabilityActivationReview(
        review_id="rev-x-1", proposal_id="cap-x-1",
        decision=ReviewDecision.APPROVE, reviewer="auditor-1",
        evidence_refs=("ev-1",), reviewed_at=NOW,
    )
    plan = planner.plan(
        plan_id="plan-x-1", proposal_id="cap-x-1",
        proposal=proposal, review=review, created_at=NOW,
    )
    builder = OfflineActivationProofBuilder()
    record = builder.record(
        record_id="rec-x-1", plan=plan, proposal=proposal, review=review,
        requested_scope="synthetic-only", recorded_at=NOW,
    )
    proof = builder.proof(
        proof_id="pf-x-1", record=record, result="ok",
        evidence_refs=("ev-1",), created_at=NOW,
    )
    return proposal, review, plan, builder, record, proof


# --- static boundary scan ------------------------------------------------

_ALL_MODULE_NAMES = [
    "capability_activation_contract",
    "capability_activation_proof",
    "capability_staged_activation",
    "capability_suspend_revocation",
]


def _real_source(name: str) -> Path:
    """折叠过的模块其实现已移入 anima/capability/, 治理扫描须指向单源。"""
    canon = Path(f"anima/capability/{name.removeprefix('capability_')}.py")
    return canon if canon.exists() else Path(f"anima/{name}.py")


ALL_MODULES = [_real_source(n) for n in _ALL_MODULE_NAMES]
_RUNTIME_PAT = [
    r"subprocess", r"\bssh\b", r"systemctl", r"docker", r"\brequests\b",
    r"httpx", r"aiohttp", r"urllib", r"http\.client", r"importlib",
    r"__import__", r"\.activate\(", r"\.enable\(", r"\.start\(",
    r"\.execute\(", r"\.apply\(", r"\.mutate\(", r"\.run\(",
    r"active\s*=\s*True", r"enabled\s*=\s*True",
]
_FROZEN_IMPORT = re.compile(
    r"^\s*(?:from|import)\s+anima\.(?!capability_(?:activation_contract|"
    r"activation_proof|staged_activation|suspend_revocation))"
    r"(lifecycle|constitution|foundation|memory|identity|runtime|capability_contract)\b",
    re.M,
)


def test_all_capability_modules_free_of_runtime_patterns():
    compiled = [re.compile(p) for p in _RUNTIME_PAT]
    for m in ALL_MODULES:
        src = Path(m).read_text(encoding="utf-8")
        hits = [p.pattern for p in compiled if p.search(src)]
        assert not hits, f"{m} contains forbidden runtime pattern(s): {hits}"


def test_no_frozen_module_imports():
    for m in ALL_MODULES:
        src = Path(m).read_text(encoding="utf-8")
        frozen = _FROZEN_IMPORT.findall(src)
        assert not frozen, f"{m} imports frozen module(s): {frozen}"


# --- structural guarantees ------------------------------------------------

_RECORD_CLASSES = [
    SyntheticCapabilityProposal, CapabilityActivationReview, CapabilityActivationPlan,
    ActivationRecord, ActivationProof, StagedActivationRecord,
    SuspensionRecord,
]


def test_all_record_dataclasses_are_frozen_and_slotted():
    for cls in _RECORD_CLASSES:
        params = getattr(cls, "__dataclass_params__", None)
        assert params is not None, f"{cls.__name__} is not a dataclass"
        assert params.frozen, f"{cls.__name__} must be frozen"
        assert params.slots, f"{cls.__name__} must use slots"


def test_no_executor_methods_on_any_record():
    forbidden_methods = {
        "activate", "enable", "start", "execute", "apply", "mutate", "run",
    }
    for cls in _RECORD_CLASSES:
        present = forbidden_methods & set(dir(cls))
        assert not present, f"{cls.__name__} exposes executor method(s): {present}"


# --- capability chain digest binding (Q2, P3 side) --------------------------

def test_capability_activation_chain_digest_binding_intact():
    proposal, review, plan, builder, record, proof = _build_capability_chain()
    v_plan = verify_activation_plan(plan, proposal=proposal, review=review)
    assert v_plan.valid and v_plan.proposal_binding and v_plan.review_binding
    v_proof = verify_activation_proof(plan=plan, record=record, proof=proof, proposal=proposal)
    assert v_proof.valid and v_proof.plan_binding and v_proof.record_binding
    assert v_proof.no_runtime_activation


def test_capability_staged_activation_binds_and_stays_inert():
    proposal, review, plan, builder, record, proof = _build_capability_chain()
    planner = OfflineStagedActivationPlanner()
    staged = planner.stage(
        stage_id="stg-1", plan=plan, record=record, proof=proof,
        proposal=proposal, gate_refs=("gate-x-1",), staged_at=NOW,
    )
    v = verify_staged_activation(
        staged=staged, plan=plan, record=record, proof=proof, proposal=proposal,
    )
    assert v.valid and v.proof_valid and v.gate_valid and v.no_runtime_activation
    assert staged.runtime_activation == "not_activated"
    assert staged.stage == StageStatus.STAGED


# --- suspend / revocation chain (Q2, P3.4 side) -----------------------------

def test_suspend_revocation_chain_intact_and_not_executed():
    proposal, review, plan, builder, record, proof = _build_capability_chain()
    sr = OfflineSuspendRevocationPlanner()
    suspension = sr.record_suspension(
        suspension_id="susp-1", activation_record=record,
        authorization="auth-1", suspended_at=NOW,
    )
    revocation = sr.record_revocation(
        revocation_id="revk-1", activation_record=record,
        suspension=suspension, authorization="auth-2", revoked_at=NOW,
    )
    v = verify_suspend_revocation(
        activation_record=record, suspension=suspension, revocation=revocation,
    )
    assert v.valid and v.authorized and v.digest_intact
    assert v.no_runtime_activation and v.no_runtime_mutation
    assert v.no_unrelated_revocation


# --- execution invariants (Q5) ---------------------------------------------

def test_approval_is_never_execution():
    # capability side
    proposal, review, plan, builder, record, proof = _build_capability_chain()
    assert record.status == PlanStatus.NOT_ACTIVATED


# --- terminal-state irreversibility across slices (Q4) ----------------------

def test_capability_records_have_no_mutation_api():
    # Frozen dataclasses expose no surrogate mutators; status is pinned.
    proposal, review, plan, builder, record, proof = _build_capability_chain()
    assert record.status == "not_activated"
    for cls in (SyntheticCapabilityProposal, ActivationRecord, StagedActivationRecord):
        executor_like = {"activate", "enable", "start", "execute", "apply", "mutate", "run"} & set(dir(cls))
        assert not executor_like, f"{cls.__name__} exposes {executor_like}"
