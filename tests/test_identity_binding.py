"""P9.2 production identity binding tests (RED -> GREEN).

QQ user_id -> permission_level -> capability scope:

  * capability_levels maps a capability to the level it requires
  * an undeclared capability is DENIED (fail closed — not configured = not
    authorized)
  * an unknown user has level 0 -> cannot satisfy any capability
  * admins (admin_level) satisfy any capability up to admin_level
  * qq identity NEVER becomes the execution identity (P8.3/P9.2: no merge,
    request_id is never a substitute for a user identity)
"""

import pathlib

import pytest

from anima.pilot.identity_policy import IdentityPolicyConfig, QQIdentityPolicy


def _policy(**over):
    base = dict(
        enabled=True,
        group_allowlist=("123456",),
        user_levels={"10001": 20, "10002": 80},
        admin_users=("50001",),
        capability_levels={
            "answer.question": 1,
            "sandbox.write": 50,
            "server.manage": 90,
        },
    )
    base.update(over)
    return QQIdentityPolicy(IdentityPolicyConfig(**base))


# ---------------------------------------------------------------------------
# A - capability scope: level gating
# ---------------------------------------------------------------------------


def test_a_capability_level_gating():
    pol = _policy()
    # user level 20: can answer (req 1), cannot sandbox.write (req 50)
    assert pol.can("10001", "answer.question", group_id="123456") is True
    assert pol.can("10001", "sandbox.write", group_id="123456") is False
    # user level 80: sandbox.write ok, server.manage (req 90) denied
    assert pol.can("10002", "sandbox.write", group_id="123456") is True
    assert pol.can("10002", "server.manage", group_id="123456") is False
    # admin level 100: everything up to admin level
    assert pol.can("50001", "server.manage", group_id="123456") is True


def test_b_undeclared_capability_denied():
    pol = _policy()
    # not configured = not authorized (fail closed), even for admins
    assert pol.can("50001", "undeclared.cap", group_id="123456") is False
    assert pol.can("10002", "network.scan", group_id="123456") is False


# ---------------------------------------------------------------------------
# C - unknown user fail closed (level 0)
# ---------------------------------------------------------------------------


def test_c_unknown_user_level_zero():
    pol = _policy()
    assert pol.level_for("999999") == 0
    # level 0 cannot satisfy any capability requiring >= 1
    assert pol.can("999999", "answer.question", group_id="123456") is False
    assert pol.can("999999", "sandbox.write", group_id="123456") is False


def test_d_policy_disabled_denies_everything():
    pol = _policy(enabled=False)
    assert pol.can("50001", "server.manage", group_id="123456") is False
    assert pol.can("10001", "answer.question", group_id="123456") is False


# ---------------------------------------------------------------------------
# E - group scope applies to capability checks too
# ---------------------------------------------------------------------------


def test_e_capability_group_scope():
    pol = _policy()
    # user allowed in group 123456 but group 999999 is not whitelisted
    assert pol.can("10002", "sandbox.write", group_id="123456") is True
    assert pol.can("10002", "sandbox.write", group_id="999999") is False


# ---------------------------------------------------------------------------
# F - identity isolation: qq identity != execution identity
# ---------------------------------------------------------------------------


def test_f_identity_not_merged():
    from anima.pilot.identity_correlator import IdentityCorrelator

    corr = IdentityCorrelator()
    chain = corr.bind(
        group_id="123456", user_id="10001", request_id="req-abc"
    )
    # qq user id is never the execution key / authz id
    assert chain.execution_key is not None
    assert "10001" not in [str(x) for x in chain.execution_key]
    assert chain.authz_id != "10001"
    assert chain.intent_id != "10001"
    assert chain.request_id == "req-abc"


def test_f2_request_id_not_identity():
    from anima.pilot.identity_correlator import IdentityCorrelator

    corr = IdentityCorrelator()
    chain = corr.bind(group_id="123456", user_id="10001", request_id="req-abc")
    # request_id must not be reused as the execution identity component
    assert chain.request_id != chain.authz_id
    assert chain.request_id != chain.execution_key[0]


# ---------------------------------------------------------------------------
# G - static boundary: no System B / gateway / capability activation
# ---------------------------------------------------------------------------


def test_g_static_boundary():
    src = pathlib.Path("anima/pilot/identity_policy.py").read_text(encoding="utf-8")
    parts = src.split('"""')
    body = parts[2] if len(parts) >= 3 else src
    forbidden = (
        "executor", "Executor", "PlanOrchestrator", "pilot_rollout",
        "CapabilityGateway", "gateway", "tools", "approve_and_execute",
        "capability_mode", "webui", "napcat", "subprocess", "os.environ",
        "getenv", "requests", "httpx", "ssh", "paramiko",
    )
    for needle in forbidden:
        assert needle not in body, f"forbidden token: {needle}"
    allowed_prefixes = ("anima.pilot.", "anima.runtime.", "anima.constitution.")
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith(("from ", "import ")) and "anima." in stripped:
            target = stripped.split(" import ")[0].replace("from ", "").strip()
            if target.startswith("anima."):
                assert target.startswith(allowed_prefixes), f"import escapes: {target!r}"
