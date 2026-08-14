"""P10.4-R3.4 — Audit Completeness Patch (RED contract).

Closes G3 at the handle() audit site: when a sandbox.write is ALLOWED and
executed through the F5 pilot entry, the durable audit sink
(sandbox_write_audit.jsonl) MUST record the provider's safe hash fields
(path_hash / sha256 / bytes) — not leave them empty.

This test MUST fail before the R3.4 change in pilot_runtime.handle() lands,
then pass after IMPLEMENT.

Scope guardrails (unchanged by R3.4):
  * capability set is untouched
  * grant / identity policy is untouched
  * execution bridge (PilotEntry) is untouched
  * sandbox permissions / provider are untouched
Only the audit enrichment in handle() is exercised here.
"""

import hashlib
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
    """F5-style pilot entry stub: admits + records runs (no real FS).

    Mirrors the production PilotEntry contract for handle(): run() returns a
    string (success / FAILED observation).  The provider result is intentionally
    NOT surfaced (that is exactly the gap R3.4 closes at the handle() layer).
    """

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


def _audit_lines(tmp_path):
    p = pathlib.Path(tmp_path / "data" / "logs" / "sandbox_write_audit.jsonl")
    assert p.exists(), "audit sink must exist after a handled event"
    return [json.loads(l) for l in p.read_text(encoding="utf-8").strip().splitlines()]


# ---------------------------------------------------------------------------
# R3.4 — RED: the allowed sandbox.write audit event must carry the provider's
#         safe hash fields (path_hash / sha256 / bytes), not leave them empty.
# ---------------------------------------------------------------------------


def test_r3_4_allowed_audit_carries_provider_hashes(tmp_path):
    text = "帮我生成一份配置文件：name=anima"
    entry = _Entry()
    rt = _runtime(tmp_path, _grant_policy(), entry=entry)
    rt.handle(_ev(text=text))  # owner -> granted -> executed

    lines = _audit_lines(tmp_path)
    allowed = [l for l in lines if l["event"] == "sandbox_write_allowed"]
    assert allowed, "sandbox_write_allowed event must be present after execution"
    ev = allowed[0]

    # path_hash: non-empty 32-hex (mirrors provider's sha256(path)[:32])
    assert ev["path_hash"], "G3: path_hash must be populated, not empty"
    assert len(ev["path_hash"]) == 32
    assert all(c in "0123456789abcdef" for c in ev["path_hash"])

    # sha256: must equal the provider's content hash (no content leakage — it is
    # the digest, never the plaintext)
    expected_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert ev["sha256"] == expected_sha, "G3: sha256 must match the written content"
    assert ev["sha256"] != text

    # bytes: must equal the written content byte length
    assert ev["bytes"] == len(text.encode("utf-8")), "G3: bytes must equal content size"
    assert ev["bytes"] > 0

    # still no plaintext qq / secret / content in the sink (G3 invariant)
    raw = pathlib.Path(tmp_path / "data" / "logs" / "sandbox_write_audit.jsonl").read_text(
        encoding="utf-8"
    )
    assert OWNER_QQ not in raw
    assert "name=anima" not in raw


def test_r3_4_denied_audit_has_no_plaintext(tmp_path):
    """Negative control: a denied (unknown user) write records NO hashes and
    NO plaintext — proving the enrichment only attaches on the allowed path."""
    entry = _Entry()
    rt = _runtime(tmp_path, _grant_policy(), entry=entry)
    rt.handle(_ev(user_id="99999999", text="帮我生成一份文件"))  # unknown -> policy block

    lines = _audit_lines(tmp_path)
    blocked = [l for l in lines if l["event"] == "sandbox_write_policy_block"]
    assert blocked
    # blocked events legitimately carry empty safe fields
    assert blocked[0]["path_hash"] == ""
    assert blocked[0]["sha256"] == ""
    assert blocked[0]["bytes"] == 0
    raw = pathlib.Path(tmp_path / "data" / "logs" / "sandbox_write_audit.jsonl").read_text(
        encoding="utf-8"
    )
    assert OWNER_QQ not in raw  # no plaintext in any case
