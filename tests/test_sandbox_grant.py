"""P10.2-C sandbox.write grant tests (RED -> GREEN).

Permission Controller contract for the 1562290139 grant:
  * subject is NEVER the plaintext qq id — grants bind to the hash
  * capability check runs BEFORE any execution (fail closed)
  * chain: identity -> capability -> scope -> safety -> audit -> execute
  * any step DENY -> BLOCKED, no execution, audited
  * unknown / under-level users DENY; production paths / shell / credential
    are out of scope forever
"""

import json
import pathlib

import pytest

from anima.pilot.conversation_evidence import hash_user_id
from anima.pilot.identity_policy import (
    IdentityPolicyConfig,
    QQIdentityPolicy,
    grant_subject,
)
from anima.pilot.pilot_runtime import LivePilotConfig, PilotRuntime

OWNER_QQ = "1562290139"
OWNER_SUBJECT = grant_subject(OWNER_QQ)  # deterministic digest for grants


def _grant_policy(**over):
    base = dict(
        enabled=True,
        group_allowlist=("123456",),
        user_levels={OWNER_SUBJECT: 100},       # grant bound to hash
        admin_users=(OWNER_SUBJECT,),
        capability_levels={"sandbox.write": 50, "chat.answer": 1},
    )
    base.update(over)
    return QQIdentityPolicy(IdentityPolicyConfig(**base))


def _ev(user_id=OWNER_QQ, text="帮我生成一个配置文件", **over):
    ev = {
        "topic": "event.message.group",
        "user_id": user_id,
        "group_id": "123456",
        "message_id": "m-1",
        "text": text,
        "time": 1786450000,
        "mentions_self": True,
        "raw": {"message_type": "group", "sender": {"nickname": "u"}},
    }
    ev.update(over)
    return ev


class _Entry:
    """F5-style pilot entry stub: admits + records runs (no real FS)."""

    def __init__(self):
        self.runs = 0
        self.admits = True

    def admit(self, goal_id, plan_id):
        return self.admits

    def run(self, *, task_id, request, goal_id, plan_id):
        self.runs += 1
        return f"executed {task_id}"


def _runtime(tmp_path, policy, entry=None, **rt_over):
    return PilotRuntime(
        config=LivePilotConfig(allowed_groups=("123456",), min_interval_sec=0.0),
        data_dir=str(tmp_path / "data"),
        pilot_entry=entry,
        identity_policy=policy,
        metrics_log=str(tmp_path / "data" / "logs" / "pilot_metrics.jsonl"),
        **rt_over,
    )


# ---------------------------------------------------------------------------
# 1 - subject normalization: plaintext qq id NEVER appears in the grant
# ---------------------------------------------------------------------------


def test_subject_normalized():
    cfg = _grant_policy().config
    assert OWNER_QQ not in json.dumps(cfg.user_levels)
    assert OWNER_QQ not in json.dumps(cfg.admin_users)
    assert OWNER_SUBJECT in cfg.user_levels  # grant bound to hash


# ---------------------------------------------------------------------------
# 2 - PASS: authorized subject executes sandbox.write
# ---------------------------------------------------------------------------


def test_authorized_executes(tmp_path):
    entry = _Entry()
    rt = _runtime(tmp_path, _grant_policy(), entry=entry)
    r = rt.handle(_ev())
    assert r.handled is True
    assert entry.runs == 1  # executed
    assert rt._evidence_log.read()[0]["result"] == "EXECUTED"


# ---------------------------------------------------------------------------
# 3 - DENY: unknown user (level 0) -> policy block, no execution
# ---------------------------------------------------------------------------


def test_unknown_user_denied(tmp_path):
    entry = _Entry()
    rt = _runtime(tmp_path, _grant_policy(), entry=entry)
    r = rt.handle(_ev(user_id="99999999"))
    assert r.handled is True
    assert "未授权" in r.reply or "权限" in r.reply
    assert entry.runs == 0  # never executed


# ---------------------------------------------------------------------------
# 4 - DENY: known but under-level user -> no execution
# ---------------------------------------------------------------------------


def test_underlevel_user_denied(tmp_path):
    pol = _grant_policy(user_levels={grant_subject("20002"): 10})
    entry = _Entry()
    rt = _runtime(tmp_path, pol, entry=entry)
    r = rt.handle(_ev(user_id="20002"))
    assert entry.runs == 0  # level 10 < 50 -> DENY


# ---------------------------------------------------------------------------
# 5 - DENY: policy disabled -> fail closed (even the owner)
# ---------------------------------------------------------------------------


def test_policy_disabled_denies_owner(tmp_path):
    pol = _grant_policy(enabled=False)
    entry = _Entry()
    rt = _runtime(tmp_path, pol, entry=entry)
    r = rt.handle(_ev())
    assert entry.runs == 0


# ---------------------------------------------------------------------------
# 6 - DENY: capability undeclared -> fail closed
# ---------------------------------------------------------------------------


def test_undeclared_capability_denies(tmp_path):
    pol = _grant_policy(capability_levels={"chat.answer": 1})  # no sandbox.write
    entry = _Entry()
    rt = _runtime(tmp_path, pol, entry=entry)
    r = rt.handle(_ev())
    assert entry.runs == 0  # sandbox.write undeclared -> not authorized


# ---------------------------------------------------------------------------
# 7 - DENY: dangerous verb never reaches the grant (classify first)
# ---------------------------------------------------------------------------


def test_dangerous_deny_first(tmp_path):
    entry = _Entry()
    rt = _runtime(tmp_path, _grant_policy(), entry=entry)
    r = rt.handle(_ev(text="删除服务器文件"))
    assert r.handled is True
    assert "不在允许范围" in r.reply  # ACTION_DENY before capability check
    assert entry.runs == 0


# ---------------------------------------------------------------------------
# 8 - audit: events written with subject hash only (no plaintext)
# ---------------------------------------------------------------------------


def test_audit_events(tmp_path):
    entry = _Entry()
    rt = _runtime(tmp_path, _grant_policy(), entry=entry)
    rt.handle(_ev())  # allowed
    rt.handle(_ev(user_id="99999999"))  # denied (unknown)
    rt.handle(_ev(text="删除服务器文件"))  # denied (dangerous)
    p = pathlib.Path(tmp_path / "data" / "logs" / "sandbox_write_audit.jsonl")
    assert p.exists()
    lines = [json.loads(l) for l in p.read_text(encoding="utf-8").strip().splitlines()]
    assert any(l["event"] == "sandbox_write_allowed" for l in lines)
    # unknown user -> policy block (capability check fail); dangerous verb is
    # denied BEFORE the grant chain (classify) so it yields no grant event
    assert any(l["event"] == "sandbox_write_policy_block" for l in lines)
    assert any(l["event"] == "sandbox_write_requested" for l in lines)
    raw = p.read_text(encoding="utf-8")
    assert OWNER_QQ not in raw  # no plaintext qq in audit


# ---------------------------------------------------------------------------
# 9 - kill switch still wins over the grant
# ---------------------------------------------------------------------------


def test_kill_switch_wins(tmp_path):
    entry = _Entry()
    rt = _runtime(tmp_path, _grant_policy(), entry=entry)
    rt._kill_switch = type("_Off", (), {"check": lambda self: (_ for _ in ()).throw(RuntimeError("off"))})()
    r = rt.handle(_ev())
    assert r.handled is False
    assert r.denied_reason == "kill_switch_disabled"
    assert entry.runs == 0


# ---------------------------------------------------------------------------
# 10 - runtime wiring: coordinator built with identity_policy from config
# ---------------------------------------------------------------------------
class _WCore:
    pilot_coordinator = None
    gateway = None

    class _Bus:
        def __init__(self):
            self.subs = []

        def subscribe(self, topic, fn):
            self.subs.append((topic, fn))

    def __init__(self):
        self.bus = self._Bus()


def test_runtime_wiring_with_policy(tmp_path):
    from anima.pilot.wiring import build_pilot_coordinator

    core = _WCore()
    coord = build_pilot_coordinator(
        config=LivePilotConfig(allowed_groups=("123456",), min_interval_sec=0.0),
        data_dir=str(tmp_path / "data"),
        core=core,
        identity_policy=_grant_policy(),
    )
    # the runtime's policy is the one we injected
    assert coord._runtime._policy.config.user_levels.get(OWNER_SUBJECT) == 100
    # granted subject passes the capability check through the runtime
    assert coord._runtime._policy.can(
        OWNER_SUBJECT, "sandbox.write", group_id="123456") is True


def test_runtime_wiring_default_fail_closed(tmp_path):
    from anima.pilot.wiring import build_pilot_coordinator

    core = _WCore()
    coord = build_pilot_coordinator(
        config=LivePilotConfig(allowed_groups=("123456",), min_interval_sec=0.0),
        data_dir=str(tmp_path / "data"),
        core=core,
        identity_policy=None,  # absent config section -> fail closed
    )
    assert coord._runtime._policy.config.enabled is False
    assert coord._runtime._policy.can(
        OWNER_SUBJECT, "sandbox.write", group_id="123456") is False


def test_runtime_execute_with_bridge_off(tmp_path):
    """grant passes at runtime but execution bridge is OFF -> denied, audited."""
    entry = None  # bridge OFF (pilot_entry absent)
    rt = _runtime(tmp_path, _grant_policy(), entry=entry)
    r = rt.handle(_ev())
    assert r.handled is True
    assert "执行" in r.reply  # bridge-not-available message
    assert rt._evidence_log.read()[0]["result"] == "BLOCKED"
    p = pathlib.Path(tmp_path / "data" / "logs" / "sandbox_write_audit.jsonl")
    lines = [json.loads(l) for l in p.read_text(encoding="utf-8").strip().splitlines()]
    # requested -> capability passed -> bridge denied
    assert any(l["event"] == "sandbox_write_requested" for l in lines)
    assert any(l["event"] == "sandbox_write_denied" for l in lines)
