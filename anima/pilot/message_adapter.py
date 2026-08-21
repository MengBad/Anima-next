"""P8.1 — NapCat Message Adapter (standardized QQ input layer).

The ONLY job of this adapter: receive a normalized OneBot11 event (produced by
the existing OneBot11Adapter channel), standardize it into the P8 Message
Schema, and forward it to the internal system.  It NEVER calls the executor,
never performs planning, never executes anything — it is a pure boundary
between the QQ transport and the Entry Gate.

Message Schema (P8.0):
    {
      "group_id": "", "user_id": "", "message_id": "",
      "content": "", "time": "", "platform": "qq"
    }

Scope limits (P8.0 approval):
    * QQ input layer ONLY — no executor, no planning, no System B, no gateway
      mutation, no capability activation, no env bypass.
    * PASSIVE by design: the adapter does not decide to reply; the Entry Gate
      decides.  No auto-spam, no auto-DM, no auto-join.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = ["QQMessage", "NapCatMessageAdapter", "normalize_qq_message"]

#: canonical platform tag for the P8 Message Schema
_PLATFORM = "qq"


@dataclass(frozen=True, slots=True)
class QQMessage:
    """One standardized inbound QQ message (P8 Message Schema)."""

    group_id: str
    user_id: str
    message_id: str
    content: str
    time: str
    platform: str = _PLATFORM
    #: whether the message summons the bot (@"bot" / reply-to-bot) — the
    #: PASSIVE trigger the Entry Gate uses; never derived from content alone.
    triggered: bool = False
    #: optional reference to the original event for downstream evidence
    #: (identity binding); not part of the schema, kept out of serialization.
    raw_event: dict = field(default_factory=dict, repr=False, compare=False)

    def to_dict(self) -> dict:
        """The P8 Message Schema dict (no raw_event — evidence-level only)."""
        return {
            "group_id": self.group_id,
            "user_id": self.user_id,
            "message_id": self.message_id,
            "content": self.content,
            "time": self.time,
            "platform": self.platform,
        }


class NapCatMessageAdapter:
    """Standardize OneBot11 events into QQMessage (fail closed on malformed).

    Non-message events (notices / meta / requests) are dropped (return None).
    A message without a user_id is dropped (cannot bind identity).  The
    adapter never replies, never executes, never decides policy.
    """

    def normalize(self, event: Optional[dict]) -> Optional[QQMessage]:
        """Event -> QQMessage, or None when the event is not an actionable message."""
        if not isinstance(event, dict):
            return None
        topic = str(event.get("topic", ""))
        if not topic.startswith("event.message."):
            return None  # notice / meta / request — not an inbound chat message
        user_id = event.get("user_id")
        if user_id in (None, ""):
            return None  # cannot bind identity without a sender
        raw = event.get("raw", {}) or {}
        detail_type = raw.get("message_type", topic.rsplit(".", 1)[-1])
        if detail_type != "group":
            return None  # P8.0: group pilot only — no auto-DM
        group_id = event.get("group_id")
        if group_id in (None, ""):
            return None
        # PASSIVE trigger: bot was mentioned or replied-to (never content-based)
        triggered = bool(
            event.get("mentions_self")
            or event.get("reply_to_self")
        )
        content = str(event.get("text") or event.get("message") or "").strip()
        ts = str(event.get("time") or int(time.time()))
        return QQMessage(
            group_id=str(group_id),
            user_id=str(user_id),
            message_id=str(event.get("message_id") or ""),
            content=content,
            time=ts,
            triggered=triggered,
            raw_event=event,
        )


def normalize_qq_message(event: Optional[dict]) -> Optional[QQMessage]:
    """Module-level convenience for tests / wiring."""
    return NapCatMessageAdapter().normalize(event)
