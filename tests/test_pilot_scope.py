"""P7.14 — Controlled Pilot Implementation (RED contract).

P7.13 control model: PilotScope + CohortMatcher + PilotEnablementRecord +
kill-switch-integrated pilot gate.  Pilot authorization is NEVER execution
authorization; the F5 identity chain (intent/authz/execution_key) is untouched.

Acceptance:
    A  Scope match      - correct goal/plan prefix + allowlist enters
    B  Default deny     - empty/absent scope rejects everything
    C  Wildcard deny    - "*" prefixes are rejected at construction
    D  Approval binding - no record / digest mismatch rejects
    E  Kill switch      - DISABLED rejects even when scope matches
    F  Identity isolation- scope/record carry no intent/authz/execution_key
    G  Legacy           - pilot_scope never touches the legacy orchestrator path
    H  Static boundary  - no System B / gateway / capability_mode / env
"""
from __future__ import annotations

import pathlib
import re

import pytest

from anima.runtime.pilot_control import BridgeDisabledError, BridgeKillSwitch
from anima.runtime.pilot_scope import (
    CohortMatcher,
    PilotEnablementRecord,
    PilotScope,
    pilot_gate,
)


def _scope(**overrides):
    defaults = dict(
        scope_id="pilot-scope-1",
        enabled=True,
        mode="CONTROLLED",
        goal_prefixes=("goal#pilot-a",),
        plan_prefixes=("plan#pilot-a",),
        allowlist=("plan#pilot-a-001",),
        operator_approval_ref="opr-approval-001",
    )
    defaults.update(overrides)
    return PilotScope(**defaults)


def _record(scope, **overrides):
    defaults = dict(
        pilot_enablement_id="pe-001",
        operator="owner",
        scope_digest=scope.scope_digest,
        approved_at="2026-08-11T18:00:00+00:00",
    )
    defaults.update(overrides)
    return PilotEnablementRecord(**defaults)


def _switch(enabled=True):
    return BridgeKillSwitch(enabled=enabled)


# ---------------------------------------------------------------------------
# A - Scope match: correct cohort enters
# ---------------------------------------------------------------------------
def test_a_scope_match():
    scope = _scope()
    record = _record(scope)
    switch = _switch()
    assert pilot_gate(scope, record, switch, goal_id="goal#pilot-a-1", plan_id="plan#pilot-a-001")


# ---------------------------------------------------------------------------
# B - Default deny: empty/absent scope rejects
# ---------------------------------------------------------------------------
def test_b_default_deny():
    # absent scope
    assert pilot_gate(None, None, _switch(), goal_id="goal#x", plan_id="plan#x") is False
    # disabled scope
    disabled = _scope(enabled=False)
    assert pilot_gate(disabled, _record(disabled), _switch(), goal_id="goal#pilot-a-1", plan_id="plan#pilot-a-001") is False
    # empty prefixes + empty allowlist -> nothing matches
    empty = _scope(goal_prefixes=(), plan_prefixes=(), allowlist=())
    assert pilot_gate(empty, _record(empty), _switch(), goal_id="goal#anything", plan_id="plan#anything") is False


# ---------------------------------------------------------------------------
# C - Wildcard deny: "*" rejected at construction (no global enable)
# ---------------------------------------------------------------------------
def test_c_wildcard_deny():
    for bad in ("*", "goal#*", "plan#*"):
        gp = (bad,) if (bad == "*" or bad.startswith("goal")) else ("goal#x",)
        pp = (bad,) if (bad == "*" or bad.startswith("plan")) else ("plan#x",)
        with pytest.raises(ValueError):
            _scope(goal_prefixes=gp, plan_prefixes=pp)
    # a "*" in the allowlist is also rejected
    with pytest.raises(ValueError):
        _scope(allowlist=("*",))


# ---------------------------------------------------------------------------
# D - Approval binding: no record / digest mismatch rejects
# ---------------------------------------------------------------------------
def test_d_approval_binding():
    scope = _scope()
    # no record at all
    assert pilot_gate(scope, None, _switch(), goal_id="goal#pilot-a-1", plan_id="plan#pilot-a-001") is False
    # record bound to a different scope (digest mismatch)
    other = _scope(scope_id="other")
    assert pilot_gate(scope, _record(other), _switch(), goal_id="goal#pilot-a-1", plan_id="plan#pilot-a-001") is False


# ---------------------------------------------------------------------------
# E - Kill switch: DISABLED rejects even when scope matches
# ---------------------------------------------------------------------------
def test_e_kill_switch_rejects():
    scope = _scope()
    record = _record(scope)
    switch = _switch(enabled=False)
    assert pilot_gate(scope, record, switch, goal_id="goal#pilot-a-1", plan_id="plan#pilot-a-001") is False


# ---------------------------------------------------------------------------
# F - Identity isolation: scope/record carry no F5 identity vocabulary
# ---------------------------------------------------------------------------
def test_f_identity_isolation():
    scope = _scope()
    record = _record(scope)
    for obj in (scope, record):
        slots = set(getattr(obj, "__slots__", ()))
        assert slots.isdisjoint({"intent_id", "authz_id", "execution_key", "evidence_id"})
    # record vocabulary is pilot-enablement, never authz
    assert "pilot_enablement_id" in record.__slots__
    assert "authz_id" not in record.__slots__


# ---------------------------------------------------------------------------
# G - Legacy: pilot_scope never touches the legacy orchestrator path
# ---------------------------------------------------------------------------
def test_g_legacy_unchanged():
    from anima.constitution.execution import ExecutionRuntime
    from anima.runtime.orchestrator import PlanOrchestrator

    legacy = PlanOrchestrator(execution=ExecutionRuntime())
    assert legacy._executor is None  # legacy fallback intact
    # pilot_scope module itself imports no orchestrator
    src = pathlib.Path("anima/runtime/pilot_scope.py").read_text(encoding="utf-8")
    assert "PlanOrchestrator" not in src
    assert "run_plan" not in src


# ---------------------------------------------------------------------------
# H - Static boundary: no System B / gateway / capability_mode / env
# ---------------------------------------------------------------------------
def test_h_static_boundary():
    src = pathlib.Path("anima/runtime/pilot_scope.py").read_text(encoding="utf-8")
    parts = src.split('"""')
    code_body = parts[2] if len(parts) >= 3 else src
    forbidden = [
        "CapabilityGateway", "gateway", "tools", "approve_and_execute",
        "capability_mode", "webui", "napcat", r"\brequests\b", "httpx",
        "subprocess", r"\bssh\b", "paramiko", "os.environ", "getenv",
    ]
    for needle in forbidden:
        assert not re.search(needle, code_body), f"forbidden token: {needle}"
    allowed_prefixes = ("anima.runtime.", "anima.constitution.", "anima.core.persistence")
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith(("from ", "import ")):
            target = stripped.split(" import ")[0].replace("from ", "").strip()
            if target.startswith("anima."):
                assert target.startswith(allowed_prefixes), f"import escapes: {target!r}"
