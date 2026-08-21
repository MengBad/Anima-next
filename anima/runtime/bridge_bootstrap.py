"""P7.6 — Controlled Execution Bridge Bootstrap (TEST-ONLY wiring helper).

Builds the controlled F5 wiring

    PlanOrchestrator -> RealTaskExecutor -> CapabilityResolver -> sandbox.write
        -> ExecutionLedger

with a caller-injected sandbox root.  This helper is the *test-only* bootstrap
for proving the wiring end-to-end; it is NOT a production entry point:

  * it never attaches to a production caller,
  * it never enables capability_mode / gateway,
  * it never touches System B / tools / governance,
  * passing ``executor=None`` keeps the legacy ExecutionRuntime fallback
    untouched (proving wiring does not change legacy behavior).

Scope limits (P7.6 authorization): modify anima/runtime/ + tests/ only; no
RealTaskExecutor signature change; no new provider; no production enablement.
"""
from __future__ import annotations

from typing import Any, Optional

from anima.runtime.capability_resolver import (
    CapabilityContract,
    CapabilityRegistry,
    CapabilityResolver,
)
from anima.runtime.execution_ledger import ExecutionLedger
from anima.runtime.orchestrator import PlanOrchestrator
from anima.runtime.providers.sandbox_write_provider import SandboxWriteProvider
from anima.runtime.real_task_executor import RealTaskExecutor

__all__ = ["build_controlled_orchestrator"]

#: sentinel distinguishing "not provided" from an explicit ``None``.
_UNSET = object()


def build_controlled_orchestrator(
    sandbox_root: Optional[str],
    *,
    planning: Any = None,
    executor: Any = _UNSET,
    ledger: Optional[ExecutionLedger] = None,
    return_parts: bool = False,
) -> Any:
    """Build a controlled PlanOrchestrator wired to sandbox.write (test-only).

    - ``executor=_UNSET`` (default): construct the sandbox.write RealTaskExecutor
      (single provider domain, sandbox root injected by the caller).
    - ``executor=None``: keep the legacy path (PlanOrchestrator falls back to
      ExecutionRuntime) — proves wiring does not change legacy behavior.
    - ``sandbox_root`` is always caller-controlled (tests use a temp dir).

    Returns ``PlanOrchestrator``, or ``(orchestrator, (executor, ledger))`` when
    ``return_parts=True``.
    """
    if ledger is None:
        ledger = ExecutionLedger()  # F5.1 memory-only mode

    if executor is _UNSET:
        registry = CapabilityRegistry()
        registry.register(
            CapabilityContract(
                capability_id="sandbox.write",
                side_effect=True,
                idempotent=True,
            ),
            SandboxWriteProvider(str(sandbox_root)),
        )
        executor = RealTaskExecutor(resolver=CapabilityResolver(registry))

    orchestrator = PlanOrchestrator(
        planning=planning,
        executor=executor if executor is not _UNSET else None,
        ledger=ledger,
    )
    if return_parts:
        return orchestrator, (executor, ledger)
    return orchestrator
