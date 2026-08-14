"""P10.4-R3.5 — G6 Kill Switch Hardening (RED contract).

Closes governance gap G6: the config ``kill_switch`` field (currently ``null``
in production) must become an auditable, fail-closed emergency circuit breaker.
When ``kill_switch=true`` EVERY execution capability (sandbox.write) is denied
fail-closed; ``chat.answer`` (read-only) is unaffected; the denial is recorded
in the durable audit sink with ``denied_reason="kill_switch"`` and only the
hashed subject (never plaintext qq / secret / content).

This test MUST fail before the R3.5 change in pilot_runtime lands, then pass
after IMPLEMENT.

Scope guardrails (unchanged by R3.5):
  * capability set is untouched
  * grant / identity policy is untouched
  * execution bridge (PilotEntry / BridgeKillSwitch) is untouched
  * sandbox permissions / provider are untouched
Only the config-driven kill switch + its audit entry are exercised here.
"""

import json
import pathlib

import pytest

from anima.pilot.identity_policy import (
    IdentityPolicyConfig,
    QQIdentityPolicy,
    grant_subject,
)
from anima.pilot.pilot_runtime import LivePilotConfig, PilotRuntime

OWNER_QQ = "1562290139"
OWNER_SUBJECT = grant_subject(OWNER_QQ)  # deterministic digest (64-hex)


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


def _ev(user_id=OWNER_QQ, text="帮我生成一份配置文件：name=anima", **over):
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


def _runtime(tmp_path, policy, *, kill_switch=False, entry=None):
    return PilotRuntime(
        config=LivePilotConfig(
            allowed_groups=("123456",), min_interval_sec=0.0, kill_switch=kill_switch
        ),
        data_dir=str(tmp_path / "data"),
        pilot_entry=entry,
        identity_policy=policy,
        metrics_log=str(tmp_path / "data" / "logs" / "pilot_metrics.jsonl"),
    )


def _audit_lines(tmp_path):
    p = pathlib.Path(tmp_path / "data" / "logs" / "sandbox_write_audit.jsonl")
    assert p.exists(), "audit sink must exist after a handled event"
    return [json.loads(l) for l in p.read_text(encoding="utf-8").strip().splitlines()]


# ---------------------------------------------------------------------------
# R3.5 — RED #1: kill_switch=true must fail-closed deny sandbox.write.
# ---------------------------------------------------------------------------


def test_r3_5_kill_switch_on_denies_sandbox_write(tmp_path):
    entry = _Entry()
    rt = _runtime(tmp_path, _grant_policy(), kill_switch=True, entry=entry)
    res = rt.handle(_ev(text="帮我生成一份配置文件：name=anima"))  # owner + granted

    # fail-closed: execution denied even though the owner is granted
    assert res.handled is False
    assert res.denied_reason == "kill_switch"

    lines = _audit_lines(tmp_path)
    denied = [l for l in lines if l["event"] == "kill_switch_deny"]
    assert denied, "kill_switch_deny audit event must be present when switch is ON"
    ev = denied[0]

    # auditable, fail-closed record — only safe fields
    assert ev["subject_hash"] == OWNER_SUBJECT
    assert ev["capability"] == "sandbox.write"
    assert ev["decision"] == "DENY"
    assert ev["denied_reason"] == "kill_switch"

    # the provider was never actually invoked (fail closed)
    assert entry.runs == 0

    # no plaintext leak
    raw = pathlib.Path(tmp_path / "data" / "logs" / "sandbox_write_audit.jsonl").read_text(
        encoding="utf-8"
    )
    assert OWNER_QQ not in raw
    assert "name=anima" not in raw


# ---------------------------------------------------------------------------
# R3.5 — RED #2: kill_switch=false must preserve current behavior (granted
#         sandbox.write still executes).
# ---------------------------------------------------------------------------


def test_r3_5_kill_switch_off_behaves_unchanged(tmp_path):
    entry = _Entry()
    rt = _runtime(tmp_path, _grant_policy(), kill_switch=False, entry=entry)
    res = rt.handle(_ev(text="帮我生成一份配置文件：name=anima"))

    assert res.denied_reason == ""  # not denied
    assert entry.runs == 1           # executed through the bridge

    lines = _audit_lines(tmp_path)
    assert not [l for l in lines if l["event"] == "kill_switch_deny"]
    allowed = [l for l in lines if l["event"] == "sandbox_write_allowed"]
    assert allowed


# ---------------------------------------------------------------------------
# R3.5 — RED #3: chat.answer is unaffected by the kill switch (read-only
#         answer path must still fall through to the host LLM).
# ---------------------------------------------------------------------------


def test_r3_5_kill_switch_on_does_not_block_chat_answer(tmp_path):
    entry = _Entry()
    rt = _runtime(tmp_path, _grant_policy(), kill_switch=True, entry=entry)
    # a plain question (no 帮我生成 EXECUTE trigger) -> ANSWER path
    res = rt.handle(_ev(text="今天天气怎么样"))

    # ANSWER is NOT an execution capability -> not denied by the breaker
    assert res.denied_reason != "kill_switch"
    assert entry.runs == 0  # nothing executed

    # and no kill_switch_deny audit was emitted for the chat answer (a pure
    # ANSWER event writes nothing to the execution audit sink at all)
    p = pathlib.Path(tmp_path / "data" / "logs" / "sandbox_write_audit.jsonl")
    if p.exists():
        lines = [json.loads(l) for l in p.read_text(encoding="utf-8").strip().splitlines()]
        assert not [l for l in lines if l["event"] == "kill_switch_deny"]
