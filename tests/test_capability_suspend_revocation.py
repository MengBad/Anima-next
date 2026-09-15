"""Tests for the offline suspend / revocation proof prototype (P3.4).

These tests prove the five required governance properties:
  * suspend / revoke requires authorization
  * digest binding (transitive: revocation -> suspension -> activation)
  * scope binding (synthetic-only, matches the activation)
  * cannot revoke an unrelated capability
  * cannot mutate runtime state
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from anima.capability.activation_contract import (
    CapabilityActivationPlan,
    CapabilityActivationReview,
    OfflineCapabilityActivationPlanner,
    PlanStatus,
    ReviewDecision,
    SyntheticCapabilityProposal,
)
from anima.capability.activation_proof import (
    ActivationRecord,
    OfflineActivationProofBuilder,
)

from anima.capability.suspend_revocation import (  # RED: fails until module exists
    OfflineSuspendRevocationPlanner,
    RevocationRecord,
    SuspendRevocationError,
    SuspendRevocationVerification,
    SuspensionRecord,
    verify_suspend_revocation,
)


NOW = "2026-08-11T03:39:00+08:00"
SYNTHETIC = "synthetic-only"


def _digest(payload) -> str:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def make_activation_record(capability_id: str = "cap-suspend-1", scope: str = SYNTHETIC) -> ActivationRecord:
    proposal = SyntheticCapabilityProposal(
        capability_id=capability_id,
        version="0.1.0",
        provider_ref="synthetic://test/harness",
        contract_digest="a" * 64,
        risk_level="low",
        scope=scope,
        proposed_at=NOW,
    )
    review = CapabilityActivationReview(
        review_id="rev-1",
        proposal_id=capability_id,
        decision=ReviewDecision.APPROVE,
        reviewer="governance-agent",
        evidence_refs=("ev-1",),
        reviewed_at=NOW,
    )
    plan = OfflineCapabilityActivationPlanner().plan(
        plan_id="plan-1",
        proposal_id=capability_id,
        proposal=proposal,
        review=review,
        created_at=NOW,
    )
    return OfflineActivationProofBuilder().record(
        record_id="rec-1",
        plan=plan,
        proposal=proposal,
        review=review,
        requested_scope=scope,
        recorded_at=NOW,
    )


def make_suspension(rec: ActivationRecord) -> SuspensionRecord:
    return OfflineSuspendRevocationPlanner().record_suspension(
        suspension_id="susp-1",
        activation_record=rec,
        authorization="approved-by:operator-1:" + "b" * 64,
        suspended_at=NOW,
    )


def make_revocation(rec: ActivationRecord, susp: SuspensionRecord) -> RevocationRecord:
    return OfflineSuspendRevocationPlanner().record_revocation(
        revocation_id="revk-1",
        activation_record=rec,
        suspension=susp,
        authorization="approved-by:operator-2:" + "c" * 64,
        revoked_at=NOW,
    )


def craft_revocation(base: RevocationRecord, **overrides) -> RevocationRecord:
    """Build a RevocationRecord with overridden fields and a correctly computed
    revocation_digest (unless the digest itself is the override)."""
    fields = {name: getattr(base, name) for name in base.__dataclass_fields__}
    fields.update(overrides)
    if "revocation_digest" not in overrides:
        payload = {
            "revocation_id": fields["revocation_id"],
            "activation_record_id": fields["activation_record_id"],
            "suspension_id": fields["suspension_id"],
            "capability_id": fields["capability_id"],
            "scope": fields["scope"],
            "authorization": fields["authorization"],
            "status": fields["status"],
            "runtime_activation": fields["runtime_activation"],
            "activation_record_digest": fields["activation_record_digest"],
            "suspension_digest": fields["suspension_digest"],
            "revoked_at": fields["revoked_at"],
        }
        fields["revocation_digest"] = _digest(payload)
    return RevocationRecord(**fields)


def test_valid_suspend_revocation_chain_verifies():
    rec = make_activation_record()
    susp = make_suspension(rec)
    rev = make_revocation(rec, susp)
    ver = verify_suspend_revocation(activation_record=rec, suspension=susp, revocation=rev)
    assert ver.valid is True
    assert ver.authorized is True
    assert ver.activation_binding is True
    assert ver.suspension_binding is True
    assert ver.scope_valid is True
    assert ver.no_unrelated_revocation is True
    assert ver.digest_intact is True
    assert ver.no_runtime_activation is True
    assert ver.no_runtime_mutation is True
    assert ver.errors == ()


def test_suspension_record_binds_activation_digest():
    rec = make_activation_record()
    susp = make_suspension(rec)
    assert susp.activation_record_digest == rec.record_digest
    assert susp.capability_id == rec.proposal_id


def test_suspend_requires_authorization():
    rec = make_activation_record()
    with pytest.raises(SuspendRevocationError):
        OfflineSuspendRevocationPlanner().record_suspension(
            suspension_id="susp-x",
            activation_record=rec,
            authorization="",
            suspended_at=NOW,
        )


def test_revoke_requires_authorization():
    rec = make_activation_record()
    susp = make_suspension(rec)
    with pytest.raises(SuspendRevocationError):
        OfflineSuspendRevocationPlanner().record_revocation(
            revocation_id="revk-x",
            activation_record=rec,
            suspension=susp,
            authorization="",
            revoked_at=NOW,
        )


def test_digest_binding_tampering_rejected():
    rec = make_activation_record()
    susp = make_suspension(rec)
    rev = make_revocation(rec, susp)
    tampered = replace(rev, activation_record_digest="f" * 64)
    ver = verify_suspend_revocation(activation_record=rec, suspension=susp, revocation=tampered)
    assert ver.valid is False
    assert ver.digest_intact is False
    assert any("digest" in e for e in ver.errors)


def test_scope_binding_rejected():
    rec = make_activation_record()
    susp = make_suspension(rec)
    rev = make_revocation(rec, susp)
    bad = craft_revocation(rev, scope="production")
    ver = verify_suspend_revocation(activation_record=rec, suspension=susp, revocation=bad)
    assert ver.valid is False
    assert ver.scope_valid is False
    assert any("scope" in e for e in ver.errors)


def test_cannot_revoke_unrelated_capability_verify_level():
    rec = make_activation_record(capability_id="cap-A")
    susp = make_suspension(rec)
    rev = make_revocation(rec, susp)
    bad = craft_revocation(rev, capability_id="cap-B")
    ver = verify_suspend_revocation(activation_record=rec, suspension=susp, revocation=bad)
    assert ver.valid is False
    assert ver.no_unrelated_revocation is False
    assert any("unrelated" in e for e in ver.errors)


def test_cannot_revoke_unrelated_capability_builder_level():
    rec_a = make_activation_record(capability_id="cap-A")
    susp_a = make_suspension(rec_a)
    susp_b = replace(susp_a, capability_id="cap-B")
    with pytest.raises(SuspendRevocationError):
        OfflineSuspendRevocationPlanner().record_revocation(
            revocation_id="revk-x",
            activation_record=rec_a,
            suspension=susp_b,
            authorization="approved-by:operator:" + "c" * 64,
            revoked_at=NOW,
        )


def test_cannot_mutate_runtime_state():
    rec = make_activation_record()
    susp = make_suspension(rec)
    rev = make_revocation(rec, susp)
    assert rec.status == PlanStatus.NOT_ACTIVATED
    assert susp.runtime_activation == "not_activated"
    assert rev.runtime_activation == "not_activated"
    ver = verify_suspend_revocation(activation_record=rec, suspension=susp, revocation=rev)
    assert ver.no_runtime_activation is True
    assert ver.no_runtime_mutation is True


_FORBIDDEN_SOURCE_PATTERNS = [
    r"subprocess",
    r"\bssh\b",
    r"systemctl",
    r"docker",
    r"\brequests\b",
    r"httpx",
    r"aiohttp",
    r"urllib",
    r"http\.client",
    r"importlib",
    r"__import__",
    r"\.activate\(",
    r"\.enable\(",
    r"\.start\(",
    r"\.execute\(",
    r"active\s*=\s*True",
    r"enabled\s*=\s*True",
]


def test_suspend_revocation_source_has_no_executor_or_runtime_calls():
    import re
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    ev = repo_root / "anima/capability/suspend_revocation.py"
    if not ev.exists():
        ev = repo_root / "anima/capability_suspend_revocation.py"
    source = ev.read_text(encoding="utf-8")
    compiled = [re.compile(p) for p in _FORBIDDEN_SOURCE_PATTERNS]
    for pattern in compiled:
        assert not pattern.search(source), f"source contains forbidden pattern {pattern.pattern!r}"
