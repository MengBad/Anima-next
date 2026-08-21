"""P8.4 — Conversation Evidence Logging (RED contract).

Records one evidence record per REAL conversation:
    timestamp / group_id / user_id HASH / message_id / request_id /
    intent / response / execution_key / result / latency

Privacy: user id stored only as salted digest; unrelated chat not saved;
records deletable.

Acceptance:
    A  one record writes one JSONL line with all fields
    B  user_id_hash != raw user id; two records of the same user differ
       (per-record salt -> not cross-linkable)
    C  raw qq id never appears in the serialized record
    D  clear() deletes all records (deletable)
    E  read() returns parseable records
    F  static boundary: no System B / gateway / executor import
"""
from __future__ import annotations

import json
import pathlib

from anima.pilot.conversation_evidence import (
    ConversationEvidence,
    ConversationEvidenceLog,
    hash_user_id,
)


def _record(user_id="10001", **overrides):
    base = dict(
        timestamp=1786450000.0,
        group_id="123456",
        user_id_hash=hash_user_id(user_id),
        message_id="m-1",
        request_id="req-1",
        intent="intent-req-1",
        response="你好！",
        execution_key=("run-req-1", "t-1", 0),
        result="SUCCESS",
        latency_ms=12.5,
    )
    base.update(overrides)
    return ConversationEvidence(**base)


# ---------------------------------------------------------------------------
# A - record writes one JSONL line with all fields
# ---------------------------------------------------------------------------
def test_a_record_writes_jsonl(tmp_path):
    log = ConversationEvidenceLog(str(tmp_path / "data"))
    log.record(_record())
    lines = (tmp_path / "data" / "logs" / "conversation_evidence.jsonl") \
        .read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    for key in ("timestamp", "group_id", "user_id_hash", "message_id",
                "request_id", "intent", "response", "execution_key",
                "result", "latency_ms", "platform"):
        assert key in obj
    assert obj["platform"] == "qq"


# ---------------------------------------------------------------------------
# B - user hash is salted; same user different records not linkable
# ---------------------------------------------------------------------------
def test_b_user_hash_privacy(tmp_path):
    log = ConversationEvidenceLog(str(tmp_path / "data"))
    log.record(_record(user_id="10001", message_id="m-1"))
    log.record(_record(user_id="10001", message_id="m-2"))
    recs = log.read()
    assert recs[0]["user_id_hash"] != recs[1]["user_id_hash"]  # per-record salt
    assert recs[0]["user_id_hash"] != "10001"


# ---------------------------------------------------------------------------
# C - raw qq id never in the serialized record
# ---------------------------------------------------------------------------
def test_c_no_raw_user_id(tmp_path):
    log = ConversationEvidenceLog(str(tmp_path / "data"))
    log.record(_record(user_id="10001"))
    raw = (tmp_path / "data" / "logs" / "conversation_evidence.jsonl") \
        .read_text(encoding="utf-8")
    assert "10001" not in raw


# ---------------------------------------------------------------------------
# D - clear() deletes all records
# ---------------------------------------------------------------------------
def test_d_clear_deletes(tmp_path):
    log = ConversationEvidenceLog(str(tmp_path / "data"))
    log.record(_record())
    log.record(_record(message_id="m-2"))
    assert len(log.read()) == 2
    removed = log.clear()
    assert removed == 2
    assert log.read() == []
    assert log.clear() == 0


# ---------------------------------------------------------------------------
# E - read() returns parseable records
# ---------------------------------------------------------------------------
def test_e_read_parseable(tmp_path):
    log = ConversationEvidenceLog(str(tmp_path / "data"))
    log.record(_record())
    log.record(_record(message_id="m-2", result="FAILED"))
    recs = log.read()
    assert len(recs) == 2
    assert recs[0]["result"] == "SUCCESS"
    assert recs[1]["result"] == "FAILED"


# ---------------------------------------------------------------------------
# F - static boundary
# ---------------------------------------------------------------------------
def test_f_static_boundary():
    src = pathlib.Path("anima/pilot/conversation_evidence.py").read_text(encoding="utf-8")
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
