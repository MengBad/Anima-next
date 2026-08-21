"""P8.1 — NapCat Message Adapter (RED contract).

The adapter standardizes OneBot11 events into the P8 Message Schema and
forwards them to the internal system.  It NEVER calls the executor, never
plans, never replies, never decides policy.

Acceptance:
    A  group message -> QQMessage with the full P8 schema
    B  PASSIVE trigger: mentions_self / reply_to_self -> triggered=True
    C  non-message events (notice/meta/request) -> None
    D  private message -> None (group pilot only, no auto-DM)
    E  missing user_id / group_id -> None (fail closed)
    F  schema has exactly the 6 P8 fields (platform=qq)
    G  adapter never imports executor / planning / System B
    H  content-only "summon" words do NOT set triggered (PASSIVE)
"""
from __future__ import annotations

import pathlib

from anima.pilot.message_adapter import (
    NapCatMessageAdapter,
    QQMessage,
    normalize_qq_message,
)


def _group_event(**overrides):
    ev = {
        "topic": "event.message.group",
        "user_id": 10001,
        "group_id": 123456,
        "message_id": "m-001",
        "text": "你好",
        "time": 1786450000,
        "raw": {"message_type": "group", "sender": {"nickname": "u1"}},
    }
    ev.update(overrides)
    return ev


def _adapter():
    return NapCatMessageAdapter()


# ---------------------------------------------------------------------------
# A - group message -> full P8 schema
# ---------------------------------------------------------------------------
def test_a_group_message_schema():
    msg = _adapter().normalize(_group_event())
    assert isinstance(msg, QQMessage)
    d = msg.to_dict()
    assert d == {
        "group_id": "123456",
        "user_id": "10001",
        "message_id": "m-001",
        "content": "你好",
        "time": "1786450000",
        "platform": "qq",
    }


# ---------------------------------------------------------------------------
# B - PASSIVE trigger: mentions_self / reply_to_self
# ---------------------------------------------------------------------------
def test_b_passive_trigger():
    m1 = _adapter().normalize(_group_event(mentions_self=True))
    assert m1.triggered is True
    m2 = _adapter().normalize(_group_event(reply_to_self=True))
    assert m2.triggered is True
    m3 = _adapter().normalize(_group_event())
    assert m3.triggered is False


# ---------------------------------------------------------------------------
# C - non-message events -> None
# ---------------------------------------------------------------------------
def test_c_non_message_events_none():
    a = _adapter()
    assert a.normalize({"topic": "event.notice", "user_id": 1}) is None
    assert a.normalize({"topic": "event.meta", "user_id": 1}) is None
    assert a.normalize({"topic": "event.request", "user_id": 1}) is None
    assert a.normalize({"topic": "event.message.unknown", "user_id": 1}) is None


# ---------------------------------------------------------------------------
# D - private message -> None (group pilot only)
# ---------------------------------------------------------------------------
def test_d_private_message_none():
    ev = _group_event(topic="event.message.private",
                      raw={"message_type": "private"})
    assert _adapter().normalize(ev) is None


# ---------------------------------------------------------------------------
# E - missing identity -> None (fail closed)
# ---------------------------------------------------------------------------
def test_e_missing_identity_none():
    a = _adapter()
    assert a.normalize(_group_event(user_id=None)) is None
    assert a.normalize(_group_event(user_id="")) is None
    assert a.normalize(_group_event(group_id=None)) is None
    assert a.normalize(_group_event(group_id="")) is None
    assert a.normalize(None) is None


# ---------------------------------------------------------------------------
# F - schema has exactly the 6 P8 fields
# ---------------------------------------------------------------------------
def test_f_schema_exact_fields():
    d = _adapter().normalize(_group_event()).to_dict()
    assert set(d.keys()) == {
        "group_id", "user_id", "message_id", "content", "time", "platform",
    }
    assert d["platform"] == "qq"


# ---------------------------------------------------------------------------
# G - static boundary: no executor / planning / System B
# ---------------------------------------------------------------------------
def test_g_static_boundary():
    src = pathlib.Path("anima/pilot/message_adapter.py").read_text(encoding="utf-8")
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
# H - content-only summon words do NOT set triggered (PASSIVE)
# ---------------------------------------------------------------------------
def test_h_content_does_not_trigger():
    ev = _group_event(text="@Anima 你好")
    # even if the text contains a summon, WITHOUT mentions_self the adapter
    # must NOT set triggered (the trigger decision belongs to the channel /
    # Entry Gate, never to content heuristics here)
    msg = _adapter().normalize(ev)
    assert msg.triggered is False
