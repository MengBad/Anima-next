"""P7.10/P7.11 — Production Construction Boundary + Kill Switch.

Controlled factory + kill switch for the F5 Execution Bridge, WITHOUT enabling
production traffic:

  * P7.10: ``build_pilot_bridge`` — config-driven controlled factory (default
    DISABLED, no env bypass, never touches capability_mode / System B / tools).
  * P7.11: ``build_production_bridge`` — the UNIQUE production construction
    boundary: config -> kill switch -> PlanOrchestrator(executor) ->
    sandbox.write -> durable ledger.  ``KillSwitchExecutor`` consumes the kill
    switch at the single check point BEFORE ``executor.execute()``.

Scope limits (P7.10/P7.11 authorization): no production traffic, no user
traffic, no capability activation, no gateway dependency, no provider expansion,
no multi-construction points.
"""
from __future__ import annotations

from typing import Any, Optional

from anima.core.persistence import PersistenceProvider
from anima.runtime.capability_resolver import (
    CapabilityContract,
    CapabilityRegistry,
    CapabilityResolver,
)
from anima.runtime.execution_ledger import ExecutionLedger
from anima.runtime.executor import TaskExecutor
from anima.runtime.orchestrator import PlanOrchestrator
from anima.runtime.providers.sandbox_write_provider import SandboxWriteProvider
from anima.runtime.real_task_executor import RealTaskExecutor

__all__ = [
    "BridgeDisabledError",
    "BridgeKillSwitch",
    "KillSwitchExecutor",
    "build_pilot_bridge",
    "build_production_bridge",
    "_PILOT_MODES",
]

#: pilot boundary states (OFF default / CONTROLLED — NEVER PRODUCTION in this slice).
_PILOT_MODES = ("OFF", "CONTROLLED")


class BridgeDisabledError(RuntimeError):
    """Raised when the execution bridge is DISABLED (fail closed)."""


class BridgeKillSwitch:
    """Minimal F5-domain kill switch: ENABLED / DISABLED, default DISABLED.

    Local state only — no remote toggle, no System B control plane, no gateway
    dependency.  ``check()`` raises when DISABLED (fail closed).
    """

    def __init__(self, enabled: bool = False) -> None:
        self._enabled = bool(enabled)

    @property
    def state(self) -> str:
        return "ENABLED" if self._enabled else "DISABLED"

    def check(self) -> None:
        """Fail closed: raise unless the bridge is ENABLED."""
        if not self._enabled:
            raise BridgeDisabledError("execution bridge is DISABLED (fail closed)")

    def enable(self) -> None:
        """Local, explicit enable (never derived from the environment)."""
        self._enabled = True

    def disable(self) -> None:
        """Local, explicit disable."""
        self._enabled = False


class KillSwitchExecutor(TaskExecutor):
    """P7.11: consumes the kill switch at the SINGLE check point.

    ``execute()`` checks the switch BEFORE delegating to the inner executor —
    DISABLED raises (fail closed: no provider call, no ledger write); ENABLED
    proceeds into the F5 chain.  Identity / execution_key are never mutated by
    the toggle.
    """

    def __init__(self, inner: TaskExecutor, switch: BridgeKillSwitch) -> None:
        self._inner = inner
        self._switch = switch

    def execute(self, task: Any, execution_key: tuple, ledger: Any = None) -> Any:
        self._switch.check()  # single check point, before any execution
        return self._inner.execute(task, execution_key, ledger)


def _construct_bridge(data_dir: str) -> tuple[RealTaskExecutor, ExecutionLedger]:
    """THE single F5-bridge construction point (P7.11: no multi-construction).

    Durable ledger (F5.2) + sandbox.write single-provider executor with a
    config-derived sandbox root (P7.8 G2: data_dir/sandbox_write).
    """
    ledger = ExecutionLedger(persistence=PersistenceProvider(data_dir))
    registry = CapabilityRegistry()
    registry.register(
        CapabilityContract(
            capability_id="sandbox.write",
            side_effect=True,
            idempotent=True,
        ),
        SandboxWriteProvider(sandbox_root=None, data_dir=data_dir),
    )
    executor = RealTaskExecutor(resolver=CapabilityResolver(registry))
    return executor, ledger


def build_pilot_bridge(
    config: Optional[dict],
    *,
    data_dir: str,
    planning: Any = None,
) -> tuple[BridgeKillSwitch, Optional[PlanOrchestrator]]:
    """Config-driven controlled factory (P7.10 L3).

    ``config = {"execution_bridge": {"enabled": bool}}`` — default DISABLED.
    When DISABLED the factory returns ``(switch, None)`` and the legacy path is
    untouched.  When ENABLED it builds the F5 bridge through the single
    construction point ``_construct_bridge``.  ``planning`` (optional) is
    injected into the orchestrator (production: the Planning Authority's
    interface); executor/ledger/provider remain the single construction point.
    """
    section = (config or {}).get("execution_bridge") or {}
    enabled = bool(section.get("enabled", False))
    switch = BridgeKillSwitch(enabled=enabled)
    if not enabled:
        return switch, None
    executor, ledger = _construct_bridge(data_dir)
    return switch, PlanOrchestrator(planning=planning, executor=executor, ledger=ledger)


def build_production_bridge(
    config: Optional[dict],
    *,
    data_dir: str,
    planning: Any = None,
) -> tuple[BridgeKillSwitch, Optional[PlanOrchestrator], str]:
    """P7.11: the UNIQUE production construction boundary.

    Returns ``(switch, orchestrator|None, mode)``:

    * config absent / disabled -> ``(DISABLED switch, None, "OFF")`` — legacy
      path untouched, zero construction;
    * config explicit ``{"execution_bridge": {"enabled": true}}`` -> builds the
      F5 bridge through ``_construct_bridge`` (single point), wraps the executor
      with ``KillSwitchExecutor`` (kill switch consumed before every execute),
      and reports ``mode="CONTROLLED"`` — NEVER "PRODUCTION" in this slice.
      ``planning`` (optional) is injected like in ``build_pilot_bridge``.
    """
    section = (config or {}).get("execution_bridge") or {}
    enabled = bool(section.get("enabled", False))
    switch = BridgeKillSwitch(enabled=enabled)
    mode = "CONTROLLED" if enabled else "OFF"
    if not enabled:
        return switch, None, mode
    inner, ledger = _construct_bridge(data_dir)
    executor = KillSwitchExecutor(inner, switch)
    return switch, PlanOrchestrator(planning=planning, executor=executor, ledger=ledger), mode
