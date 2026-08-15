"""Provider Contract (F5.4b) — the external-capability boundary (RFC-0001 / RFC-0001A).

This module is the ONLY place that names the provider-side vocabulary. It is deliberately
small and dependency-free (runtime-only): it imports NO constitution / workflow / ledger, so
a Provider can never reach back into lifecycle state (I2 — Provider ≠ Workflow Authority).

Frozen invariants this module encodes:

- **I2 (Provider ≠ Workflow Authority):** ``Provider.invoke`` receives only
  ``(capability, arguments, execution_key)`` and returns a pure-data ``ProviderResult``.
  It is given NO run handle, NO workflow reference, NO ``report_task_outcome`` — it cannot
  write run/task state. The Observation (produced upstream by the executor) is the only
  carrier back into the Workflow.
- **I3 (execution identity unchanged):** the ``execution_key`` tuple
  ``(run_id, task_id, attempt)`` is passed through verbatim; the Provider only *consumes*
  it as the external idempotency token (for a side-effecting provider this is MANDATORY —
  F5.7 C2: F5 identity == external mutation identity). It must never redefine or extend
  the key.
- **Error normalization:** a Provider signals retryable vs logical failure with the two
  exceptions below; any other failure is expressed as a ``ProviderResult(success=False)``
  (no raw exception escapes into the Resolver's control flow).

The Score-relevant dimensions (success_rate / health / latency_ms / cost) are advertised as
provider properties so the Resolver's default Scorer can rank candidates without a separate
metadata store (mirrors RFC-0001A metadata, kept on the live provider).
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProviderResult:
    """Pure-data output of one provider invocation (F5.4b Provider Contract).

    The Provider returns THIS and nothing else. It never mutates run/task state; the
    executor projects this onto an ``ExecutionObservation`` (the only carrier back into
    the Workflow). ``data`` must be JSON-serializable; ``metadata`` carries provider-only
    bookkeeping (provider_id, timings, side_effect_recorded...).
    """

    data: Any = None
    success: bool = False
    error: str = ""                          # failure reason ("" on success)
    metadata: dict = field(default_factory=dict)
    # R6.1 治理/审计链必需字段（带默认值，向后兼容）：结构化拒绝原因、
    # 退出码、审计轨迹（capability/principal/owner_authorized）、执行标识与产出度量。
    denied_reason: str | None = None        # 结构化拒绝原因（governance 可判定）
    exit_code: int | None = None
    audit: dict = field(default_factory=dict)
    execution_id: str | None = None         # 外部幂等/溯源标识
    bytes: int | None = None                # 标准输出字节数
    stdout_hash: str | None = None          # 标准输出 sha256 溯源


class Provider(abc.ABC):
    """The external-capability boundary (F5.4b).

    A Provider wraps one external capability (memory.read, search.web, code.execute...).
    It owns the *act* of calling the external world; it owns NO selection logic (that is
    the Resolver's job — I1) and NO lifecycle state (I2).
    """

    # -- capability routing -------------------------------------------------
    @property
    @abc.abstractmethod
    def capability_id(self) -> str:
        """The capability this provider can serve (registry key)."""

    # -- permission boundary (I2) -------------------------------------------
    @property
    def granted_scopes(self) -> set[str]:
        """Scopes this provider is permitted to run under.

        The Resolver only considers a provider whose ``granted_scopes`` is a SUPERSET of
        the capability contract's required ``permission`` (RFC-0001 permission-superset
        rule). A provider must never escalate or assume a broader tier than granted here.
        """
        return {"capability:invoke"}

    # -- health probe (RFC-0001 health filtering) ---------------------------
    @property
    def healthy(self) -> bool:
        """Live health flag. Unhealthy providers are excluded from candidate sets."""
        return True

    # -- scoring dimensions (RFC-0001A metadata, advertised by the provider) -
    @property
    def success_rate(self) -> float:
        return 1.0

    @property
    def health(self) -> float:
        return 1.0

    @property
    def latency_ms(self) -> float:
        return 0.0

    @property
    def cost(self) -> float:
        return 0.0

    # -- side-effect / idempotency profile (RFC-0001A) ----------------------
    @property
    def side_effect(self) -> bool:
        """Whether invoking this provider mutates external state."""
        return False

    @property
    def idempotent(self) -> bool:
        """Whether a repeated external call is safe (no duplicate external side effect)."""
        return True

    # -- the invocation act -------------------------------------------------
    @abc.abstractmethod
    def invoke(
        self, capability: str, arguments: dict, execution_key: tuple
    ) -> ProviderResult:
        """Execute the capability once under ``execution_key``; return pure data.

        ``execution_key`` = (run_id, task_id, attempt) — consumed verbatim (I3); a
        side-effecting provider MUST offer it to the external system as the idempotency
        token (F5.7 C2) so a commit-then-crash window cannot double-apply. May raise
        :class:`TransientError` (retryable) or :class:`LogicalError`
        (non-retryable, skip to next candidate); all other failures MUST be returned as
        ``ProviderResult(success=False, error=...)`` rather than raised.
        """
        raise NotImplementedError


class TransientError(Exception):
    """Provider-side retryable failure (network blip, timeout). Resolver retries."""


class LogicalError(Exception):
    """Provider-side non-retryable logical failure (bad args, contract violation).

    The Resolver skips to the next candidate immediately — retrying would be futile.
    """
