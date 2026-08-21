"""P10.4-R2-B — Media / Sticker recovery controller.

Wraps the existing sticker/meme/voice selection behind a hard capability gate
(QQIdentityPolicy), the response SafetyFilter, an explicit size limit and an
audit sink.

Invariants (fail-closed, recovery-safe):
    * The controller NEVER sends anything.  It only returns a local asset path
      for the caller to attach to an already-authorised reply.  No auto-spam,
      no active send.
    * Every request is denied unless BOTH the capability is explicitly granted
      for (subject, group) AND the request text passes the SafetyFilter.
    * An explicit byte-size ceiling is enforced (defence in depth over the
      StickerManager heuristic) — oversized assets are rejected.
    * All outcomes are audited with a one-way subject hash (never plaintext QQ).

Default OFF: the controller is only constructed/used when the relevant
``media.*`` capability is explicitly granted via QQIdentityPolicy (owner enable
token).  Without that grant, ``request()`` returns a DENY decision.

Scope limits (P8.0 / P10.4): no System B, no gateway, no capability activation,
no env bypass; the controller only decides and returns a path, it never acts.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from anima.pilot.identity_policy import QQIdentityPolicy, grant_subject
from anima.pilot.safety_filter import SafetyFilter, SafetyResult

__all__ = [
    "MediaRecoveryController",
    "MediaDecision",
    "DEFAULT_MAX_STICKER_BYTES",
    "GIF_MAX_STICKER_BYTES",
]

#: size ceilings (defence in depth over the StickerManager heuristic)
DEFAULT_MAX_STICKER_BYTES = 700 * 1024          # non-gif cap, matches StickerManager
GIF_MAX_STICKER_BYTES = 3 * 1024 * 1024          # gif tolerance

#: a picker maps (query, group_id, allow_cross_group) -> local path or None.
StickerPicker = Callable[[str, object, bool], Optional[str]]
#: audit sink receives a plain dict (subject_hash / media_type / action / ts).
AuditSink = Callable[[dict], None]


@dataclass(frozen=True, slots=True)
class MediaDecision:
    """One media recovery decision (read-only)."""

    allowed: bool
    reason: str
    path: Optional[str] = None
    size_bytes: int = 0


class MediaRecoveryController:
    """Fail-closed media/sticker recovery gate.

    ``pickers`` maps a capability name (e.g. ``"media.sticker"``) to a callable
    that performs the actual (already-authorised) selection.  Cooldown and
    group-scoped eligibility live inside the picker (e.g. StickerManager); the
    controller adds capability + safety + size + audit.
    """

    def __init__(
        self,
        policy: QQIdentityPolicy,
        safety: SafetyFilter,
        pickers: Dict[str, StickerPicker],
        *,
        audit: Optional[AuditSink] = None,
        max_sticker_bytes: int = DEFAULT_MAX_STICKER_BYTES,
        gif_max_bytes: int = GIF_MAX_STICKER_BYTES,
    ) -> None:
        self._policy = policy
        self._safety = safety
        self._pickers = dict(pickers or {})
        self._audit = audit
        self._max = max(1, int(max_sticker_bytes))
        self._gif_max = max(1, int(gif_max_bytes))

    def request(
        self,
        user_id: object,
        group_id: object,
        capability: str,
        query: str = "",
        *,
        allow_cross_group: bool = False,
    ) -> MediaDecision:
        subject = grant_subject(str(user_id))
        picker = self._pickers.get(capability)
        # emit "requested" first so denials are always observable
        self._emit(subject, capability, "requested")

        # 1) capability gate (fail closed)
        if picker is None or not self._policy.can(
            subject, capability, group_id=str(group_id)
        ):
            self._emit(subject, capability, "blocked", reason="capability_denied")
            return MediaDecision(False, "capability_denied")

        # 2) safety filter on the request text (no leaked secrets / system info)
        sr: SafetyResult = self._safety.filter(query)
        if not sr.safe:
            self._emit(subject, capability, "blocked", reason=f"safety:{sr.reason}")
            return MediaDecision(False, f"safety:{sr.reason}")

        # 3) selection (cooldown + group eligibility handled by the picker)
        try:
            path = picker(query, group_id, allow_cross_group)
        except Exception:
            self._emit(subject, capability, "blocked", reason="picker_error")
            return MediaDecision(False, "picker_error")
        if not path:
            self._emit(subject, capability, "allowed_empty", reason="no_candidate")
            return MediaDecision(False, "no_candidate")

        # 4) explicit size limit (defence in depth)
        size = self._file_size(path)
        if size < 0:
            self._emit(subject, capability, "blocked", reason="file_unreadable")
            return MediaDecision(False, "file_unreadable")
        cap = self._gif_max if str(path).lower().endswith(".gif") else self._max
        if size > cap:
            self._emit(subject, capability, "blocked", reason="size_exceeded")
            return MediaDecision(False, "size_exceeded")

        self._emit(subject, capability, "allowed", reason="selected")
        return MediaDecision(True, "selected", path=path, size_bytes=size)

    @staticmethod
    def _file_size(path: str) -> int:
        try:
            return os.path.getsize(path)
        except OSError:
            return -1

    def _emit(self, subject_hash: str, media_type: str, action: str, reason: str = "") -> None:
        if self._audit is None:
            return
        try:
            self._audit({
                "subject_hash": subject_hash,
                "media_type": media_type,
                "action": action,
                "reason": reason[:120],
                "ts": time.time(),
            })
        except Exception:
            pass  # audit must never break the request
