"""P8.7 — Live Pilot Runtime (REAL_USER_CONTROLLED orchestration).

Wires the P8 chain for ONE real QQ message end-to-end:

    QQ event -> NapCatMessageAdapter (P8.1)
              -> EntryGate (P8.2, whitelist + PASSIVE + rate limit)
              -> IdentityCorrelator (P8.3)
              -> PermissionBoundary (P8.6)
              -> (bounded execute via the F5 PilotEntry) or read-only answer
              -> SafetyFilter (P8.5)
              -> ConversationEvidenceLog (P8.4)
              -> reply text

Mode: REAL_USER_CONTROLLED (P8.7).  The runtime is a thin, testable glue:
policy decisions live in the gate / boundary; execution lives in the F5
PilotEntry (sandbox.write only); nothing here bypasses the kill switch.

Kill switch: when the pilot switch is DISABLED, EVERYTHING is denied before
any admission — the runtime fails closed at the very first check.

Scope limits (P8.0): no System B, no gateway mutation, no capability
activation, no auto-join / auto-DM, no env bypass, no key merge.
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any, Optional

from anima.pilot.conversation_evidence import (
    ConversationEvidence,
    ConversationEvidenceLog,
    hash_user_id,
)
from anima.pilot.entry_gate import EntryGate, GateConfig, GateDecision
from anima.pilot.identity_correlator import IdentityChain, IdentityCorrelator
from anima.pilot.identity_policy import grant_subject
from anima.pilot.message_adapter import NapCatMessageAdapter, QQMessage
from anima.pilot.permission_boundary import (
    ACTION_ANSWER,
    ACTION_DENY,
    ACTION_EXECUTE,
    PermissionBoundary,
)
from anima.pilot.safety_filter import SafetyFilter, SafetyResult

__all__ = ["PilotRuntime", "LivePilotConfig", "LivePilotResult", "REAL_USER_CONTROLLED"]

REAL_USER_CONTROLLED = "REAL_USER_CONTROLLED"


class LivePilotConfig:
    """P8.7 deployment config (fail-closed defaults)."""

    def __init__(
        self,
        *,
        allowed_groups: tuple = (),
        allowed_users: tuple = (),
        max_groups: int = 3,
        unlimited_users: bool = False,
        min_interval_sec: float = 3.0,
        max_content_len: int = 2000,
        mode: str = REAL_USER_CONTROLLED,
        keyword_triggers: tuple = (),   # P10.2-C: explicit content triggers
        # P10.4-R2.5: controlled-observation activation flags (fail-closed OFF).
        media_mode: str = "off",        # "off" | "observe"  (never "autonomy")
        media_pilot_groups: tuple = (), # groups where media recovery may respond
        sylann_mode: str = "shadow",    # "shadow" | "suggestion" (never "active")
    ) -> None:
        self.allowed_groups = tuple(allowed_groups)
        self.allowed_users = tuple(allowed_users)
        self.max_groups = max_groups
        self.unlimited_users = unlimited_users
        self.min_interval_sec = min_interval_sec
        self.max_content_len = max_content_len
        self.mode = mode
        self.keyword_triggers = tuple(keyword_triggers)
        self.media_mode = "observe" if str(media_mode) == "observe" else "off"
        self.media_pilot_groups = tuple(str(g) for g in (media_pilot_groups or ()))
        self.sylann_mode = "suggestion" if str(sylann_mode) == "suggestion" else "shadow"

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "LivePilotConfig":
        d = d or {}
        return cls(
            allowed_groups=tuple(d.get("allowed_groups") or ()),
            allowed_users=tuple(d.get("allowed_users") or ()),
            max_groups=int(d.get("max_groups", 3)),
            unlimited_users=bool(d.get("unlimited_users", False)),
            min_interval_sec=float(d.get("min_interval_sec", 3.0)),
            max_content_len=int(d.get("max_content_len", 2000)),
            mode=str(d.get("mode", REAL_USER_CONTROLLED)),
            keyword_triggers=tuple(d.get("keyword_triggers") or ()),
            media_mode=str(d.get("media_mode", "off")),
            media_pilot_groups=tuple(d.get("media_pilot_groups") or ()),
            sylann_mode=str(d.get("sylann_mode", "shadow")),
        )


class LivePilotResult:
    __slots__ = ("handled", "reply", "denied_reason", "chain", "evidence")

    def __init__(self, handled: bool, reply: str = "", denied_reason: str = "",
                 chain: Optional[IdentityChain] = None, evidence: Optional[ConversationEvidence] = None):
        self.handled = handled
        self.reply = reply
        self.denied_reason = denied_reason
        self.chain = chain
        self.evidence = evidence


class PilotRuntime:
    """Orchestrate ONE real QQ message through the P8 chain."""

    def __init__(
        self,
        *,
        config: Optional[LivePilotConfig] = None,
        data_dir: str,
        pilot_entry: Any = None,       # F5 PilotEntry (sandbox.write only)
        kill_switch: Any = None,       # BridgeKillSwitch (fail closed)
        evidence_log: Optional[ConversationEvidenceLog] = None,
        reply_limit: int = 800,        # max reply length to the QQ group
        metrics_log: Optional[str] = None,  # P9.5: JSONL path for per-event metrics
        identity_policy: Any = None,   # P10.2-C: QQIdentityPolicy (capability grants)
    ) -> None:
        self._config = config or LivePilotConfig()
        self._adapter = NapCatMessageAdapter()
        self._gate = EntryGate(GateConfig(
            allowed_groups=self._config.allowed_groups,
            allowed_users=self._config.allowed_users,
            reply_mode="PASSIVE",
            min_interval_sec=self._config.min_interval_sec,
            max_content_len=self._config.max_content_len,
            keyword_triggers=self._config.keyword_triggers,
        ))
        self._identity = IdentityCorrelator(pilot_enablement_id="pilot-live-001")
        self._permission = PermissionBoundary()
        self._safety = SafetyFilter()
        self._pilot_entry = pilot_entry
        self._kill_switch = kill_switch
        self._data_dir = data_dir
        self._evidence_log = evidence_log or ConversationEvidenceLog(data_dir)
        self._reply_limit = reply_limit
        # P9.5: cross-process metrics sink — the 72h observer reads this file
        # to aggregate real traffic metrics (messages/answered/reject/...).
        self._metrics_log = metrics_log
        if metrics_log:
            Path(metrics_log).parent.mkdir(parents=True, exist_ok=True)
        # P10.2-C: capability grant policy (fail closed: disabled policy denies
        # everything, so the runtime behaves exactly as before when no grants
        # are configured).
        if identity_policy is None:
            from anima.pilot.identity_policy import (
                IdentityPolicyConfig,
                QQIdentityPolicy,
            )

            identity_policy = QQIdentityPolicy(IdentityPolicyConfig())
        self._policy = identity_policy
        # P10.2-C: sandbox.write audit sink (subject hash only)
        self._grant_audit_path = (
            Path(data_dir) / "logs" / "sandbox_write_audit.jsonl"
        )
        self._grant_audit_path.parent.mkdir(parents=True, exist_ok=True)

    def _audit_grant(self, *, event: str, subject: str, capability: str,
                     path: str = "", decision: str = "", reason: str = "",
                     path_hash: str = "", sha256: str = "", byte_count: int = 0,
                     result: str = "", denied_reason: str = "") -> None:
        """P10.2-C audit: JSONL, subject hash only (never plaintext qq/token).

        R3.1 (G3): schema extended with safe fields only — path_hash / sha256 /
        byte_count / result / denied_reason. File CONTENT, plaintext qq, and
        secrets are NEVER written (callers must not pass them).
        """
        import json as _json

        try:
            entry = {
                "ts": time.time(),
                "event": event,
                "subject_hash": subject,
                "capability": capability,
                "path": path[:200],
                "decision": decision,
                "reason": reason[:200],
                "path_hash": path_hash,
                "sha256": sha256,
                "bytes": byte_count,
                "result": result,
                "denied_reason": denied_reason[:200],
            }
            with self._grant_audit_path.open("a", encoding="utf-8") as fh:
                fh.write(_json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass  # audit must never break the pilot path

    def _record_metric(self, *, action: str, result: str, denied_reason: str,
                       latency_ms: float, group_id: str) -> None:
        """P9.5: append one metrics line (privacy: no raw qq user id)."""
        if not self._metrics_log:
            return
        import json as _json

        try:
            entry = {
                "ts": time.time(),
                "action": action,
                "result": result,
                "denied_reason": denied_reason,
                "latency_ms": round(latency_ms, 3),
                "group_id": group_id,
            }
            with Path(self._metrics_log).open("a", encoding="utf-8") as fh:
                fh.write(_json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass  # metrics must never break the pilot path

    # -- kill switch (fail closed at the very first check) ----------------

    def _switch_enabled(self) -> bool:
        if self._kill_switch is None:
            return True  # no switch wired = no gate to flip (test mode)
        try:
            self._kill_switch.check()
            return True
        except Exception:  # noqa: BLE001 - any switch failure denies
            return False

    # -- main entry ---------------------------------------------------------

    def handle(self, event: Optional[dict]) -> LivePilotResult:
        """Process one real QQ event; returns the (possibly empty) reply plan."""
        if not self._switch_enabled():
            self._record_metric(action="deny", result="DENIED",
                                denied_reason="kill_switch_disabled",
                                latency_ms=0.0,
                                group_id=str((event or {}).get("group_id", "")))
            return LivePilotResult(False, denied_reason="kill_switch_disabled")
        msg: Optional[QQMessage] = self._adapter.normalize(event)
        if msg is None:
            self._record_metric(action="drop", result="DROPPED",
                                denied_reason="not_a_group_message",
                                latency_ms=0.0,
                                group_id=str((event or {}).get("group_id", "")))
            return LivePilotResult(False, denied_reason="not_a_group_message")

        decision: GateDecision = self._gate.admit(
            group_id=msg.group_id,
            user_id=msg.user_id,
            message_id=msg.message_id,
            content=msg.content,
            triggered=msg.triggered,
        )
        if not decision.admitted:
            self._record_metric(action="deny", result="DENIED",
                                denied_reason=decision.reason, latency_ms=0.0,
                                group_id=msg.group_id)
            return LivePilotResult(False, denied_reason=decision.reason)

        chain: IdentityChain = self._identity.bind(
            group_id=decision.group_id,
            user_id=decision.user_id,
            request_id=decision.request_id,
        )
        intent = self._permission.classify(decision.content)
        reply = ""
        result = "DENIED"
        started = time.time()

        if intent.action == ACTION_DENY:
            reply = "该操作不在允许范围内，已拒绝。如需执行请通过审批流程。"
            result = "DENIED"
        elif intent.action == ACTION_EXECUTE:
            # P10.2-C: capability grant check FIRST (fail closed).  The
            # subject is the DETERMINISTIC digest of the qq id (grant_subject)
            # — never the plaintext — and the policy decides via
            # capability_levels + user_levels.  A policy that is disabled /
            # undeclared / under-level DENIES.
            subject = grant_subject(decision.user_id)
            self._audit_grant(
                event="sandbox_write_requested",
                subject=subject, capability="sandbox.write",
                path="", decision="PENDING", reason="",
            )
            if not self._policy.can(
                subject, "sandbox.write", group_id=decision.group_id
            ):
                self._audit_grant(
                    event="sandbox_write_policy_block",
                    subject=subject, capability="sandbox.write",
                    path="", decision="DENY",
                    reason="capability check failed (no grant / under level)",
                )
                reply = "执行能力未授权：权限检查未通过。"
                result = "BLOCKED"
            elif self._pilot_entry is not None and self._pilot_entry.admit(
                f"goal#pilot-{decision.group_id}",
                f"plan#pilot-{decision.request_id}",
            ):
                reply = self._pilot_entry.run(
                    task_id=f"task-{decision.request_id}",
                    request=decision.content,
                    goal_id=f"goal#pilot-{decision.group_id}",
                    plan_id=f"plan#pilot-{decision.request_id}",
                )
                result = "FAILED" if "FAILED observation" in reply else "EXECUTED"
                # R3.4 (G3 close): forward the provider's safe audit fields to
                # the durable sink.  PilotEntry.run() returns only a string and
                # swallows the ProviderResult, so handle() mirrors the provider's
                # DETERMINISTIC hash formula on the SAME write inputs it hands the
                # bridge — path = "pilot/task-{request_id}.txt", content =
                # decision.content — yielding values identical to the real on-disk
                # write.  No plaintext qq / secret / content ever leaves here.
                _sw_path = f"pilot/task-{decision.request_id}.txt"
                _sw_content = decision.content or ""
                self._audit_grant(
                    event="sandbox_write_allowed",
                    subject=subject, capability="sandbox.write",
                    path="", decision="ALLOW", reason="granted",
                    result=result,
                    path_hash=hashlib.sha256(_sw_path.encode("utf-8")).hexdigest()[:32],
                    sha256=hashlib.sha256(_sw_content.encode("utf-8")).hexdigest(),
                    byte_count=len(_sw_content.encode("utf-8")),
                )
            else:
                self._audit_grant(
                    event="sandbox_write_denied",
                    subject=subject, capability="sandbox.write",
                    path="", decision="DENY",
                    reason="execution bridge not available / not admitted",
                )
                reply = "执行能力当前不可用（受控试点未授权该请求）。"
                result = "BLOCKED"
        else:  # ANSWER
            # P9.5: the pilot completes gating + evidence but does NOT own an
            # LLM — the question is handed back (handled=False) so the
            # coordinator falls back to the host LLM handler for the real
            # answer (the same production-grade answer path the bot uses for
            # every group message).  No受理确认 is sent; the user gets the
            # actual LLM reply once.
            reply = ""
            result = "ANSWERED"

        latency_ms = (time.time() - started) * 1000.0
        # Safety filter (P8.5) BEFORE the reply leaves
        safe: SafetyResult = self._safety.filter(reply)
        final_reply = safe.answer if safe.safe else (
            "回复包含不安全内容，已拦截。" if reply else ""
        )
        if len(final_reply) > self._reply_limit:
            final_reply = final_reply[: self._reply_limit] + "…"

        # Evidence (P8.4): record the admitted conversation (user hashed)
        evidence = ConversationEvidence(
            timestamp=time.time(),
            group_id=decision.group_id,
            user_id_hash=hash_user_id(decision.user_id),
            message_id=decision.message_id,
            request_id=decision.request_id,
            intent=intent.action,
            response=final_reply,
            execution_key=chain.execution_key,
            result=result if safe.safe else "BLOCKED",
            latency_ms=latency_ms,
        )
        self._evidence_log.record(evidence)

        # P9.5: cross-process metrics line for the 72h observer
        self._record_metric(
            action=intent.action,
            result=result if safe.safe else "BLOCKED",
            denied_reason="",
            latency_ms=latency_ms,
            group_id=decision.group_id,
        )

        answered = intent.action == ACTION_ANSWER
        return LivePilotResult(
            handled=not answered,       # ANSWER -> LLM fallback answers
            reply=final_reply,
            denied_reason="llm_answer" if answered else "",
            chain=chain, evidence=evidence,
        )

    # -- read-only answer -----------------------------------------------------

    def _answer(self, content: str) -> str:
        """Read-only fallback: a bounded, safe, NON-hallucinating answer.

        The pilot runtime does not own an LLM; it returns a controlled,
        factual acknowledgement and routes the question to the host bot's
        existing answer path (the caller decides).  This method never claims
        to have executed anything (P8.5 false-promise guard).
        """
        snippet = content[:120]
        return (
            "收到你的问题（仅试点群内由执行桥受理）："
            f"「{snippet}」。该问题已转交回答通道，请稍候。"
        )
