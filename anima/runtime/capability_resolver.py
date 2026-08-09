"""CapabilityResolver Contract (F5.4b) — capability selection layer (RFC-0001 / RFC-0001A).

This module owns the SELECTION logic that the Executor must NOT own (I1 —
Executor ≠ Capability Selector). It answers:

    CapabilityNeed -> candidates -> score -> pick -> ProviderResult

and nothing about *when* tasks run, *what* the Workflow thinks, or *how* observations
become run state. That separation is what keeps the executor a pure executioner and the
Provider boundary clean (I2).

Frozen invariants:

- **I1 (Executor ≠ Capability Selector):** selection lives HERE. The Executor builds a
  ``CapabilityNeed`` from the task and calls ``resolve``; the Resolver chooses the
  provider. The Executor never holds a provider list and never names a provider.
- **I2 (Provider ≠ Workflow Authority):** the Resolver only calls ``provider.invoke`` and
  returns its ``ProviderResult``. It never writes run/task state.
- **I3 (execution identity unchanged):** the ``execution_key`` tuple
  ``(run_id, task_id, attempt)`` is passed through to ``provider.invoke`` verbatim.
  Neither the Resolver nor the Provider may redefine or extend it.

Resolution algorithm (mirrors RFC-0001 Candidate→Score→Pick):

1. **Contract lookup** — capability must be registered (else ``CapabilityNotFound``).
2. **Candidate discovery** — providers for that capability that are ``healthy`` AND whose
   ``granted_scopes`` is a SUPERSET of the contract's required ``permission``.
   No candidate -> ``PermissionDenied`` (scope gap) or ``NoProvider`` (none registered).
3. **Scoring** — a pluggable Scorer ranks candidates (default: 0.4*Success + 0.3*Health
   + 0.2*Latency(inverted) + 0.1*Cost(inverted), dimensions from RFC-0001A metadata).
4. **Pick / degradation** — try candidates in score order. ``TransientError`` is retried
   up to ``contract.retry`` times within the same provider; ``LogicalError`` or a
   ``success=False`` result skips to the next candidate. All candidates exhausted ->
   ``CapabilityUnavailable`` (last error surfaced in the message, diagnostics only).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from anima.constitution.execution import ExecutionStep
from anima.runtime.providers.base import (
    LogicalError,
    Provider,
    ProviderResult,
    TransientError,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import edge
    from anima.capability.governance.enforcement import EnforcementLevel
    from anima.capability.governance.validation import ValidationReport


# ---------------------------------------------------------------------------
# Error taxonomy (RFC-0001 resolution errors)
# ---------------------------------------------------------------------------
class ResolutionError(Exception):
    """Base class for capability-resolution failures."""


class CapabilityNotFound(ResolutionError):
    """The requested ``capability_id`` is not registered at all."""


class NoProvider(ResolutionError):
    """A contract exists but no provider is registered for the capability."""


class PermissionDenied(ResolutionError):
    """No registered provider holds the scope required by the capability contract."""


class CapabilityUnavailable(ResolutionError):
    """All candidate providers failed (transient exhausted / logical / soft-fail)."""


class RegistrationIntegrityError(Exception):
    """Registration refused: a capability contract and its provider disagree on the
    side-effect / idempotency profile (F7.1 guard).

    ``CapabilityContract`` is the capability *policy* (declaration layer); the
    ``Provider`` is the runtime *truth* (implementation layer). The two MUST agree:
    a policy that declares ``side_effect=False`` while the provider actually mutates
    external state is a silent safety hole — the executor would skip its ledger
    side-effect gate (C2 / C4). We refuse registration rather than let the
    inconsistency stand.

    Deliberately NOT a ``ResolutionError`` (this is a configuration-time guard, not a
    resolution-time failure), and deliberately NOT contract-as-sole-truth: the provider
    is the real executor, so a "declares safe but mutates" implementation cannot hide
    behind a policy it satisfies on paper.
    """


# ---------------------------------------------------------------------------
# CapabilityNeed + Contract + Registry
# ---------------------------------------------------------------------------
@dataclass
class CapabilityNeed:
    """What the Executor asks the Resolver to fulfill (F5.4b).

    Built from a task's ``capability_need`` dict by :meth:`from_step`. ``context`` carries
    optional scoring preferences (latency/cost bias) — the resolver reads them but they
    never override the permission/safety gates.
    """

    capability_id: str
    args: dict = field(default_factory=dict)
    context: dict = field(default_factory=dict)

    @classmethod
    def from_step(cls, step: ExecutionStep) -> "CapabilityNeed":
        """Project an ``ExecutionStep``'s ``capability_need`` dict into a ``CapabilityNeed``.

        The shape is ``{"capability": <id>, "args": {...}, "context": {...}}``. A missing
        ``"capability"`` is a contract misuse by the caller (the task declared no need),
        surfaced as ``CapabilityNotFound`` rather than silently inventing one.
        """
        cn = step.capability_need or {}
        capability_id = cn.get("capability")
        if not capability_id:
            raise CapabilityNotFound(
                f"CapabilityNeed.from_step: ExecutionStep {step.id!r} declares no "
                f"'capability' in capability_need (executor must not invent one)"
            )
        return cls(
            capability_id=capability_id,
            args=dict(cn.get("args", {})),
            context=dict(cn.get("context", {})),
        )


@dataclass
class CapabilityContract:
    """Frozen metadata for a capability (RFC-0001A), owned by the registry.

    Carries the safety/selection parameters the Resolver enforces: the required
    ``permission`` scope, the side-effect/idempotency profile, the timeout, and the
    per-provider ``retry`` budget for transient failures.
    """

    capability_id: str
    permission: str = "capability:invoke"   # required granted scope (superset gate)
    side_effect: bool = False
    idempotent: bool = True
    timeout: float = 30.0
    retry: int = 0                           # transient-retry budget per provider


class CapabilityRegistry:
    """Config-driven capability → [providers] routing table (F5-native, RFC-0001).

    Distinct from the V1 ``anima/tools`` ToolRegistry: this is the F5 runtime's resolver
    substrate. Registration is explicit and ordered (the order seeds candidate priority
    before scoring re-ranks).

    Registration passes two independent gates, in this order:

    1. **F7.1 registration integrity** — contract (policy) vs provider (runtime truth)
       must agree on the side-effect / idempotency profile. This is a *safety* gate and
       therefore runs first: a mismatch here is a silent correctness hole, not a
       documentation problem.
    2. **F8.2b metadata integrity** — the contract is projected into the canonical
       governance model and refused if its metadata *contradicts itself* (see
       ``anima.capability.governance.enforcement``). Structural gaps — fields a given
       contract face cannot express at all — are recorded as warnings, not refusals.

    Both gates validate BEFORE any mutation, so a rejected registration leaves the
    registry byte-for-byte unchanged.
    """

    def __init__(self, *, metadata_enforcement: "EnforcementLevel | None" = None) -> None:
        # Imported lazily: the governance package must not become a load-time
        # dependency of the runtime, and registry construction is config-time anyway.
        from anima.capability.governance.enforcement import DEFAULT_ENFORCEMENT_LEVEL

        self._contracts: dict[str, CapabilityContract] = {}
        self._providers: dict[str, list[Provider]] = {}
        self._metadata_reports: dict[str, "ValidationReport"] = {}
        self._metadata_enforcement = (
            DEFAULT_ENFORCEMENT_LEVEL if metadata_enforcement is None else metadata_enforcement
        )

    def register(
        self, contract: CapabilityContract, *providers: Provider
    ) -> None:
        if not providers:
            raise ValueError("register requires at least one provider")
        # F7.1 Registration Integrity: contract (policy / declaration layer) and
        # provider (runtime truth / implementation layer) must agree on the
        # side-effect / idempotency profile. A mismatch lets a "declares safe but
        # actually mutates" provider slip past the executor's ledger side-effect
        # gate (C2 / C4). Validate BEFORE mutating so a reject leaves the registry
        # unchanged (atomic).
        for provider in providers:
            if (
                provider.side_effect != contract.side_effect
                or provider.idempotent != contract.idempotent
            ):
                raise RegistrationIntegrityError(
                    f"register: capability {contract.capability_id!r} contract "
                    f"(policy) contradicts provider {provider.capability_id!r} "
                    f"(runtime truth) on side-effect/idempotency. "
                    f"contract=(side_effect={contract.side_effect}, "
                    f"idempotent={contract.idempotent}) "
                    f"provider=(side_effect={provider.side_effect}, "
                    f"idempotent={provider.idempotent}). "
                    f"Declared policy must match runtime truth."
                )

        # F8.2b Metadata Integrity: refuse metadata that contradicts itself (cacheable
        # but non-deterministic, non-positive timeout...). Runs AFTER the F7.1 safety
        # gate — when both are unhappy the safety violation is the one worth reporting.
        # Config-time only: resolve() never pays for this. Raises before any mutation.
        from anima.capability.governance.enforcement import enforce_runtime_contract

        report = enforce_runtime_contract(contract, level=self._metadata_enforcement)

        self._contracts[contract.capability_id] = contract
        self._providers.setdefault(contract.capability_id, []).extend(providers)
        self._metadata_reports[contract.capability_id] = report

    def has(self, capability_id: str) -> bool:
        return capability_id in self._contracts

    def contract_for(self, capability_id: str) -> CapabilityContract:
        return self._contracts[capability_id]

    def providers_for(self, capability_id: str) -> list[Provider]:
        return list(self._providers.get(capability_id, []))

    def metadata_report_for(self, capability_id: str) -> "ValidationReport | None":
        """The governance report captured when this capability was registered.

        Non-blocking findings (every structural gap, plus warnings like "retries a
        non-idempotent capability") would otherwise evaporate the moment registration
        succeeds. Keeping them reachable is what separates an enforcement gate from
        governance theatre.
        """
        return self._metadata_reports.get(capability_id)


# ---------------------------------------------------------------------------
# Scoring (RFC-0001A metadata → ranking)
# ---------------------------------------------------------------------------
def default_scorer(provider: Provider, contract: CapabilityContract) -> float:
    """Default candidate ranking (RFC-0001 weights).

    0.4*Success + 0.3*Health + 0.2*Latency(inverted) + 0.1*Cost(inverted).

    Latency and cost are normalized to [0,1] (lower is better), so their inverted forms
    reward speed/cheapness. Success/Health are already [0,1].
    """
    success = max(0.0, min(1.0, provider.success_rate))
    health = max(0.0, min(1.0, provider.health))
    latency = 1.0 - max(0.0, min(1.0, provider.latency_ms / 1000.0))
    cost = 1.0 - max(0.0, min(1.0, provider.cost / 1.0))
    return 0.4 * success + 0.3 * health + 0.2 * latency + 0.1 * cost


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------
class CapabilityResolver:
    """The selection authority (F5.4b). Resolves a ``CapabilityNeed`` to a ``ProviderResult``.

    See module docstring for the frozen resolution algorithm. The resolver owns NO
    execution act and NO workflow state — it is pure routing + degradation.
    """

    def __init__(
        self,
        registry: CapabilityRegistry,
        scorer: Callable[[Provider, CapabilityContract], float] | None = None,
    ) -> None:
        self._registry = registry
        self._scorer = scorer or default_scorer

    def resolve(
        self, need: CapabilityNeed, execution_key: tuple
    ) -> ProviderResult:
        """Select a provider and invoke it; degrade across candidates on failure.

        ``execution_key`` = (run_id, task_id, attempt) — passed verbatim to the chosen
        provider (I3). Raises a ``ResolutionError`` subclass if no provider can serve the
        need (see module docstring for each case).
        """
        if not self._registry.has(need.capability_id):
            raise CapabilityNotFound(
                f"resolve: capability {need.capability_id!r} is not registered"
            )
        contract = self._registry.contract_for(need.capability_id)
        candidates = [
            p
            for p in self._registry.providers_for(need.capability_id)
            if p.healthy and contract.permission in p.granted_scopes
        ]
        if not candidates:
            # distinguish a scope gap from a total absence for clearer diagnostics
            if self._registry.providers_for(need.capability_id):
                raise PermissionDenied(
                    f"resolve: no provider for {need.capability_id!r} holds the "
                    f"required scope {contract.permission!r}"
                )
            raise NoProvider(
                f"resolve: no provider registered for {need.capability_id!r}"
            )

        ordered = sorted(
            candidates, key=lambda p: self._scorer(p, contract), reverse=True
        )

        last_error: str | None = None
        for provider in ordered:
            # per-provider transient-retry budget (LogicalError / soft-fail skip immediately)
            attempts = 1 + max(0, contract.retry)
            for attempt in range(attempts):
                try:
                    result = provider.invoke(
                        need.capability_id, need.args, execution_key
                    )
                except TransientError as exc:
                    last_error = f"transient: {exc}"
                    continue  # retry within budget
                except LogicalError as exc:
                    last_error = f"logical: {exc}"
                    break  # non-retryable -> next candidate
                if result.success:
                    return result
                last_error = result.error or "unknown failure"
                break  # soft failure -> next candidate
        raise CapabilityUnavailable(
            f"resolve: capability {need.capability_id!r} unavailable "
            f"(all candidates failed; last error: {last_error})"
        )
