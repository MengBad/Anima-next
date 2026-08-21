"""P9.4 final authorization audit tests (RED -> GREEN).

An AuthorizationAuditLog appends JSONL events for the full authorization
lifecycle: approval created / enabled / disabled / revoked / denied /
expired.  Fail closed: no env bypass, no auto-enable, default OFF.
"""

import json
import pathlib

import pytest

from anima.pilot.authorization import (
    ApprovalRecord,
    AuthorizationAuditLog,
    PilotAuthorization,
)


def _approval(**over):
    base = dict(
        approval_id="apr-1",
        operator="owner",
        enabled_time=1786450000.0,
        expire_time=0.0,
        reason="canary",
        scope_groups=("123456",),
        scope_capabilities=("sandbox.write",),
    )
    base.update(over)
    return ApprovalRecord(**base)


# ---------------------------------------------------------------------------
# A - audit log appends JSONL events
# ---------------------------------------------------------------------------


def test_a_audit_append(tmp_path):
    log = AuthorizationAuditLog(str(tmp_path / "data"))
    log.record("approval_created", approval_id="apr-1", operator="owner",
               scope={"groups": ["123456"]}, reason="canary")
    log.record("enabled", approval_id="apr-1", operator="owner")
    records = log.read()
    assert len(records) == 2
    assert records[0]["event"] == "approval_created"
    assert records[0]["approval_id"] == "apr-1"
    assert records[1]["event"] == "enabled"
    # every event carries a timestamp
    assert records[0]["ts"] > 0
    assert records[1]["ts"] >= records[0]["ts"]


def test_a2_audit_file_append_only(tmp_path):
    log = AuthorizationAuditLog(str(tmp_path / "data"))
    log.record("enabled", approval_id="a")
    log.record("revoked", approval_id="a")
    # appending again must not truncate previous events
    log2 = AuthorizationAuditLog(str(tmp_path / "data"))
    log2.record("disabled", approval_id="a")
    records = log2.read()
    assert [r["event"] for r in records] == ["enabled", "revoked", "disabled"]


# ---------------------------------------------------------------------------
# B - lifecycle actions produce audit events (when wired)
# ---------------------------------------------------------------------------


def test_b_lifecycle_audit(tmp_path):
    log = AuthorizationAuditLog(str(tmp_path / "data"))
    authz = PilotAuthorization(
        approval=_approval(), enable_token="tok-1", ttl_sec=3600.0,
        audit_log=log,
    )
    authz.enable()
    authz.disable()
    authz.enable()
    authz.revoke()
    events = [r["event"] for r in log.read()]
    assert events == ["enabled", "disabled", "enabled", "revoked"]


# ---------------------------------------------------------------------------
# C - denies are audited with their reason
# ---------------------------------------------------------------------------


def test_c_deny_audited(tmp_path):
    log = AuthorizationAuditLog(str(tmp_path / "data"))
    authz = PilotAuthorization(
        approval=_approval(), enable_token="tok-1", ttl_sec=3600.0,
        audit_log=log,
    )
    authz.enable()
    ok, reason = authz.allows(group_id="999999", capability="sandbox.write",
                              token="tok-1")
    assert ok is False and reason == "group_out_of_scope"
    events = log.read()
    deny = [r for r in events if r["event"] == "denied"]
    assert len(deny) == 1
    assert deny[0]["reason"] == "group_out_of_scope"


# ---------------------------------------------------------------------------
# D - default OFF: never enabled implicitly
# ---------------------------------------------------------------------------


def test_d_default_off(tmp_path):
    log = AuthorizationAuditLog(str(tmp_path / "data"))
    authz = PilotAuthorization(
        approval=_approval(), enable_token="tok-1", ttl_sec=3600.0,
        audit_log=log,
    )
    # default: NOT enabled -> allows() denies with "not_enabled"
    ok, reason = authz.allows(group_id="123456", capability="sandbox.write",
                              token="tok-1")
    assert ok is False and reason == "not_enabled"
    # no env / config auto-enable: nothing in the audit except the deny
    events = [r["event"] for r in log.read()]
    assert "enabled" not in events


# ---------------------------------------------------------------------------
# E - clear deletes the audit trail (privacy: records deletable, P8.4)
# ---------------------------------------------------------------------------


def test_e_clear(tmp_path):
    log = AuthorizationAuditLog(str(tmp_path / "data"))
    log.record("enabled", approval_id="a")
    log.record("revoked", approval_id="a")
    assert len(log.read()) == 2
    removed = log.clear()
    assert removed == 2
    assert log.read() == []


# ---------------------------------------------------------------------------
# F - static boundary
# ---------------------------------------------------------------------------


def test_f_static_boundary():
    src = pathlib.Path("anima/pilot/authorization.py").read_text(encoding="utf-8")
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
