"""P8.9 — Real Wiring: OneBot11Adapter -> PilotRuntime, with LLM fallback.

Wires the real QQ event flow so the pilot runtime is tried FIRST and the
LLMHandler remains the fallback:

    NapCat WS -> OneBot11Adapter -> EventBus.publish(event.message.group)
                                         |
                     +--------------------+-------------------+
                     |                                        |
              PilotRuntime (front)                    LLMHandler (fallback)
              decides "handled?"                        replies only when the
              replies via gateway                        pilot did NOT handle
                     |                                        |
                     +------------------+---------------------+
                                        v
                                 one reply per message
                                  (no double-reply)

Why a coordinator (not two plain subscribers): ``EventBus.publish`` runs all
handlers CONCURRENTLY (asyncio.gather) — a plain "Pilot first, LLM second"
ordering cannot prevent the LLM from also replying.  The coordinator owns the
"pilot handled?" decision and hands the fallback ONLY the events the pilot
did not handle.  The LLM handler itself is unchanged (fallback preserved).

Kill switch: when the pilot switch is DISABLED, the coordinator marks the
event as "not handled" and EVERYTHING falls through to the LLM — i.e. the
pilot path is fully inert, the bot behaves exactly as before (fail closed
for pilot, zero behavior change for the host bot).

Scope limits (P8.0/P8.9): no System B, no capability_mode, no env bypass,
no auto-join, no auto-DM; the coordinator never executes anything itself.
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional

from anima.pilot.pilot_runtime import LivePilotConfig, PilotRuntime

__all__ = [
    "PilotCoordinator",
    "build_pilot_coordinator",
    "should_llm_handle",
]

#: the event topic the coordinator listens on (group chat only, P8.0).
_GROUP_TOPIC = "event.message.group"


class PilotCoordinator:
    """Decide "pilot handled?" for each real group event; fall back to LLM.

    ``reply_fn`` is the async gateway send callback: (detail_type, target_id,
    message) -> message_id|None.  ``fallback_fn`` is the async LLM handler.
    """

    def __init__(
        self,
        runtime: PilotRuntime,
        *,
        reply_fn: Callable[..., Awaitable[Any]],
        fallback_fn: Callable[..., Awaitable[Any]],
        min_fallback_delay: float = 0.0,
    ) -> None:
        self._runtime = runtime
        self._reply = reply_fn
        self._fallback = fallback_fn
        self._min_fallback_delay = min_fallback_delay
        self._handled_count = 0
        self._fallback_count = 0
        self._denied_count = 0
        self._lock = asyncio.Lock()

    # -- stats (monitoring) -------------------------------------------------

    async def stats(self) -> dict:
        async with self._lock:
            return {
                "pilot_handled": self._handled_count,
                "llm_fallback": self._fallback_count,
                "silent_denied": self._denied_count,
            }

    # -- main handler ---------------------------------------------------------

    async def on_group_event(self, event: Optional[dict]) -> None:
        """Try the pilot runtime; fall back to the LLM ONLY when appropriate.

        Fail-closed decision (P9.5):
          * pilot handled with a reply (deny message / execute result) -> send
          * pilot handled without a reply -> consumed, send nothing
          * denied_reason == llm_answer   -> gate passed, LLM answers for real
          * denied_reason == kill_switch_disabled -> pilot off, bot resumes
            its normal LLM behaviour
          * any other denial (group_not_allowed / passive_not_triggered /
            policy_disabled / ...) -> SILENT: never fall back to the LLM
            (P8.2 whitelist + PASSIVE + fail closed must hold)
        """
        result = self._runtime.handle(event)
        if result.handled:
            if result.reply:
                async with self._lock:
                    self._handled_count += 1
                detail_type = "group"
                target_id = int(event.get("group_id") or 0)
                await self._reply(detail_type, target_id, result.reply)
            return
        # not handled by the pilot -> decide between LLM answer and silence
        if result.denied_reason in ("llm_answer", "kill_switch_disabled"):
            if self._min_fallback_delay > 0:
                await asyncio.sleep(self._min_fallback_delay)
            async with self._lock:
                self._fallback_count += 1
            await self._fallback(event)
            return
        # fail-closed denial: silent (never bypass the whitelist/PASSIVE)
        async with self._lock:
            self._denied_count += 1

    async def on_group_event_safe(self, event: Optional[dict]) -> None:
        """Isolated wrapper: a coordinator failure never breaks the bus."""
        try:
            await self.on_group_event(event)
        except Exception:  # noqa: BLE001 - degradation, never crash dispatch
            import logging

            logging.getLogger("anima.pilot.wiring").exception(
                "pilot coordinator error, falling back to LLM"
            )
            try:
                await self._fallback(event)
            except Exception:  # noqa: BLE001
                logging.getLogger("anima.pilot.wiring").exception(
                    "fallback LLM handler also failed"
                )


def should_llm_handle(event: Optional[dict], core: Any) -> bool:
    """P9.5: decide whether the STANDALONE LLM handler may process a message.

    When a pilot coordinator is wired, GROUP traffic is owned by the
    coordinator (gate -> pilot -> LLM fallback).  The standalone LLM handler
    (an independent EventBus subscriber) must NOT also process group events:
      * double reply on whitelisted groups (coordinator fallback + handler)
      * whitelist bypass on non-whitelisted groups (coordinator is silent but
        the handler would still answer)
    Private traffic keeps the normal LLM path.  With no coordinator wired the
    behaviour is unchanged (LLM-only).
    """
    if (
        event is not None
        and event.get("topic") == "event.message.group"
        and getattr(core, "pilot_coordinator", None) is not None
    ):
        return False
    return True


def build_pilot_coordinator(
    *,
    config: Optional[LivePilotConfig] = None,
    data_dir: str,
    core: Any,
    pilot_entry: Any = None,
    kill_switch: Any = None,
    reply_fn: Optional[Callable[..., Awaitable[Any]]] = None,
    fallback_fn: Optional[Callable[..., Awaitable[Any]]] = None,
    metrics_log: Optional[str] = None,
    identity_policy: Any = None,
) -> PilotCoordinator:
    """Build the coordinator wired to a real core (cli start path)."""
    from anima.pilot.pilot_runtime import PilotRuntime

    runtime = PilotRuntime(
        config=config or LivePilotConfig(),
        data_dir=data_dir,
        pilot_entry=pilot_entry,
        kill_switch=kill_switch,
        metrics_log=metrics_log,
        identity_policy=identity_policy,
    )
    reply = reply_fn or (lambda dt, tid, msg: core.gateway.send(
        __import__("anima.core.gateway", fromlist=["OutboundMsg"]).OutboundMsg(
            dt, tid, msg
        )
    ))
    fallback = fallback_fn or (lambda ev: core.llm_handler.handle(ev))
    return PilotCoordinator(runtime, reply_fn=reply, fallback_fn=fallback)
