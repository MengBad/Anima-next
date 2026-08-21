"""P9.1 canary wiring — real QQ traffic entry for the canary environment.

Layers the canary-specific entry on top of the P8.9 coordinator:

    NapCat (OneBot11 WS)
        -> OneBot11Adapter (existing, untouched)
        -> EventBus "event.message.group"
        -> PilotCoordinator.on_group_event_safe   (P9.1: canary wiring)
             -> PilotRuntime (EntryGate -> Identity -> Evidence -> Safety
                              -> Permission -> [F5 execution | answer])
             -> LLMHandler fallback (only when the pilot did NOT handle)

Everything here is config-driven and **default OFF**: with no ``pilot``
section (or ``enabled: false``) the coordinator is never built and the bot
behaves exactly as before (fail closed for the pilot, zero behavior change
for the host bot).

Boundaries (P9.1):
  * only independent canary data dirs are accepted (system / production /
    test roots refused) — the canary must never share a production store.
  * no System B import, no gateway/capability activation, no new provider.
  * the kill switch stays effective: DISABLED -> pilot fully inert.

The P8.9 ``cli.py`` wiring already reads the raw yaml ``pilot`` section;
this module is the reusable, testable canary composition used by that block
(and by the canary deployment script).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

__all__ = [
    "CanaryWireSpec",
    "load_canary_pilot_config",
    "validate_canary_data_dir",
    "build_canary_coordinator",
]

#: data roots that must never host a canary store (production / test / system
#: dirs).  The filesystem root itself is handled separately via the anchor
#: check (any absolute path's parents contain the root, so it can't be listed).
_FORBIDDEN_ROOTS = (
    Path("/etc"),
    Path("/usr"),
    Path("/home"),
    Path("/var"),
    Path("/opt/anima"),        # production
    Path("/opt/anima-test"),   # earlier VPS test env (P7.17-A)
)


@dataclass(frozen=True, slots=True)
class CanaryWireSpec:
    """Parsed canary pilot config (immutable, fail-closed)."""

    enabled: bool
    config: Any        # LivePilotConfig (allowed_groups / max_groups / ...)
    execution_bridge: dict  # raw execution_bridge section (may be empty)


def load_canary_pilot_config(raw_yaml: Optional[dict]) -> Optional[CanaryWireSpec]:
    """Parse the raw yaml ``pilot`` section; None when not enabled (OFF).

    Fail closed: absent section / enabled:false -> None -> coordinator absent
    -> LLM-only behavior, exactly like today.
    """
    raw = raw_yaml or {}
    pilot = raw.get("pilot") or {}
    if not bool(pilot.get("enabled", False)):
        return None
    from anima.pilot.pilot_runtime import LivePilotConfig

    config = LivePilotConfig.from_dict(pilot.get("gate") or {})
    return CanaryWireSpec(
        enabled=True,
        config=config,
        execution_bridge=dict(pilot.get("execution_bridge") or {}),
    )


def validate_canary_data_dir(data_dir: str) -> None:
    """Fail closed: refuse system / production / test data roots.

    The canary must live in its own independent directory (e.g.
    ``/opt/production-canary/data``); sharing a store with production or an
    earlier test environment is never allowed.  Raises ValueError otherwise.
    """
    if not data_dir or not str(data_dir).strip():
        raise ValueError("canary data_dir must not be empty")
    path = Path(str(data_dir))
    resolved = path.resolve()
    # a filesystem root itself (/, C:\, ...) is always forbidden
    if str(resolved.anchor) == str(resolved):
        raise ValueError(
            f"canary data_dir {resolved!s} is a filesystem root — use an "
            f"independent canary directory"
        )
    for bad in _FORBIDDEN_ROOTS:
        bad_resolved = bad.resolve()
        if resolved == bad_resolved or bad_resolved in resolved.parents:
            raise ValueError(
                f"canary data_dir {resolved!s} resolves under a forbidden "
                f"root ({bad_resolved!s}) — use an independent canary directory"
            )


def build_canary_coordinator(
    *,
    spec: Optional[CanaryWireSpec],
    data_dir: str,
    core: Any,
    kill_switch: Any = None,
    reply_fn: Optional[Callable[..., Any]] = None,
    fallback_fn: Optional[Callable[..., Any]] = None,
) -> Optional[Any]:
    """Build the canary coordinator, or None when the pilot is OFF.

    When ``spec`` is None (pilot OFF) nothing is built and the core is left
    untouched (LLM-only).  When enabled, the data dir is validated and the
    coordinator is built, registered on ``core.pilot_coordinator`` and wired
    to the group-event topic.  A missing/disabled kill switch still builds a
    coordinator whose runtime gate fails closed (no group is whitelisted by
    default), so an operator who forgets the switch does NOT accidentally
    open traffic.
    """
    if spec is None:
        return None
    validate_canary_data_dir(data_dir)
    from anima.pilot.wiring import build_pilot_coordinator

    coordinator = build_pilot_coordinator(
        config=spec.config,
        data_dir=data_dir,
        core=core,
        pilot_entry=None,
        kill_switch=kill_switch,
        reply_fn=reply_fn,
        fallback_fn=fallback_fn,
    )
    core.pilot_coordinator = coordinator
    core.bus.subscribe("event.message.group", coordinator.on_group_event_safe)
    return coordinator
