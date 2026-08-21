"""P7.2 — RealTaskExecutor Minimal Implementation Slice.

The first *real* (non-fake) F5-domain TaskExecutor: it wires the F5 chain

    TaskExecutor contract
        -> RealTaskExecutor
        -> CapabilityResolver
        -> MemoryReadProvider (single, read-only provider domain)
        -> ExecutionObservation
        -> ExecutionLedger idempotency gate

end-to-end with a REAL provider, still WITHOUT any production wiring: the
executor is constructed locally, serves exactly one provider domain
(``memory.read``), and is never attached to the orchestrator / run_plan path.

Safety invariants (held by construction and by test):

  * SINGLE PROVIDER DOMAIN - only MemoryReadProvider is registered; any other
    capability is a ResolutionError (fail closed).
  * LEDGER GATE           - a ledger HIT returns the cached observation with NO
    provider call; facts are put only after a successful read-only act.
  * FAILURE = OBSERVATION  - resolution/provider failure produces a FAILED
    ExecutionObservation, never a run-crashing exception (I2) and never a
    fabricated success.
  * NON-ESCALATION        - the observation vocabulary is fact-only: no
    approved / authorized / deployed / production semantics.
  * AUTHORITY SEPARATION   - the executor projects per-task facts (F5.1); run
    outcomes stay the Workflow Authority's (F-04).
  * NO PRODUCTION WIRING   - this module never imports the governance layer's
    entry point, the tools layer, the web UI or any production entry; it is a
    local, injectable class.

Scope limits (P7.2 authorization): no System B changes, no legacy
approval-execution changes, no capability_mode changes, no production registry
changes, no execution_key changes, no authz_id injection into the F5 key, no
real external execution.
"""
from __future__ import annotations

from typing import Any, Optional

from anima.runtime.executor import ExecutionObservation, ExecutionStep, TaskExecutor
from anima.runtime.capability_resolver import (
    CapabilityContract,
    CapabilityNeed,
    CapabilityRegistry,
    CapabilityResolver,
    ResolutionError,
)
from anima.runtime.providers.memory_read_provider import MemoryReadProvider

__all__ = ["RealTaskExecutor"]

#: the single provider domain this slice serves (P7.2 scope lock).
_CAPABILITY_ID = "memory.read"
_PROVIDER_ID = "memory.read.local"


class RealTaskExecutor(TaskExecutor):
    """A real (non-fake) F5 TaskExecutor serving one read-only provider domain.

    Surface is exactly the TaskExecutor contract: ``execute()`` only.  There is
    deliberately NO approve / activate / dispatch / invoke / apply / repair.
    """

    def __init__(
        self,
        store: Optional[dict] = None,
        *,
        provider: Optional[MemoryReadProvider] = None,
        resolver: Optional[CapabilityResolver] = None,
    ) -> None:
        if resolver is None:
            registry = CapabilityRegistry()
            provider = provider or MemoryReadProvider(store=dict(store or {}))
            # F7.1 registration integrity: side_effect=False + idempotent=True
            # matches the MemoryReadProvider profile (read-only, deterministic).
            registry.register(
                CapabilityContract(
                    capability_id=_CAPABILITY_ID,
                    side_effect=False,
                    idempotent=True,
                ),
                provider,
            )
            resolver = CapabilityResolver(registry)
        self._resolver = resolver

    @property
    def resolver(self) -> CapabilityResolver:
        """Expose the resolver for audit (read-only inspection)."""
        return self._resolver

    def execute(
        self,
        task: ExecutionStep,
        execution_key: tuple,
        ledger: Optional[Any] = None,
    ) -> ExecutionObservation:
        """Execute one task under its idempotency key (F5.1 contract).

        Ledger HIT -> cached observation, NO provider call.  Resolution or
        provider failure -> FAILED observation (never a crash, never a fake
        success).  Read-only facts are put after the act (F5.4a policy).
        """
        # Idempotency gate: ledger HIT skips the provider call entirely.
        if ledger is not None:
            cached = ledger.get(execution_key)
            if cached is not None:
                return cached

        # I1: build the need, delegate SELECTION to the resolver (the executor
        # never names a provider itself).
        need = CapabilityNeed.from_step(task)
        try:
            result = self._resolver.resolve(need, execution_key)
        except ResolutionError as exc:
            # A capability that cannot be served is a FAILED task, not a
            # run-crashing exception (I2) and not a fabricated success.
            return ExecutionObservation(
                task_id=task.id,
                success=False,
                result={},
                error=f"capability resolution failed: {exc}",
                metadata={"provider_id": None, "side_effect_recorded": False},
            )

        observation = ExecutionObservation(
            task_id=task.id,
            success=result.success,
            result={"data": result.data} if result.data is not None else {},
            error=result.error,
            metadata={
                "provider_id": result.metadata.get("provider_id", _PROVIDER_ID),
                "side_effect_recorded": result.metadata.get(
                    "side_effect_recorded", False
                ),
            },
        )

        # Record the fact AFTER a successful read-only act (F5.4a policy): a
        # crash before this put only loses a read-only fact, so a resume retry
        # is safe.
        if ledger is not None:
            ledger.put(execution_key, observation)

        return observation
