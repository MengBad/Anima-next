"""ResolvingTaskExecutor (F5.4b) — the F5-native TaskExecutor wiring Resolver → Provider.

This is the ONE new executor that proves the F5.4a chain works end-to-end through the
existing F5.3 orchestrator path (``_dispatch_task`` calls ``executor.execute(step, key,
ledger)``). It does NOT modify ``executor.py`` / ``orchestrator.py`` / ``ledger.py`` — it is
a pure subclass of the frozen ``TaskExecutor`` contract.

Frozen invariants enforced here:

- **I1 (Executor ≠ Capability Selector):** this executor holds NO provider list. It builds
  a ``CapabilityNeed`` from the task and hands it to the injected ``CapabilityResolver``,
  which chooses the provider. The executor only projects the result.
- **I2 (Provider ≠ Workflow Authority):** the executor turns the ``ProviderResult`` into a
  pure ``ExecutionObservation``. It never writes run/task state (inherited from the
  ``TaskExecutor`` contract) — the orchestrator reports the observation.
- **I3 (execution identity unchanged):** the idempotency key ``(run_id, task_id, attempt)``
  is passed verbatim through ``resolve`` → ``provider.invoke``. The executor never
  redefines it. It also gates execution on the ledger BEFORE acting (mirrors
  ``FakeTaskExecutor``): on a hit the cached observation is returned with NO provider call.

Side-effect recording policy (F5.4a, corrected by F5.7 C4):
- read-only / deterministic providers (``side_effect=False``): the executor records the
  fact in the ledger AFTER the provider returns (a crash before ``put`` only loses a
  read-only fact → safe retry).
- side-effecting providers (``side_effect=True``): the LEDGER GATE belongs to the
  EXECUTOR (this class), NOT the Provider — I2 forbids the Provider from touching the
  ledger. The executor's HIT gate (``ledger.get`` before resolve) prevents a blind
  re-invoke; the provider only PROPAGATES the execution_key as the external idempotency
  token, and the external system performs the authoritative dedupe (F5.7 C2 / C4).
"""
from __future__ import annotations

from typing import Optional

from anima.constitution.execution import ExecutionStep
from anima.runtime.capability_resolver import (
    CapabilityNeed,
    CapabilityResolver,
    ResolutionError,
)
from anima.runtime.executor import ExecutionObservation, TaskExecutor
from anima.runtime.execution_ledger import ExecutionLedger


class ResolvingTaskExecutor(TaskExecutor):
    """TaskExecutor that resolves a task's capability need via the CapabilityResolver.

    Delegates SELECTION to the resolver (I1) and only projects the returned
    ``ProviderResult`` into an ``ExecutionObservation`` (I2). The idempotency key is
    forwarded verbatim (I3) and gated on the ledger before any provider call.
    """

    def __init__(self, resolver: CapabilityResolver) -> None:
        self._resolver = resolver

    def execute(
        self,
        task: ExecutionStep,
        execution_key: tuple,
        ledger: Optional["ExecutionLedger"] = None,
    ) -> ExecutionObservation:
        # Idempotency gate (mirrors FakeTaskExecutor): ledger HIT -> return cached, no act.
        if ledger is not None:
            cached = ledger.get(execution_key)
            if cached is not None:
                return cached

        # I1: build the need, delegate SELECTION to the resolver. The executor names no
        # provider itself.
        need = CapabilityNeed.from_step(task)
        try:
            result = self._resolver.resolve(need, execution_key)
        except ResolutionError as exc:
            # A capability that cannot be served (no candidate / permission gap / all
            # providers failed) is a FAILED task, NOT a run-crashing exception (I2): the
            # orchestrator records the observation as FAILED via report_task_outcome.
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
                "provider_id": result.metadata.get("provider_id"),
                "side_effect_recorded": result.metadata.get(
                    "side_effect_recorded", False
                ),
            },
        )

        # Record the fact AFTER a successful read-only act (F5.4a policy). A crash before
        # this put only loses a read-only fact, so a resume retry is safe.
        if ledger is not None:
            ledger.put(execution_key, observation)

        return observation
