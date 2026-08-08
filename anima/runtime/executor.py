"""TaskExecutor — the runtime execution contract (F5.1).

Implements the frozen F5 Executor Contract WITHOUT real side effects yet:

    Executor.execute(task, execution_key) -> ExecutionObservation

- ``execution_key`` is the idempotency key ``(run_id, task_id, attempt)`` — the triple
  that uniquely identifies one logical execution attempt (F5 ruling).
- ``ExecutionObservation`` is a PER-TASK record (never an aggregate): task_id / success /
  result / error / metadata.

Boundary rules (frozen in F5 / F5.1):

- **The executor never writes WorkflowRun.** No ``_transition``, no ``_registry``, no
  ``report_task_outcome``, no ``run.status =``, no ``task_states =``. It produces an
  *observation*; the caller (orchestrator) converts it and reports through the Workflow's
  public API.
- **The executor never judges the run.** A single observation has no opinion about run
  completion/failure — that is the Workflow Authority's F-04.
- **Idempotency is the caller's gate.** The interface accepts ``execution_key`` and (in
  ``execute``) an optional ``ledger``; when a ledger is supplied and the key is already
  recorded, the cached observation is returned and no execution happens. The executor
  itself stays dumb: it executes when asked.

FakeTaskExecutor is a *controlled* implementation for tests — it executes exactly the
scripted outcomes, so F5.1 can validate the contract (DONE/FAILED/SKIPPED/exception/
idempotency hit) without touching any real Capability→Provider. Real side-effect
executors (Capability layer wiring) are a later slice (F5.3+).
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from anima.constitution.execution import ExecutionStep


@dataclass
class ExecutionObservation:
    """The per-task fact an executor produces after one execution attempt (F5.1).

    Deliberately per-task, never aggregate: it describes what happened to THIS task.
    It holds no run-level judgment (no run status, no completion verdict) — the Workflow
    Authority derives those from the whole task set via F-04.
    """

    task_id: str = ""
    success: bool = False
    result: dict = field(default_factory=dict)   # recorded outcome payload
    error: str = ""                              # failure reason ("" on success)
    metadata: dict = field(default_factory=dict) # e.g. {"skipped": True} / timings


class TaskExecutor(abc.ABC):
    """The execution contract: one task + one idempotency key -> one observation.

    Subclasses implement the real side-effecting act (Capability→Provider, tool call,
    API request...). An optional ``ledger`` may be passed to ``execute``: if the key is
    already recorded, the cached observation is returned and no execution happens.
    """

    @abc.abstractmethod
    def execute(
        self,
        task: ExecutionStep,
        execution_key: tuple,
        ledger: Optional["ExecutionLedger"] = None,
    ) -> ExecutionObservation:
        """Execute ``task`` once under ``execution_key``; return its observation.

        ``execution_key`` = (run_id, task_id, attempt). When ``ledger`` is given and the
        key is already recorded, return the cached observation (idempotency hit) without
        executing. The executor must not write WorkflowRun state.
        """
        raise NotImplementedError


class FakeTaskExecutor(TaskExecutor):
    """Controlled executor for F5.1 contract validation (no real side effects).

    ``script`` maps an execution_key to a fixed observation; ``on_execute`` is an
    optional hook invoked on real (non-cached) executions — tests use it to count
    actual executions and prove the idempotency hit skips re-execution.
    """

    def __init__(
        self,
        script: dict = None,
        on_execute: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._script = dict(script or {})
        self._on_execute = on_execute

    def execute(
        self,
        task: ExecutionStep,
        execution_key: tuple,
        ledger: Optional["ExecutionLedger"] = None,
    ) -> ExecutionObservation:
        if ledger is not None:
            cached = ledger.get(execution_key)
            if cached is not None:
                return cached  # idempotency hit: no re-execution

        if execution_key not in self._script:
            raise KeyError(
                f"FakeTaskExecutor: no scripted outcome for key {execution_key!r} "
                f"(controlled executor never invents results)"
            )
        observation = self._script[execution_key]
        if self._on_execute is not None:
            self._on_execute()

        if ledger is not None:
            ledger.put(execution_key, observation)  # record the side-effect fact

        return observation
