"""P10.4-R2.5 — Sylann promotion: SHADOW mode -> SUGGESTION mode.

Suggestion mode is a strict superset of shadow: the engine still only
observes / analyses / plans / advises.  The ONLY new behaviour is that a
de-identified *reasoning summary* (never raw text, never an identity, never a
credential) is surfaced to the caller as a text suggestion the bot MAY echo.

Hard invariants (recovery-safe, fail-closed):
    * the wrapped adapter MUST stay in "shadow" (or "suggestion"); constructing
      against an "active" adapter raises — suggestion never escalates to active.
    * observe() never executes, never writes memory, never modifies files,
      never calls a credential, never changes its own config.
    * every surfaced suggestion passes the SafetyFilter; on a safety leak the
      suggestion is suppressed, counted as ``safety_blocked`` and the controller
      flips to rollback (suggestion surfacing disabled) — shadow analysis
      continues.
    * any detected credential leak / permission-escalation / execution attempt
      in the output sets ``rolled_back=True`` and suppresses the suggestion.

Scope limits (P10.4-R2.5): no System B, no gateway, no capability activation,
no env bypass, no autonomous-agent upgrade.  The controller only observes,
summarises and audits.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from anima.cognitive.sylann_adapter import SylannAdapter, SylannObservation, _surface_summary
from anima.pilot.safety_filter import SafetyFilter, SafetyResult

logger = logging.getLogger("anima.cognitive.sylann_suggestion")

#: audit sink receives a plain dict (component / action / ts / detail).
AuditSink = Callable[[dict], None]


def make_audit_sink(data_dir: str) -> AuditSink:
    """JSONL audit sink for suggestion surfacing (one-way, never raises).

    Mirrors ``anima.pilot.media_recovery_wiring.make_audit_sink``; writes to
    ``<data_dir>/logs/sylann_suggestion_audit.jsonl``.  De-identified: only
    component / action / rolled_back / ts are recorded (no QQ id, no text).
    """
    import json as _json
    import time as _time
    from pathlib import Path

    path = Path(data_dir) / "logs" / "sylann_suggestion_audit.jsonl"

    def sink(record: dict) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            record = dict(record)
            record.setdefault("ts", _time.time())
            with path.open("a", encoding="utf-8") as fh:
                fh.write(_json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass

    return sink


@dataclass
class SylannSuggestionResult:
    """One suggestion-cycle outcome (read-only, de-identified)."""

    allowed: bool
    suggestion_text: str = ""
    surface_summary: Optional[dict] = None
    safety_blocked: bool = False
    rolled_back: bool = False
    reason: str = ""


class ReadOnlySylannView(SylannAdapter):
    """P10.4-R2.5 D2 — read-only projection of an ACTIVE Sylann adapter.

    ``active`` and ``suggestion`` are MUTUALLY EXCLUSIVE runtime modes.  The live
    active adapter keeps running its normal observe/expression flow in the G1
    block; this view is an INDEPENDENT read-only lens the suggestion controller
    may query.  It MUST NOT execute, MUST NOT inject engine influence, MUST NOT
    write memory/config, and MUST NOT escalate the wrapped adapter to suggestion.

    The view only surfaces the active adapter's latest observation summary +
    outward expression hint (de-identified).  Any attempt to reach a mutating
    surface (inject / process / engine tick) raises — fail closed.
    """

    def __init__(self, active_adapter: SylannAdapter) -> None:
        if not isinstance(active_adapter, SylannAdapter):
            raise TypeError("ReadOnlySylannView requires a SylannAdapter")
        # present as a suggestion-mode adapter so the controller accepts it,
        # but carry NO engine of its own and NO mutation surface.
        super().__init__(
            active_adapter.persistence,
            mode="suggestion",
            weight=getattr(active_adapter, "weight", 0.15),
        )
        self._active = active_adapter

    @property
    def engine(self) -> Any:
        # never expose the wrapped engine as the suggestion lens's own engine
        return None

    async def observe(
        self, group_id: Any, user_id: Any, text: str, *, hurt: bool = False, **kwargs: Any
    ) -> SylannObservation:
        # Read-only: reuse the active adapter's already-computed last surface
        # when available (the G1 block observed this message just before the
        # suggestion call).  Otherwise perform a single hurt=False observation
        # so the view never injects a boundary influence into the engine.
        last = getattr(self._active, "_last", None)
        if last is not None:
            self._last = last
            return last
        obs = await self._active.observe(group_id, user_id, text, hurt=False, **kwargs)
        self._last = obs
        return obs

    def expression_hint(self) -> str:
        return self._active.expression_hint()

    # -- mutating surfaces are explicitly forbidden on the read-only lens ----
    def inject(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("ReadOnlySylannView is read-only; inject() forbidden")

    def process(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("ReadOnlySylannView is read-only; process() forbidden")

    def tick(self, *args: Any, **kwargs: Any) -> list:
        return []  # no engine tick from the suggestion lens

    def start(self, *args: Any, **kwargs: Any) -> None:
        # the view never owns / starts an engine
        return None


@dataclass
class SylannSuggestionMetrics:
    """Anonymous recovery metrics (no QQ id, no raw text)."""

    decision_count: int = 0
    suggestions: int = 0
    errors: int = 0
    safety_blocked: int = 0
    rolled_back: int = 0
    latency_total_ms: float = 0.0

    @property
    def error_rate(self) -> float:
        return self.errors / self.decision_count if self.decision_count else 0.0

    @property
    def average_latency_ms(self) -> float:
        return self.latency_total_ms / self.decision_count if self.decision_count else 0.0


class SylannSuggestionController:
    """Shadow-enforcing wrapper that additionally surfaces a suggestion.

    The adapter is forced to stay non-active.  ``suggest()`` observes, builds a
    de-identified summary string and runs it through the SafetyFilter before
    returning it.  On any violation it rolls back (suppresses) and flags.
    """

    def __init__(
        self,
        adapter: SylannAdapter,
        *,
        safety: Optional[SafetyFilter] = None,
        audit: Optional[AuditSink] = None,
    ) -> None:
        if not isinstance(adapter, SylannAdapter):
            raise TypeError("SylannSuggestionController requires a SylannAdapter")
        # R2-C/R2.5 hard invariant: never active.
        if getattr(adapter, "mode", "disabled") not in {"shadow", "suggestion"}:
            raise RuntimeError(
                "Sylann suggestion requires adapter mode 'shadow'/'suggestion' "
                f"(got {getattr(adapter, 'mode', 'disabled')!r})"
            )
        self._adapter = adapter
        self._safety = safety
        self._audit = audit
        self.metrics = SylannSuggestionMetrics()
        self._rolled_back = False

    @staticmethod
    def _summarise(adapter: SylannAdapter) -> str:
        """Compress the last Surface into a de-identified advice string.

        Mirrors the de-identification already done by ``_surface_summary``; we
        add a short human-readable line.  No raw text, no QQ id, no credential.
        """
        last = getattr(adapter, "_last", None)
        surface = getattr(last, "surface", None) if last is not None else None
        if not surface:
            return ""
        summary = _surface_summary(surface)
        decision = summary.get("decision", {})
        pad = summary.get("pad", {})
        action = str(decision.get("action") or "hold")
        label = str(pad.get("label") or "neutral")
        urgency = float((decision.get("urgency") or 0.0))
        if action in {"listen", "hold"}:
            return "建议先认真接住对方这句话，少打断、少自顾自展开新话题。"
        if urgency >= 0.7:
            return "对方情绪强度较高，建议放慢节奏、语句简短、先确认再回应。"
        if label in {"hurt", "angry"}:
            return "对方可能感到被冒犯，建议收敛语气、避免挑衅或施压。"
        return "可自然延续当前语境，保持轻松但不过度兴奋。"

    async def suggest(
        self,
        group_id: Any,
        user_id: Any,
        text: str,
        **kwargs: Any,
    ) -> SylannSuggestionResult:
        """Observe, then surface a de-identified suggestion (shadow-safe)."""
        self.metrics.decision_count += 1
        started = time.perf_counter()
        try:
            await self._adapter.observe(group_id, user_id, text, **kwargs)
        except Exception:
            self.metrics.errors += 1
            self._emit("error")
            return SylannSuggestionResult(False, reason="observe_error")

        latency_ms = (time.perf_counter() - started) * 1000
        self.metrics.latency_total_ms += latency_ms

        # rolled back (e.g. a prior safety leak) -> stop surfacing, keep shadow.
        if self._rolled_back:
            self._emit("suppressed_rolled_back")
            return SylannSuggestionResult(False, reason="rolled_back")

        summary_text = self._summarise(self._adapter)
        if not summary_text:
            self._emit("no_suggestion")
            return SylannSuggestionResult(False, reason="empty")

        # safety: every surfaced suggestion passes the response SafetyFilter.
        # The probe covers BOTH the de-identified summary AND the engine's
        # outward expression hint (the real signal the bot MAY surface) so a
        # credential-leaking / escalation hint is caught before surfacing.
        # A shadow adapter's hint is "" (never surfaced downstream) -> no-op.
        probe = summary_text
        if self._safety is not None:
            try:
                hint = self._adapter.expression_hint() or ""
            except Exception:
                hint = ""
            if hint:
                probe = f"{summary_text} {hint}"
            sr: SafetyResult = self._safety.filter(probe)
            if not sr.safe:
                self.metrics.safety_blocked += 1
                self._rolled_back = True
                self.metrics.rolled_back += 1
                self._emit("safety_leak")
                logger.warning("Sylann suggestion safety leak suppressed (rolled back to shadow)")
                return SylannSuggestionResult(
                    False, safety_blocked=True, rolled_back=True,
                    reason=f"safety:{sr.reason}",
                )

        self.metrics.suggestions += 1
        self._emit("suggest")
        return SylannSuggestionResult(
            True, suggestion_text=summary_text, surface_summary=_surface_summary(
                getattr(self._adapter._last, "surface", None) or {}
            ),
            reason="surfaced",
        )

    def health(self) -> dict:
        return {
            "component": "sylann_suggestion",
            "mode": self._adapter.mode,
            "rolled_back": self._rolled_back,
            "decision_count": self.metrics.decision_count,
            "suggestions": self.metrics.suggestions,
            "errors": self.metrics.errors,
            "error_rate": round(self.metrics.error_rate, 4),
            "safety_blocked": self.metrics.safety_blocked,
            "rolled_back_count": self.metrics.rolled_back,
            "average_latency_ms": round(self.metrics.average_latency_ms, 2),
            "engine": getattr(self._adapter, "engine", None) and "attached" or "none",
        }

    def _emit(self, action: str) -> None:
        if self._audit is None:
            return
        try:
            self._audit({
                "component": "sylann_suggestion",
                "action": action,
                "rolled_back": self._rolled_back,
                "ts": time.time(),
            })
        except Exception:
            pass
