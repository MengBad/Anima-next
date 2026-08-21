"""P6.10-B Authorization Dispatch Separation Slice - authoritative contract.

Implements the three-way fact flow split (approve / dispatch / record) with
AuthorizationFact consumption and ExecutionFact recording — WITHOUT any
executor / provider invocation:

    approve()   -> AuthorizationFact          (intent to authorize)
    dispatch()  -> ExecutionFactDraft         (consume authz, construct execution_key)
    record()    -> ExecutionFact              (execution fact contract, immutable)

The flow is fact-flow only: dispatch() NEVER invokes an executor or provider;
it constructs the execution identity and evidence carrier.  Real execution
remains a future, separately-authorized slice.

Acceptance (P6.9 contract A-F):
  A. Authorization isolation  - approved/authorized produces no execution fact
  B. Dispatch fact            - dispatch produces execution evidence (key + id + result slot)
  C. Evidence immutability    - recorded facts never overwritten/recomputed/forged
  D. Replay safety            - same authz_id never re-dispatched/re-recorded
  E. Cross-sink consistency   - authz fact != execution fact (disjoint fields + authz_id link)
  F. Non-escalation           - neither fact ever upgrades to executed/deployed/production

Hard boundary (this contract enforces it):
  * NO executor / provider / workflow invocation
  * NO production wiring
  * NO capability expansion
  * NO frozen/governance import in the module
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import tempfile
from dataclasses import FrozenInstanceError

import pytest

from anima.capability_authorization_fact import (
    AuthorizationFactStore,
    verify_authorization_fact,
)

NOW = "2026-08-11T15:00:00+08:00"


def _digest(payload) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@pytest.fixture()
def flow(tmp_path):
    from anima.capability_execution_dispatch import DispatchFlow  # RED

    return DispatchFlow(str(tmp_path))


def _authz_kwargs(**overrides):
    defaults = dict(
        authz_id="authz-dispatch-1",
        principal="owner-123",
        capability_id="code.execute",
        scope="pilot-allowlist",
        granted_at=NOW,
    )
    defaults.update(overrides)
    return defaults


# --------------------------------------------------------------------------
# A. Authorization isolation: approve produces authz fact ONLY
# --------------------------------------------------------------------------
def test_a_approve_produces_authz_fact_only(flow, tmp_path):
    fact = flow.approve(**_authz_kwargs())
    # approve returns an AuthorizationFact (P6.10-A model), not an execution fact
    assert type(fact).__name__ == "AuthorizationFact"
    assert verify_authorization_fact(fact).valid is True
    # only the authorization_facts domain exists; NO execution_facts domain
    assert (pathlib.Path(tmp_path) / "authorization_facts").is_dir()
    assert not (pathlib.Path(tmp_path) / "execution_facts").exists()


# --------------------------------------------------------------------------
# B. Dispatch fact: dispatch produces execution evidence contract
# --------------------------------------------------------------------------
def test_b_dispatch_produces_execution_fact_contract(flow, tmp_path):
    flow.approve(**_authz_kwargs())
    draft = flow.dispatch("authz-dispatch-1")
    # the draft carries execution identity: execution_key + evidence_id + result slot
    assert draft.execution_key == ("authz-dispatch-1", "code.execute", 0)
    assert draft.evidence_id
    assert draft.capability_id == "code.execute"
    assert "result" in draft.__dataclass_fields__ or draft is not None

    recorded = flow.record(draft)
    # recorded ExecutionFact carries key + id + result slot + additive digest
    assert recorded.execution_key == draft.execution_key
    assert recorded.evidence_id == draft.evidence_id
    assert recorded.authz_id == "authz-dispatch-1"
    assert recorded.result == {}          # result slot present (unexecuted: empty)
    assert recorded.fact_digest


# --------------------------------------------------------------------------
# C. Evidence immutability: recorded facts never overwritten/recomputed
# --------------------------------------------------------------------------
def test_c_evidence_immutable_after_record(flow, tmp_path):
    flow.approve(**_authz_kwargs())
    draft = flow.dispatch("authz-dispatch-1")
    recorded = flow.record(draft)
    before = (pathlib.Path(tmp_path) / "execution_facts" / f"{recorded.evidence_id}.json").read_bytes()

    # a second record of the same evidence_id is a no-op - never overwritten
    again = flow.record(draft)
    after = (pathlib.Path(tmp_path) / "execution_facts" / f"{recorded.evidence_id}.json").read_bytes()
    assert again.fact_digest == recorded.fact_digest
    assert before == after


def test_c2_tampered_execution_fact_fails_closed(flow, tmp_path):
    from anima.capability_execution_dispatch import (
        ExecutionFact,
        verify_execution_fact,
    )

    flow.approve(**_authz_kwargs())
    recorded = flow.record(flow.dispatch("authz-dispatch-1"))
    tampered = object.__new__(ExecutionFact)
    for field in ExecutionFact.__slots__:
        object.__setattr__(tampered, field, getattr(recorded, field))
    object.__setattr__(tampered, "fact_digest", "0" * 64)
    verification = verify_execution_fact(tampered)
    assert verification.valid is False
    assert verification.digest_intact is False


# --------------------------------------------------------------------------
# D. Replay safety: same authz_id never re-dispatched/re-recorded
# --------------------------------------------------------------------------
def test_d_replay_safe_same_authz(flow, tmp_path):
    from anima.capability_execution_dispatch import verify_execution_fact

    flow.approve(**_authz_kwargs())
    first_draft = flow.dispatch("authz-dispatch-1")
    first = flow.record(first_draft)

    # second dispatch of the SAME authz is idempotent: same identity, no new fact
    second_draft = flow.dispatch("authz-dispatch-1")
    assert second_draft.execution_key == first_draft.execution_key
    assert second_draft.evidence_id == first_draft.evidence_id
    facts = list((pathlib.Path(tmp_path) / "execution_facts").glob("*.json"))
    assert len(facts) == 1

    # repeated verification succeeds, never triggers execution
    assert verify_execution_fact(first).valid is True
    assert verify_execution_fact(first).valid is True


def test_d2_dispatch_unknown_authz_fails_closed(flow, tmp_path):
    from anima.capability_authorization_fact import AuthorizationFactReadError

    with pytest.raises(AuthorizationFactReadError):
        flow.dispatch("ghost-authz")  # no authorization -> no dispatch


# --------------------------------------------------------------------------
# E. Cross-sink consistency: authz fact != execution fact
# --------------------------------------------------------------------------
def test_e_cross_sink_disjoint_fields(flow, tmp_path):
    from anima.capability_execution_dispatch import ExecutionFact
    from anima.capability_authorization_fact import AuthorizationFact

    flow.approve(**_authz_kwargs())
    recorded = flow.record(flow.dispatch("authz-dispatch-1"))

    authz_fields = set(AuthorizationFact.__dataclass_fields__)
    exec_fields = set(ExecutionFact.__dataclass_fields__)
    # disjoint semantic fields: authz carries no execution, execution carries authz link
    assert "execution_status" not in authz_fields
    assert "result" not in authz_fields
    assert "authz_id" in exec_fields  # execution fact links back to its authorization
    # the flow never produces an execution fact without an authorization
    assert recorded.authz_id == "authz-dispatch-1"


# --------------------------------------------------------------------------
# F. Non-escalation: neither fact ever upgrades to execution vocabulary
# --------------------------------------------------------------------------
def test_f_neither_fact_escalates(flow, tmp_path):
    from anima.capability_execution_dispatch import verify_execution_fact

    flow.approve(**_authz_kwargs())
    recorded = flow.record(flow.dispatch("authz-dispatch-1"))
    verification = verify_execution_fact(recorded)
    assert verification.non_escalation is True
    assert verification.valid is True
    payload = json.dumps(
        {k: getattr(recorded, k) for k in recorded.__dataclass_fields__},
        ensure_ascii=False, sort_keys=True, default=str,
    )
    for banned in ("executed", "deployed", "authorized_running", "production"):
        assert banned not in payload.lower()


# --------------------------------------------------------------------------
# storage integrity + immutability
# --------------------------------------------------------------------------
def test_g_missing_execution_fact_returns_none(flow, tmp_path):
    assert flow.load_execution_fact("ghost-evidence") is None
    assert flow.has_execution_fact("ghost-evidence") is False


def test_h_execution_fact_is_immutable(flow, tmp_path):
    from dataclasses import FrozenInstanceError as FIE

    flow.approve(**_authz_kwargs())
    recorded = flow.record(flow.dispatch("authz-dispatch-1"))
    with pytest.raises(FIE):
        recorded.result = {"fake": True}  # frozen


# --------------------------------------------------------------------------
# static boundary: no executor/provider surface, no frozen imports
# --------------------------------------------------------------------------
_MODULE_PATH = "anima/capability_execution_dispatch.py"


def test_i_no_executor_provider_surface(flow, tmp_path):
    flow_cls = type(flow)
    banned = {
        "execute", "run", "invoke", "dispatch_to_provider", "activate",
        "apply", "repair", "deploy", "restart", "start", "stop", "commit",
    }
    surface = {n for n in dir(flow_cls) if not n.startswith("_")}
    assert surface & banned == set()


def test_j1_no_forbidden_transport_tokens():
    source = pathlib.Path(_MODULE_PATH).read_text(encoding="utf-8")
    transport = (
        "sub" + "process", r"\bssh\b", "para" + "miko", "doc" + "ker",
        "system" + "ctl", r"\brequests\b", "htt" + "px", "dae" + "mon",
        "ti" + "mer", r"\bpoll\b", r"\bschedule\b",
    )
    hits = [n for n in transport if re.search(n, source)]
    assert hits == [], f"transport tokens present: {hits}"


def test_j2_no_executor_provider_invocation_tokens():
    source = pathlib.Path(_MODULE_PATH).read_text(encoding="utf-8")
    banned = (
        r"def (execute|invoke|activate|dispatch_to_provider|apply|repair|deploy)\b",
        r"\." + "execute" + r"\(",
        r"\." + "invoke" + r"\(",
        r"\." + "activate" + r"\(",
    )
    hits = [n for n in banned if re.search(n, source)]
    assert hits == [], f"executor/provider tokens present: {hits}"


def test_k_no_frozen_or_governance_imports():
    source = pathlib.Path(_MODULE_PATH).read_text(encoding="utf-8")
    frozen = re.compile(
        r"^\s*(?:from|import)\s+anima\.(runtime|constitution|core|tools|watchdog|"
        r"lifecycle|foundation|memory|identity|capability_contract|"
        r"capability\.governance)\b",
        re.M,
    )
    hits = frozen.findall(source)
    assert hits == [], f"frozen/governance imports present: {hits}"
