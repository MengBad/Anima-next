"""ExecutionLedger — the idempotency fact store (F5.1).

Records the *side-effect facts* of execution, deliberately SEPARATE from the
WorkflowRunStore (F5 ruling):

    WorkflowRunStore   — lifecycle state: what the workflow thinks is happening
    ExecutionLedger    — side-effect facts: what the external world has actually changed

The two are different truth sources and must not merge: a run snapshot can say
"task t1 is PENDING" while the ledger knows "the external call for (run_id, t1, attempt
1) already happened" — exactly the dispatch-durability gap F5 addresses.

Contract (frozen in F5 / F5.1):

- Key: the idempotency key ``(run_id, task_id, attempt)`` — uniquely identifies one
  logical execution attempt.
- ``get(key)``  -> the recorded observation, or None (no fact yet).
- ``put(key, observation)`` -> record the fact (idempotency is the caller's gate: an
  executor consults the ledger BEFORE acting and records AFTER acting).

Scope note (F5.1): this slice is the in-memory contract. Persistence of the ledger
(so a restart can consult facts recorded by a crashed process — the real F5.3 value)
is explicitly deferred to F5.2. The interface is already shaped for it: nothing here
couples to run-snapshot storage, so a persistent backend can be added without touching
the WorkflowRunStore.
"""
from __future__ import annotations

from typing import Any, Optional


class ExecutionLedger:
    """In-memory idempotency fact store (F5.1 contract; persistence deferred to F5.2)."""

    def __init__(self) -> None:
        self._facts: dict[tuple, Any] = {}

    def get(self, execution_key: tuple) -> Optional[Any]:
        """Return the recorded observation for ``execution_key``, or None if absent."""
        return self._facts.get(execution_key)

    def put(self, execution_key: tuple, observation: Any) -> None:
        """Record the side-effect fact for ``execution_key``.

        Overwrites are allowed (a re-execution under the same key records the latest
        fact), but the idempotency contract relies on the caller checking ``get`` first.
        """
        self._facts[execution_key] = observation

    def has(self, execution_key: tuple) -> bool:
        """Whether a fact is already recorded for this key (idempotency probe)."""
        return execution_key in self._facts
