"""MockHttpMutationProvider — a sandbox F5-native side-effecting Provider (F5.7b).

PURPOSE (F5.7 External Side Effect Boundary): prove the External Mutation Provider
Contract (C1), Idempotency Token Contract (C2), Retry Ownership Matrix (C3) and
Ledger Ordering Decision (C4) end-to-end WITHOUT touching any real external system.

It is intentionally a *mock*: it holds an injected in-memory ``external_store`` that
stands in for "the external system". The store is keyed by the ``execution_key`` the
provider receives verbatim (I3) — i.e. the external idempotency token. On a repeated
token the store returns the previously committed effect instead of re-applying, which
is exactly how a real idempotency-token-capable external system behaves.

Contracts enforced:

- C1 (I2, Provider ≠ Workflow Authority): this provider imports NO ledger / run /
  workflow. It owns only the act of calling the (mock) external world and returns a
  pure ``ProviderResult``. It is given the execution_key and consumes it as a token; it
  NEVER writes run/task/ledger state.
- C2 (Idempotency Token): ``execution_key = (run_id, task_id, attempt)`` is passed
  through verbatim and used AS-IS as the external token. The provider must not extend
  it (new dimensions belong in ``metadata`` only).
- C3 (Retry Ownership): the provider raises ``TransientError`` only for PRE-commit
  failures (always safe to retry) or, when ``idempotent=True``, POST-commit acks-lost
  failures (the external store already holds the token, so the resolver's retry hits
  the dedupe and does not re-apply). A NON-idempotent provider MUST NOT raise
  ``TransientError`` for a post-commit unknown state — it returns
  ``ProviderResult(success=False, error="unknown_post_commit")`` so the resolver does
  not blind-retry a dangerous call.
- C4 (Ledger Ordering): the executor owns the ledger gate; this provider only
  propagates the token. No provider→ledger write permission exists (I2).
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from anima.runtime.providers.base import (
    Provider,
    ProviderResult,
    TransientError,
)


class MockHttpMutationProvider(Provider):
    """Sandbox side-effecting provider: applies a mock external mutation under a token.

    ``external_store`` is the simulated external system (keyed by execution_key). ``apply``
    is the effect ``(arguments) -> data``; the default simply records that a mutation
    occurred. ``post_commit_transient_first_call`` simulates the C4 crash window: the
    external system commits, but the local ack is lost before the ledger.put.
    """

    def __init__(
        self,
        external_store: Optional[dict] = None,
        provider_id: str = "mock.http.mutate.local",
        idempotent: bool = True,
        apply: Optional[Callable[[dict], Any]] = None,
        post_commit_transient_first_call: bool = False,
    ) -> None:
        self._store = external_store if external_store is not None else {}
        self._provider_id = provider_id
        self._idempotent = idempotent
        self._apply = apply
        self._post_commit_transient = post_commit_transient_first_call
        self.invocation_count = 0  # how many times invoke() was entered
        self.apply_count = 0       # how many actual external mutations were applied

    @property
    def capability_id(self) -> str:
        return "mock.http.mutate"

    @property
    def side_effect(self) -> bool:
        return True

    @property
    def idempotent(self) -> bool:
        return self._idempotent

    def invoke(
        self, capability: str, arguments: dict, execution_key: tuple
    ) -> ProviderResult:
        self.invocation_count += 1

        # External idempotency token dedupe (C2): if the external system already holds
        # this token's committed effect, return it WITHOUT re-applying the side effect.
        if self._idempotent and execution_key in self._store:
            return self._store[execution_key]

        # First time for this token: apply the (mock) external mutation exactly once.
        self.apply_count += 1
        data = self._apply(arguments) if self._apply is not None else {"applied": True}
        result = ProviderResult(
            success=True,
            data=data,
            metadata={
                "provider_id": self._provider_id,
                "side_effect_recorded": True,
            },
        )

        if self._idempotent:
            # record under the token so a retry / resume dedupes (C2 / C4)
            self._store[execution_key] = result

        # Simulate the C4 crash window: the external system committed, but the local ack
        # was lost BEFORE the ledger.put. Safe to retry ONLY because the token is in the
        # store (idempotent=True) — the resolver's retry will hit the dedupe above.
        if self._post_commit_transient:
            if self._idempotent:
                raise TransientError("mock: external committed but ack lost")
            # non-idempotent: a blind retry would DOUBLE-apply. Refuse to raise
            # TransientError; surface unknown_post_commit so the resolver does not retry.
            return ProviderResult(
                success=False,
                error="unknown_post_commit",
                metadata={
                    "provider_id": self._provider_id,
                    "unknown_post_commit": True,
                    "side_effect_recorded": True,
                },
            )

        return result
