"""P8.7 — Live Pilot Runtime (RED contract, REAL_USER_CONTROLLED).

End-to-end P8 chain for one real QQ event:
    Adapter -> Gate -> Identity -> Permission -> (F5 execute | answer)
    -> Safety -> Evidence -> reply

Acceptance:
    A  kill switch disabled -> everything denied at first check
    B  non-pilot group -> denied (group_not_allowed), no evidence written
    C  pilot group + triggered + read-only question -> ANSWERED reply
    D  pilot group + triggered + "删除服务器文件" -> DENIED reply
    E  pilot group + triggered + "帮我写个代码" + pilot entry -> EXECUTED
       (sandbox file written) or BLOCKED when entry not wired
    F  evidence record written per admitted conversation; user hashed
    G  PASSIVE: untriggered message in pilot group -> not handled (no reply)
    H  safety filter intercepts a leaky answer before it returns
    I  static boundary: runtime imports only pilot-domain + F5
"""
from __future__ import annotations

import json
import pathlib

from anima.pilot.conversation_evidence import ConversationEvidenceLog
from anima.pilot.entry_gate import GateConfig
from anima.pilot.pilot_runtime import LivePilotConfig, PilotRuntime


def _event(**overrides):
    ev = {
        "topic": "event.message.group",
        "user_id": 10001,
        "group_id": "123456",
        "message_id": "m-1",
        "text": "什么是量子计算？",
        "time": 1786450000,
        "mentions_self": True,
        "raw": {"message_type": "group", "sender": {"nickname": "u1"}},
    }
    ev.update(overrides)
    return ev


def _runtime(tmp_path, **cfg):
    return PilotRuntime(
        config=LivePilotConfig(
            allowed_groups=("123456",),
            allowed_users=(),
            max_groups=3,
            unlimited_users=False,
            **cfg,
        ),
        data_dir=str(tmp_path / "data"),
    )


# ---------------------------------------------------------------------------
# A - kill switch disabled -> everything denied
# ---------------------------------------------------------------------------
def test_a_kill_switch_disabled(tmp_path):
    class _Off:
        def check(self):
            raise RuntimeError("disabled")

    rt = _runtime(tmp_path)
    rt._kill_switch = _Off()
    r = rt.handle(_event())
    assert r.handled is False
    assert r.denied_reason == "kill_switch_disabled"
    assert r.reply == ""


# ---------------------------------------------------------------------------
# B - non-pilot group -> denied, no evidence
# ---------------------------------------------------------------------------
def test_b_non_pilot_group(tmp_path):
    rt = _runtime(tmp_path)
    r = rt.handle(_event(group_id="999999"))
    assert r.handled is False and r.denied_reason == "group_not_allowed"
    assert rt._evidence_log.read() == []


# ---------------------------------------------------------------------------
# C - pilot group + triggered + question -> ANSWERED
# ---------------------------------------------------------------------------
def test_c_answer_question(tmp_path):
    rt = _runtime(tmp_path)
    r = rt.handle(_event())
    # P9.5: ANSWER is handed back to the LLM handler (handled=False,
    # denied_reason=llm_answer); gating + evidence stay in the pilot.
    assert r.handled is False
    assert r.denied_reason == "llm_answer"
    assert r.reply == ""
    ev = rt._evidence_log.read()
    assert len(ev) == 1
    assert ev[0]["result"] == "ANSWERED"


# ---------------------------------------------------------------------------
# D - dangerous request -> DENIED reply + evidence
# ---------------------------------------------------------------------------
def test_d_dangerous_denied(tmp_path):
    rt = _runtime(tmp_path)
    r = rt.handle(_event(text="删除服务器文件"))
    assert r.handled is True
    assert "不在允许范围" in r.reply
    assert rt._evidence_log.read()[0]["result"] == "DENIED"


# ---------------------------------------------------------------------------
# E - explicit write -> BLOCKED (no pre-authorized request id) — fail closed
# ---------------------------------------------------------------------------
def test_e_execute_write(tmp_path):
    rt = _runtime(tmp_path)  # no pilot entry wired, policy disabled (fail closed)
    r = rt.handle(_event(text="帮我写个代码"))
    assert r.handled is True
    # P10.2-C: the capability grant check runs FIRST — a disabled policy
    # denies every execution ("权限检查未通过"), which is the fail-closed gate
    # before the execution-bridge availability check.
    assert ("权限检查未通过" in r.reply) or ("受控试点未授权" in r.reply)
    assert rt._evidence_log.read()[0]["result"] == "BLOCKED"


def test_e2_execute_entry_denies_unpreauthorized(tmp_path):
    """Even with a live F5 entry wired, a random per-request plan id that was
    NOT pre-authorized is denied (fail closed) — exactly the P8.6 rule.
    The subject must hold a sandbox.write grant first (P10.2-C)."""
    from anima.pilot.identity_policy import (
        IdentityPolicyConfig,
        QQIdentityPolicy,
        grant_subject,
    )
    from anima.runtime.pilot_control import BridgeKillSwitch
    from anima.runtime.pilot_rollout import PilotEntry
    from anima.runtime.pilot_scope import PilotEnablementRecord, PilotScope

    scope = PilotScope(
        scope_id="pilot-live-001", enabled=True, mode="CONTROLLED",
        goal_prefixes=("goal#pilot-",), plan_prefixes=("plan#pilot-",),
        allowlist=("plan#pilot-any",), operator_approval_ref="opr-p8-1",
    )
    record = PilotEnablementRecord(
        pilot_enablement_id="pe-live-1", operator="owner",
        scope_digest=scope.scope_digest,
        approved_at="2026-08-11T20:00:00+08:00",
    )
    data_dir = str(tmp_path / "data")
    entry = PilotEntry(scope=scope, record=record,
                       switch=BridgeKillSwitch(enabled=True),
                       executor=None, ledger=None, data_dir=data_dir,
                       gray_percent=100)
    subject = grant_subject("10001")
    policy = QQIdentityPolicy(IdentityPolicyConfig(
        enabled=True, group_allowlist=("123456",),
        user_levels={subject: 100}, admin_users=(subject,),
        capability_levels={"sandbox.write": 50},
    ))
    rt = PilotRuntime(
        config=LivePilotConfig(allowed_groups=("123456",), min_interval_sec=0.0),
        data_dir=data_dir,
        identity_policy=policy,
    )
    rt._pilot_entry = entry
    r = rt.handle(_event(text="帮我写个代码"))
    assert r.handled is True
    # grant passes (subject authorized) -> execution-bridge admit denies
    assert "受控试点未授权" in r.reply
    assert rt._evidence_log.read()[0]["result"] == "BLOCKED"


# ---------------------------------------------------------------------------
# F - evidence: user hashed, record written
# ---------------------------------------------------------------------------
def test_f_evidence_hashed(tmp_path):
    rt = _runtime(tmp_path)
    rt.handle(_event())
    raw = (tmp_path / "data" / "logs" / "conversation_evidence.jsonl") \
        .read_text(encoding="utf-8")
    assert "10001" not in raw  # raw qq id never persisted
    ev = rt._evidence_log.read()
    assert ev[0]["user_id_hash"] != "10001"


# ---------------------------------------------------------------------------
# G - PASSIVE: untriggered -> not handled
# ---------------------------------------------------------------------------
def test_g_passive_untriggered(tmp_path):
    rt = _runtime(tmp_path)
    r = rt.handle(_event(mentions_self=False))
    assert r.handled is False
    assert r.denied_reason == "passive_not_triggered"
    assert rt._evidence_log.read() == []  # nothing recorded for untriggered


# ---------------------------------------------------------------------------
# H - safety filter intercepts leaky content (P8.5 unit contract)
# ---------------------------------------------------------------------------
def test_h_safety_intercept(tmp_path):
    # P9.5: ANSWERS are produced by the host LLM; the pilot still applies the
    # safety filter to ITS OWN generated replies (deny / blocked / execute
    # results).  The filter unit contract is what we pin here.
    from anima.pilot.safety_filter import SafetyFilter

    sf = SafetyFilter()
    safe = sf.filter("内部路径 /opt/anima/data 泄露了")
    assert safe.safe is False
    assert "/opt/anima" not in safe.answer
    # a normal reply passes
    safe2 = sf.filter("该操作不在允许范围内，已拒绝。")
    assert safe2.safe is True


# ---------------------------------------------------------------------------
# I - static boundary
# ---------------------------------------------------------------------------
def test_i_static_boundary():
    src = pathlib.Path("anima/pilot/pilot_runtime.py").read_text(encoding="utf-8")
    parts = src.split('"""')
    code_body = parts[2] if len(parts) >= 3 else src
    forbidden = [
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
