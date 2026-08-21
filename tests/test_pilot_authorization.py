"""P8.12 — Final Authorization System (RED contract).

approval record + enable token + expiry + scope + revoke.

Acceptance:
    A  allows() requires a token (None -> token_required)
    B  wrong token -> bad_token (fail closed)
    C  correct token + group in scope + capability in scope -> allowed
    D  group out of scope -> group_out_of_scope
    E  capability out of scope -> capability_out_of_scope
    F  expiry: past TTL -> expired (fail closed)
    G  revoke: after revoke -> revoked (fail closed, immediate)
    H  store persists approval record + token digest (never the raw token)
    I  new_enable_token / token_digest are one-way and distinct
    J  static boundary: no System B / gateway / executor
"""
from __future__ import annotations

import pathlib

from anima.pilot.authorization import (
    ApprovalRecord,
    AuthorizationStore,
    PilotAuthorization,
    new_enable_token,
    token_digest,
)


def _approval(**overrides):
    base = dict(
        approval_id="ap-1",
        owner="owner",
        approved_at=1786450000.0,
        reason="p8 single-group pilot",
        scope_groups=("123456",),
        scope_capabilities=("sandbox.write",),
    )
    base.update(overrides)
    return ApprovalRecord(**base)


def _authz(**overrides):
    base = dict(
        approval=_approval(),
        enable_token="tok-secret-1",
        ttl_sec=3600.0,
    )
    base.update(overrides)
    a = PilotAuthorization(**base)
    a.enable()  # P8.12: explicit enable (default OFF)
    return a


# ---------------------------------------------------------------------------
# A - token required
# ---------------------------------------------------------------------------
def test_a_token_required():
    ok, reason = _authz().allows(group_id="123456", capability="sandbox.write")
    assert ok is False and reason == "token_required"


# ---------------------------------------------------------------------------
# B - bad token
# ---------------------------------------------------------------------------
def test_b_bad_token():
    ok, reason = _authz().allows(group_id="123456", capability="sandbox.write",
                                 token="wrong-token")
    assert ok is False and reason == "bad_token"


# ---------------------------------------------------------------------------
# C - valid full allow
# ---------------------------------------------------------------------------
def test_c_allowed():
    ok, reason = _authz().allows(group_id="123456", capability="sandbox.write",
                                 token="tok-secret-1")
    assert ok is True and reason == "allowed"


# ---------------------------------------------------------------------------
# D - group out of scope
# ---------------------------------------------------------------------------
def test_d_group_out_of_scope():
    ok, reason = _authz().allows(group_id="999999", capability="sandbox.write",
                                 token="tok-secret-1")
    assert ok is False and reason == "group_out_of_scope"


# ---------------------------------------------------------------------------
# E - capability out of scope
# ---------------------------------------------------------------------------
def test_e_capability_out_of_scope():
    ok, reason = _authz().allows(group_id="123456", capability="network.fetch",
                                 token="tok-secret-1")
    assert ok is False and reason == "capability_out_of_scope"


# ---------------------------------------------------------------------------
# F - expiry
# ---------------------------------------------------------------------------
def test_f_expiry():
    a = _authz(ttl_sec=10.0)
    import time as _t

    base = _t.time()
    ok, _ = a.allows(group_id="123456", capability="sandbox.write",
                     token="tok-secret-1", now=base + 5.0)
    assert ok is True
    ok2, reason = a.allows(group_id="123456", capability="sandbox.write",
                           token="tok-secret-1", now=base + 9999.0)
    assert ok2 is False and reason == "expired"


# ---------------------------------------------------------------------------
# G - revoke
# ---------------------------------------------------------------------------
def test_g_revoke():
    a = _authz()
    a.revoke()
    ok, reason = a.allows(group_id="123456", capability="sandbox.write",
                          token="tok-secret-1")
    assert ok is False and reason == "revoked"


# ---------------------------------------------------------------------------
# H - store persists record + digest (never raw token)
# ---------------------------------------------------------------------------
def test_h_store(tmp_path):
    a = _authz()
    store = AuthorizationStore(str(tmp_path / "data"))
    path = store.save(a)
    assert path.exists()
    raw = path.read_text(encoding="utf-8")
    assert "tok-secret-1" not in raw  # raw token never persisted
    records = store.list_records()
    assert len(records) == 1
    assert records[0]["token_digest"] == token_digest("tok-secret-1")
    assert store.clear() == 1


# ---------------------------------------------------------------------------
# I - token one-way, distinct
# ---------------------------------------------------------------------------
def test_i_token_digest():
    t = new_enable_token()
    assert len(t) >= 16
    assert token_digest(t) != t  # one-way
    assert token_digest("a") != token_digest("b")


# ---------------------------------------------------------------------------
# J - static boundary
# ---------------------------------------------------------------------------
def test_j_static_boundary():
    src = pathlib.Path("anima/pilot/authorization.py").read_text(encoding="utf-8")
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
# K - P8.12 default OFF: not enabled -> denied
# ---------------------------------------------------------------------------
def test_k_default_off():
    a = PilotAuthorization(approval=_approval(), enable_token="t", ttl_sec=3600.0)
    ok, reason = a.allows(group_id="123456", capability="sandbox.write", token="t")
    assert ok is False and reason == "not_enabled"


# ---------------------------------------------------------------------------
# L - P8.12 enable / disable explicit
# ---------------------------------------------------------------------------
def test_l_enable_disable():
    a = PilotAuthorization(approval=_approval(), enable_token="t", ttl_sec=3600.0)
    assert a.is_enabled() is False
    a.enable()
    assert a.is_enabled() is True
    assert a.allows(group_id="123456", capability="sandbox.write", token="t")[0] is True
    a.disable()
    assert a.is_enabled() is False
    assert a.allows(group_id="123456", capability="sandbox.write", token="t")[0] is False


# ---------------------------------------------------------------------------
# M - P8.12 revoke prevents re-enable
# ---------------------------------------------------------------------------
def test_m_revoke_prevents_enable():
    a = PilotAuthorization(approval=_approval(), enable_token="t", ttl_sec=3600.0)
    a.revoke()
    assert a.is_revoked() is True
    import pytest as _pytest

    with _pytest.raises(RuntimeError):
        a.enable()


# ---------------------------------------------------------------------------
# N - P8.12 approval fields align with the contract
# ---------------------------------------------------------------------------
def test_n_approval_fields():
    rec = _approval()
    d = rec.to_dict()
    for key in ("approval_id", "operator", "enabled_time", "expire_time",
                "reason", "scope_groups", "scope_capabilities"):
        assert key in d
    # compatibility aliases
    assert rec.owner == rec.operator
    # enabled_time is the canonical field; approved_at may carry its own value
    assert rec.approved_at == 1786450000.0  # set explicitly by _approval
    assert rec.enabled_time == 0.0  # default (caller may set via contract)


# ---------------------------------------------------------------------------
# O - P8.12 approval.expire_time gates expiry
# ---------------------------------------------------------------------------
def test_o_approval_expire_time():
    import time as _t

    rec = _approval(expire_time=_t.time() + 100.0)
    a = PilotAuthorization(approval=rec, enable_token="t")
    a.enable()
    assert a.allows(group_id="123456", capability="sandbox.write", token="t")[0] is True
    # past the approval expire_time -> expired
    a2 = PilotAuthorization(approval=_approval(expire_time=_t.time() - 1.0),
                            enable_token="t")
    a2.enable()
    ok, reason = a2.allows(group_id="123456", capability="sandbox.write", token="t")
    assert ok is False and reason == "expired"
