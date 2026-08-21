"""P8.10 — QQ Identity Policy (RED contract).

qq/group/user scopes + allowlist + admin role + denylist + fail closed.

Acceptance:
    A  policy disabled -> DENY (fail closed)
    B  empty group allowlist -> DENY all groups
    C  group not in allowlist -> DENY
    D  group in denylist -> DENY even for admin
    E  user in denylist -> DENY (non-admin)
    F  user in denylist + admin -> ALLOW
    G  user allowlist non-empty, user not listed -> DENY (non-admin)
    H  user allowlist non-empty, user listed -> ALLOW
    I  admin bypasses user allowlist (not listed but admin) -> ALLOW
    J  fully valid non-admin -> ALLOW, role="user"
    K  static boundary: no System B / gateway / executor
"""
from __future__ import annotations

import pathlib

from anima.pilot.identity_policy import (
    ALLOW,
    DENY,
    IdentityPolicyConfig,
    QQIdentityPolicy,
)


def _cfg(**overrides):
    base = dict(
        enabled=True,
        group_allowlist=("123456",),
        group_denylist=(),
        user_allowlist=(),
        user_denylist=(),
        admin_users=(),
    )
    base.update(overrides)
    return IdentityPolicyConfig(**base)


def _pol(**cfg):
    return QQIdentityPolicy(_cfg(**cfg))


def _decide(pol, gid="123456", uid="10001"):
    return pol.decide(group_id=gid, user_id=uid)


# ---------------------------------------------------------------------------
# A - disabled -> DENY
# ---------------------------------------------------------------------------
def test_a_disabled_deny():
    d = _decide(_pol(enabled=False))
    assert d.allowed is False and d.reason == "policy_disabled"


# ---------------------------------------------------------------------------
# B - empty group allowlist -> DENY all
# ---------------------------------------------------------------------------
def test_b_empty_group_allowlist():
    d = _decide(_pol(group_allowlist=()))
    assert d.allowed is False and d.reason == "group_allowlist_empty"


# ---------------------------------------------------------------------------
# C - group not in allowlist -> DENY
# ---------------------------------------------------------------------------
def test_c_group_not_allowed():
    d = _decide(_pol(), gid="999999")
    assert d.allowed is False and d.reason == "group_not_allowed"


# ---------------------------------------------------------------------------
# D - group denylist -> DENY even admin
# ---------------------------------------------------------------------------
def test_d_group_denylist_hard():
    d = _decide(_pol(group_denylist=("999999",), admin_users=("10001",)), gid="999999")
    assert d.allowed is False and d.reason == "group_denylisted"


# ---------------------------------------------------------------------------
# E - user denylist -> DENY (non-admin)
# ---------------------------------------------------------------------------
def test_e_user_denylist():
    d = _decide(_pol(user_denylist=("10001",)))
    assert d.allowed is False and d.reason == "user_denylisted"


# ---------------------------------------------------------------------------
# F - user denylist + admin -> ALLOW
# ---------------------------------------------------------------------------
def test_f_admin_bypass_denylist():
    d = _decide(_pol(user_denylist=("10001",), admin_users=("10001",)))
    assert d.allowed is True and d.role == "admin"


# ---------------------------------------------------------------------------
# G - user allowlist non-empty, user not listed -> DENY
# ---------------------------------------------------------------------------
def test_g_user_allowlist_miss():
    d = _decide(_pol(user_allowlist=("20002",)))
    assert d.allowed is False and d.reason == "user_not_allowed"


# ---------------------------------------------------------------------------
# H - user allowlist, user listed -> ALLOW
# ---------------------------------------------------------------------------
def test_h_user_allowlist_hit():
    d = _decide(_pol(user_allowlist=("10001",)))
    assert d.allowed is True and d.role == "user"


# ---------------------------------------------------------------------------
# I - admin bypasses user allowlist
# ---------------------------------------------------------------------------
def test_i_admin_bypass_allowlist():
    d = _decide(_pol(user_allowlist=("20002",), admin_users=("10001",)))
    assert d.allowed is True and d.role == "admin"


# ---------------------------------------------------------------------------
# J - fully valid non-admin -> ALLOW role=user
# ---------------------------------------------------------------------------
def test_j_valid_user():
    d = _decide(_pol())
    assert d.allowed is True and d.role == "user" and d.reason == "allowed"


# ---------------------------------------------------------------------------
# K - static boundary
# ---------------------------------------------------------------------------
def test_k_static_boundary():
    src = pathlib.Path("anima/pilot/identity_policy.py").read_text(encoding="utf-8")
    parts = src.split('"""')
    code_body = parts[2] if len(parts) >= 3 else src
    forbidden = [
        "executor", "Executor", "PlanOrchestrator", "pilot_rollout",
        "CapabilityGateway", "gateway", "tools", "approve_and_execute",
        "capability_mode", "webui", "napcat", "subprocess", "os.environ",
        "getenv", "requests", "httpx", "ssh", "paramiko",
    ]
    for needle in forbidden:
        assert needle not in code_body, f"forbidden token: {needle}"
    allowed_prefixes = ("anima.pilot.", "anima.runtime.", "anima.constitution.")
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith(("from ", "import ")) and "anima." in stripped:
            target = stripped.split(" import ")[0].replace("from ", "").strip()
            if target.startswith("anima."):
                assert target.startswith(allowed_prefixes), f"import escapes: {target!r}"


# ---------------------------------------------------------------------------
# L - P8.10 permission level: unknown user -> level 0 (fail closed)
# ---------------------------------------------------------------------------
def test_l_unknown_user_level_zero():
    d = _decide(_pol(), uid="99999")  # not listed anywhere
    assert d.allowed is True  # allowed into the group (member), but...
    assert d.level == 0  # ...with the FAIL-CLOSED default permission level
    assert d.role == "user"


# ---------------------------------------------------------------------------
# M - P8.10 explicit user levels
# ---------------------------------------------------------------------------
def test_m_explicit_user_level():
    pol = _pol(user_levels={"10001": 50})
    d = _decide(pol, uid="10001")
    assert d.allowed is True and d.level == 50
    # unknown member keeps 0
    d2 = _decide(pol, uid="20002")
    assert d2.allowed is True and d2.level == 0


# ---------------------------------------------------------------------------
# N - P8.10 admin gets admin_level (100)
# ---------------------------------------------------------------------------
def test_n_admin_level():
    pol = _pol(admin_users=("10001",))
    d = _decide(pol, uid="10001")
    assert d.allowed is True and d.role == "admin" and d.level == 100


# ---------------------------------------------------------------------------
# O - P8.10 ordinary user is limited below admin
# ---------------------------------------------------------------------------
def test_o_ordinary_user_limited():
    pol = _pol(user_levels={"10001": 20}, admin_users=("50001",))
    user = _decide(pol, uid="10001")
    admin = _decide(pol, uid="50001")
    assert user.level == 20 and admin.level == 100
    # a capability requiring level 50: user denied, admin allowed
    assert not (user.level >= 50)  # user (20) below the required 50
    assert admin.level >= 50  # admin (100) above
