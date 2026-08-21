"""P8.2 — Real User Entry Gate.

Every REAL QQ message passes through this gate before anything downstream:

    QQ Message -> Scope Check -> Permission Check -> Rate Limit
               -> Identity Bind -> Planner

Fail closed by default: an empty ``allowed_groups`` list allows NOTHING
(``[]`` default per P8.0).  Reply mode is PASSIVE: only triggered messages
(@"bot" / reply-to-bot) are admitted for reply; untriggered group chatter is
observed but never auto-replied (no auto-spam).

Gate checks (P8.0):
    * group whitelist       - allowed_groups (default []) -> fail closed
    * user whitelist        - allowed_users (optional, default empty = all
                              members of an allowed group are eligible)
    * reply mode            - PASSIVE: triggered only (never content-derived)
    * rate limit            - per (group, user) min interval (default 3s)
    * identity bind         - the gate returns a bound request context that
                              carries qq ids + a fresh request_id; it NEVER
                              treats qq identity as execution identity

Scope limits (P8.0): no executor call, no planning, no System B, no gateway
mutation, no auto-join, no auto-DM, no capability activation, no env bypass.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Optional

__all__ = [
    "EntryGate",
    "GateConfig",
    "GateDecision",
    "PASSIVE_MODE",
]

#: the only reply mode this pilot admits (never active auto-chat).
PASSIVE_MODE = "PASSIVE"


@dataclass(frozen=True, slots=True)
class GateConfig:
    """P8.2 configuration (all fail-closed defaults).

    ``keyword_triggers`` (P10.2-C owner decision, 2026-08-12): an explicit
    whitelist of substrings that make a message answerable WITHOUT an
    @-mention — but ONLY inside allowed groups, and the message still goes
    through the full pipeline (identity -> classify -> ANSWER, never
    execution).  Empty (default) keeps the pure PASSIVE behaviour.
    """

    allowed_groups: tuple = ()          # empty -> deny all (P8.0 default [])
    allowed_users: tuple = ()           # empty -> any member of an allowed group
    reply_mode: str = PASSIVE_MODE      # PASSIVE only
    min_interval_sec: float = 3.0       # per (group, user) rate limit
    max_content_len: int = 2000         # hard cap on admitted content
    keyword_triggers: tuple = ()        # explicit content triggers (opt-in)


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Outcome of the gate for one message."""

    admitted: bool
    reason: str = ""                    # why denied (for metrics/evidence)
    request_id: str = ""                # fresh per admitted message (P8.3 seed)
    group_id: str = ""
    user_id: str = ""
    message_id: str = ""
    content: str = ""
    triggered: bool = False

    def to_dict(self) -> dict:
        return {
            "admitted": self.admitted,
            "reason": self.reason,
            "request_id": self.request_id,
            "group_id": self.group_id,
            "user_id": self.user_id,
            "message_id": self.message_id,
            "triggered": self.triggered,
        }


class EntryGate:
    """PASSIVE, fail-closed admission gate for real QQ traffic."""

    def __init__(self, config: Optional[GateConfig] = None) -> None:
        self._config = config or GateConfig()
        # per (group, user) last-admitted ts for rate limiting
        self._last_ts: dict[tuple, float] = {}

    # -- config surface (audit) ------------------------------------------

    @property
    def config(self) -> GateConfig:
        return self._config

    def _in_allowed_group(self, group_id: str) -> bool:
        return group_id in self._config.allowed_groups

    def _in_allowed_users(self, user_id: str) -> bool:
        if not self._config.allowed_users:
            return True  # empty user whitelist = any member of an allowed group
        return user_id in self._config.allowed_users

    def _rate_limited(self, group_id: str, user_id: str) -> bool:
        key = (group_id, user_id)
        now = time.monotonic()
        last = self._last_ts.get(key)
        if last is not None and (now - last) < self._config.min_interval_sec:
            return True
        self._last_ts[key] = now
        return False

    # -- admission ---------------------------------------------------------

    def admit(
        self,
        *,
        group_id: str,
        user_id: str,
        message_id: str,
        content: str,
        triggered: bool,
    ) -> GateDecision:
        """Decide whether ONE message may proceed (fail closed)."""
        # 1. Scope check: group whitelist (P8.0: default [] -> deny all)
        if not self._in_allowed_group(group_id):
            return GateDecision(False, reason="group_not_allowed",
                                group_id=group_id, user_id=user_id,
                                message_id=message_id, content=content,
                                triggered=triggered)
        # 2. Permission check: user whitelist (optional)
        if not self._in_allowed_users(user_id):
            return GateDecision(False, reason="user_not_allowed",
                                group_id=group_id, user_id=user_id,
                                message_id=message_id, content=content,
                                triggered=triggered)
        # 3. PASSIVE: only triggered messages are admitted for reply, unless
        #    an explicit keyword trigger matched (opt-in whitelist, inside an
        #    allowed group only — never execution, never out of scope).
        if not triggered:
            kw_hit = any(kw in content for kw in self._config.keyword_triggers)
            if not kw_hit:
                return GateDecision(False, reason="passive_not_triggered",
                                    group_id=group_id, user_id=user_id,
                                    message_id=message_id, content=content,
                                    triggered=triggered)
        # 4. content cap (defense against pathological length)
        if len(content) > self._config.max_content_len:
            return GateDecision(False, reason="content_too_long",
                                group_id=group_id, user_id=user_id,
                                message_id=message_id, content=content,
                                triggered=triggered)
        # 5. rate limit
        if self._rate_limited(group_id, user_id):
            return GateDecision(False, reason="rate_limited",
                                group_id=group_id, user_id=user_id,
                                message_id=message_id, content=content,
                                triggered=triggered)
        # 6. Identity bind: a fresh request_id per admitted message (P8.3 seed)
        request_id = f"req-{secrets.token_hex(8)}"
        return GateDecision(True, reason="admitted", request_id=request_id,
                            group_id=group_id, user_id=user_id,
                            message_id=message_id, content=content,
                            triggered=triggered)
