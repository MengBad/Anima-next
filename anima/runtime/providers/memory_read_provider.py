"""MemoryReadProvider — a minimal F5-native, read-only Provider (F5.4b).

Intentionally the SIMPLEST real provider: ``side_effect=False``, ``idempotent=True``,
deterministic. It proves the Provider Contract (I2) and the execution_key passthrough
(I3) without touching any real external system — exactly the F5.4b "minimal provider"
target. Side-effecting providers (search.web, code.execute, qq.send...) are a later
slice, when the external idempotency token mechanism is exercised for real.
"""
from __future__ import annotations

from typing import Any

from anima.runtime.providers.base import Provider, ProviderResult


class MemoryReadProvider(Provider):
    """Reads a value from an injected in-memory store under ``arguments['key']``.

    Read-only by construction: it never writes the store, so a repeated call is always
    safe (idempotent=True). This makes it the canonical first provider to validate the
    Resolver → Provider → Ledger chain end-to-end before real external capabilities land.
    """

    def __init__(
        self, store: dict | None = None, provider_id: str = "memory.read.local"
    ) -> None:
        self._store = dict(store or {})
        self._provider_id = provider_id

    @property
    def capability_id(self) -> str:
        return "memory.read"

    @property
    def granted_scopes(self) -> set[str]:
        return {"capability:invoke", "memory:read"}

    def invoke(
        self, capability: str, arguments: dict, execution_key: tuple
    ) -> ProviderResult:
        key = arguments.get("key")
        if key is None:
            return ProviderResult(
                success=False,
                error="memory.read: missing 'key' argument",
                metadata={
                    "provider_id": self._provider_id,
                    "side_effect_recorded": False,
                },
            )
        if key not in self._store:
            return ProviderResult(
                success=False,
                error=f"memory.read: key {key!r} not found",
                metadata={
                    "provider_id": self._provider_id,
                    "side_effect_recorded": False,
                },
            )
        return ProviderResult(
            success=True,
            data={"value": self._store[key]},
            metadata={
                "provider_id": self._provider_id,
                "side_effect_recorded": False,
            },
        )
