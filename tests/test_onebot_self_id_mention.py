"""Regression: group @-mention must be detected even when NapCat omits self_id.

Production incident (2026-08-21): after a restart, ``qq.self_id`` was null and
NapCat group message events arrived without a ``self_id`` field. The old code
read ``self_id`` only from ``raw.get("self_id")``, so every ``@bot`` failed the
``at_qq == self_id`` check → ``mentions_self`` stayed False → group gate kept
returning ``mention_only`` (89 skips in 2h, zero replies/search).

The fix: ``parse_onebot_event`` accepts ``known_self_id`` (the channel's
configured/adaptive bot QQ) and falls back to it for both ``event["self_id"]``
and the @自己/回复自己 detection.
"""
from __future__ import annotations

from anima.channels.onebot11 import parse_onebot_event
from anima.llm.group_gate import classify_group_reply_skip


class _Autonomy:
    reply_only_when_mentioned = True
    familiar_reply_min = 0.5


def _group_at_event(self_id_in_raw, known_self_id, at_qq):
    raw = {
        "post_type": "message",
        "message_type": "group",
        "group_id": 727631526,
        "user_id": 3906509884,
        "self_id": self_id_in_raw,
        "message": [
            {"type": "at", "data": {"qq": at_qq}},
            {"type": "text", "data": {"text": "今天几号了？帮我查一下"}},
        ],
        "raw_message": f"[CQ:at,qq={at_qq}]今天几号了？帮我查一下",
    }
    return parse_onebot_event(raw, known_self_id=known_self_id)


def test_mention_detected_when_self_id_missing_from_event():
    """raw has no self_id, but channel knows its QQ → @self still detected."""
    ev = _group_at_event(None, 2752719822, 2752719822)
    assert ev.get("self_id") == 2752719822
    assert ev.get("mentions_self") is True
    assert ev.get("reply_to_self") is False


def test_mention_detected_when_self_id_present():
    ev = _group_at_event(2752719822, 2752719822, 2752719822)
    assert ev.get("self_id") == 2752719822
    assert ev.get("mentions_self") is True


def test_at_other_member_not_self():
    ev = _group_at_event(None, 2752719822, 3906509884)
    assert ev.get("mentions_self") is False


def test_gate_replies_to_mention_after_fix():
    ev = _group_at_event(None, 2752719822, 2752719822)
    gp = {"reply_mode": "mention", "familiar_min": 0.5, "autonomy": True}
    reason = classify_group_reply_skip(
        ev, ev.get("raw_message", ""), user_id=3906509884,
        group_policy=gp, autonomy=_Autonomy(), data_dir="data",
    )
    assert reason is None


def test_gate_mention_only_when_no_known_self_id():
    """Without any known self_id, an @ that omits self_id cannot be verified."""
    ev = _group_at_event(None, None, 2752719822)
    assert ev.get("self_id") is None
    assert ev.get("mentions_self") is False
