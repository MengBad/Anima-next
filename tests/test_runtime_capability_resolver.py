"""F5.4b — Capability Resolver + Provider Contract + Idempotency Boundary (read-only integration).

These tests validate the F5.4a design freeze WITHOUT touching forbidden layers
(constitution/*, core/*, workflow.py, persistence.py, recovery.py, bootstrap.py,
agent/executor.py). They exercise:

  - CapabilityResolver: candidate discovery / scoring / pick / fallback / degradation
  - Provider Contract: invoke I/O, permission boundary, error normalization
  - Idempotency Boundary (I3): (run_id, task_id, attempt) flows executor -> resolver -> provider
  - I1: Executor delegates capability selection to the Resolver (never picks itself)
  - I2: Provider only emits data (ProviderResult / Observation); never writes Workflow
  - I3: execution identity is unchanged and propagated verbatim down the stack

The executor under test is the NEW ``ResolvingTaskExecutor`` (a ``TaskExecutor``
subclass) — the orchestrator/executor/ledger signatures are NOT modified.
"""
from __future__ import annotations

from typing import Any, Callable

import pytest

from anima.constitution.execution import ExecutionStep
from anima.constitution.planning_interface import PlanningInterface
from anima.constitution.state import Drive, Goal, PlannerContext, Task
from anima.constitution.workflow import WorkflowInterface
from anima.runtime.executor import ExecutionObservation
from anima.runtime.execution_ledger import ExecutionLedger
from anima.runtime.orchestrator import PlanOrchestrator
from anima.runtime.resolving_executor import ResolvingTaskExecutor

from anima.runtime.capability_resolver import (
    CapabilityContract,
    CapabilityNeed,
    CapabilityNotFound,
    CapabilityRegistry,
    CapabilityResolver,
    CapabilityUnavailable,
    NoProvider,
    PermissionDenied,
)
from anima.runtime.providers import MemoryReadProvider
from anima.runtime.providers.base import (
    LogicalError,
    Provider,
    ProviderResult,
    TransientError,
)


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------
def _step(capability: str, args: dict | None = None, task_id: str = "t1") -> ExecutionStep:
    return ExecutionStep(
        id=task_id,
        title="step",
        intent="do it",
        depends_on=[],
        capability_need={"capability": capability, "args": args or {}},
        estimates={},
    )


class _RecordingProvider(Provider):
    """Configurable provider: controls success/errors and counts invocations."""

    def __init__(
        self,
        capability_id: str,
        *,
        granted_scopes: set[str] | None = None,
        healthy: bool = True,
        success_rate: float = 1.0,
        health: float = 1.0,
        latency_ms: float = 0.0,
        cost: float = 0.0,
        provider_id: str = "prov",
        behavior: Callable[[], ProviderResult] | None = None,
        side_effect: bool = False,
        idempotent: bool = True,
    ) -> None:
        self._capability_id = capability_id
        self._granted_scopes = granted_scopes or {"capability:invoke"}
        self._healthy = healthy
        self._success_rate = success_rate
        self._health = health
        self._latency_ms = latency_ms
        self._cost = cost
        self._provider_id = provider_id
        self._behavior = behavior
        self._side_effect = side_effect
        self._idempotent = idempotent
        self.invocations = 0

    @property
    def capability_id(self) -> str:
        return self._capability_id

    @property
    def granted_scopes(self) -> set[str]:
        return self._granted_scopes

    @property
    def healthy(self) -> bool:
        return self._healthy

    @property
    def success_rate(self) -> float:
        return self._success_rate

    @property
    def health(self) -> float:
        return self._health

    @property
    def latency_ms(self) -> float:
        return self._latency_ms

    @property
    def cost(self) -> float:
        return self._cost

    @property
    def side_effect(self) -> bool:
        return self._side_effect

    @property
    def idempotent(self) -> bool:
        return self._idempotent

    def invoke(self, capability, arguments, execution_key) -> ProviderResult:
        self.invocations += 1
        if self._behavior is not None:
            return self._behavior()
        return ProviderResult(
            success=True,
            data={"echo": arguments},
            metadata={"provider_id": self._provider_id},
        )


def _registry_with(*providers: Provider, permission: str = "capability:invoke") -> CapabilityRegistry:
    reg = CapabilityRegistry()
    cap_id = providers[0].capability_id
    reg.register(
        CapabilityContract(capability_id=cap_id, permission=permission), *providers
    )
    return reg


# ---------------------------------------------------------------------------
# A. CapabilityResolver Contract
# ---------------------------------------------------------------------------
def test_resolver_capability_not_found():
    resolver = CapabilityResolver(CapabilityRegistry())
    with pytest.raises(CapabilityNotFound):
        resolver.resolve(CapabilityNeed("nope"), ("r", "t", 0))


def test_resolver_picks_highest_scoring_healthy_provider():
    weak = _RecordingProvider("c", provider_id="weak", success_rate=0.5, latency_ms=500.0)
    strong = _RecordingProvider("c", provider_id="strong", success_rate=0.99, latency_ms=10.0)
    reg = _registry_with(weak, strong)
    result = CapabilityResolver(reg).resolve(CapabilityNeed("c"), ("r", "t", 0))
    # the strongest provider is chosen -> it recorded the invocation
    assert strong.invocations == 1
    assert weak.invocations == 0
    assert result.metadata["provider_id"] == "strong"


def test_resolver_excludes_unhealthy_provider():
    sick = _RecordingProvider("c", provider_id="sick", healthy=False, success_rate=1.0)
    well = _RecordingProvider("c", provider_id="well", healthy=True, success_rate=0.6)
    reg = _registry_with(sick, well)
    result = CapabilityResolver(reg).resolve(CapabilityNeed("c"), ("r", "t", 0))
    assert sick.invocations == 0
    assert well.invocations == 1
    assert result.metadata["provider_id"] == "well"


def test_resolver_permission_denied_excludes_scope_lacking_provider():
    # required scope is stricter than the provider's granted scope
    prov = _RecordingProvider("c", granted_scopes={"capability:invoke"})
    reg = _registry_with(prov, permission="capability:admin")
    with pytest.raises(PermissionDenied):
        CapabilityResolver(reg).resolve(CapabilityNeed("c"), ("r", "t", 0))
    assert prov.invocations == 0


def test_resolver_falls_back_on_logical_error():
    first = _RecordingProvider(
        "c",
        provider_id="first",
        behavior=lambda: (_ for _ in ()).throw(LogicalError("bad args")),
    )
    second = _RecordingProvider("c", provider_id="second")
    reg = _registry_with(first, second)
    result = CapabilityResolver(reg).resolve(CapabilityNeed("c"), ("r", "t", 0))
    assert first.invocations == 1
    assert second.invocations == 1
    assert result.metadata["provider_id"] == "second"


def test_resolver_retries_transient_then_fails_to_next():
    # first provider transient-fails but limited retry budget; then next is tried
    first = _RecordingProvider(
        "c",
        provider_id="flaky",
        behavior=lambda: (_ for _ in ()).throw(TransientError("blip")),
    )
    second = _RecordingProvider("c", provider_id="stable")
    contract = CapabilityContract(capability_id="c", permission="capability:invoke", retry=2)
    reg = CapabilityRegistry()
    reg.register(contract, first, second)
    result = CapabilityResolver(reg).resolve(CapabilityNeed("c"), ("r", "t", 0))
    # retry=2 => initial + 2 retries = 3 attempts on the flaky provider
    assert first.invocations == 3
    assert second.invocations == 1
    assert result.metadata["provider_id"] == "stable"


def test_resolver_all_fail_raises_unavailable():
    p1 = _RecordingProvider(
        "c", provider_id="p1", behavior=lambda: ProviderResult(success=False, error="e1")
    )
    p2 = _RecordingProvider(
        "c", provider_id="p2", behavior=lambda: ProviderResult(success=False, error="e2")
    )
    reg = _registry_with(p1, p2)
    with pytest.raises(CapabilityUnavailable) as exc:
        CapabilityResolver(reg).resolve(CapabilityNeed("c"), ("r", "t", 0))
    assert p1.invocations == 1 and p2.invocations == 1
    assert "e2" in str(exc.value)


# ---------------------------------------------------------------------------
# B. Provider Contract (I2) + Idempotency Boundary (I3)
# ---------------------------------------------------------------------------
def test_memory_read_provider_readonly_hit_and_miss():
    prov = MemoryReadProvider(provider_id="mem.local", store={"k1": "v1"})
    ok = prov.invoke("memory.read", {"key": "k1"}, ("r", "t", 0))
    assert ok.success and ok.data == {"value": "v1"}
    missing = prov.invoke("memory.read", {"key": "absent"}, ("r", "t", 0))
    assert not missing.success and "absent" in missing.error
    # read-only: no side effect recorded
    assert missing.metadata.get("side_effect_recorded") is False


def test_resolver_propagates_execution_key_to_provider_I3():
    captured = {}

    class _SpyProvider(_RecordingProvider):
        def invoke(self, capability, arguments, execution_key):
            captured["key"] = execution_key
            return ProviderResult(success=True, data={}, metadata={"provider_id": "spy"})

    prov = _SpyProvider("c")
    reg = _registry_with(prov)
    key = ("run-9", "task-9", 3)
    CapabilityResolver(reg).resolve(CapabilityNeed("c"), key)
    # the exact identity tuple reaches the provider unchanged (I3)
    assert captured["key"] == key


# ---------------------------------------------------------------------------
# C. ResolvingTaskExecutor — I1 (delegates) + I2 (data only) + I3 (ledger gate)
# ---------------------------------------------------------------------------
def _executor_for(*providers: Provider) -> ResolvingTaskExecutor:
    reg = _registry_with(*providers)
    return ResolvingTaskExecutor(CapabilityResolver(reg))


def test_resolving_executor_delegates_selection_to_resolver_I1():
    # Executor holds NO provider list; it only hands the need to the resolver.
    weak = _RecordingProvider("c", provider_id="weak", success_rate=0.4)
    strong = _RecordingProvider("c", provider_id="strong", success_rate=0.95)
    executor = _executor_for(weak, strong)
    obs = executor.execute(_step("c"), ("r", "t", 0))
    assert obs.success
    # selection was the resolver's job -> strongest provider ran, weak did not
    assert strong.invocations == 1 and weak.invocations == 0


def test_resolving_executor_projects_provider_result_to_observation_I2():
    prov = _RecordingProvider(
        "c",
        provider_id="p",
        behavior=lambda: ProviderResult(
            success=True, data={"x": 1}, metadata={"provider_id": "p"}
        ),
    )
    obs = _executor_for(prov).execute(_step("c", {"k": "v"}), ("r", "t", 0))
    assert isinstance(obs, ExecutionObservation)
    assert obs.success is True
    assert obs.result == {"data": {"x": 1}}
    assert obs.metadata["provider_id"] == "p"
    # I2: provider returned pure data; the executor produced an Observation, not a run mutation
    assert "skipped" not in obs.metadata


def test_resolving_executor_failure_propagates_as_observation_not_exception_I2():
    prov = _RecordingProvider(
        "c", behavior=lambda: ProviderResult(success=False, error="boom")
    )
    # a failed capability resolution becomes a FAILED observation, not a raised exception
    # that would crash run_plan (I2: provider/executor never writes run state, just data)
    obs = _executor_for(prov).execute(_step("c"), ("r", "t", 0))
    assert obs.success is False
    assert "boom" in obs.error


def test_resolving_executor_ledger_hit_skips_reinvoke_I3():
    prov = _RecordingProvider("c")
    executor = _executor_for(prov)
    ledger = ExecutionLedger()
    key = ("r", "t1", 0)
    executor.execute(_step("c", task_id="t1"), key, ledger=ledger)
    assert prov.invocations == 1
    # second call with same key -> ledger HIT, provider NOT reinvoked
    executor.execute(_step("c", task_id="t1"), key, ledger=ledger)
    assert prov.invocations == 1
    assert ledger.has(key)


def test_resolving_executor_passes_execution_key_through_I3():
    captured = {}

    class _Spy(_RecordingProvider):
        def invoke(self, capability, arguments, execution_key):
            captured["key"] = execution_key
            return ProviderResult(success=True, data={}, metadata={"provider_id": "spy"})

    executor = _executor_for(_Spy("c"))
    key = ("RR", "TT", 2)
    executor.execute(_step("c", task_id="TT"), key, ledger=ExecutionLedger())
    assert captured["key"] == key


# ---------------------------------------------------------------------------
# D. End-to-end through PlanOrchestrator (F5.3 path + F5.4b executor)
# ---------------------------------------------------------------------------
def test_orchestrator_runs_plan_with_resolving_executor():
    planning = PlanningInterface()
    goal = Goal(target="g", success_criterion="sc", horizon="weeks")
    plan = planning.request_plan("goal-1", goal=goal, drive=Drive(), context=PlannerContext())
    # override the derived task graph with a capability-bearing task (test control)
    plan.task_graph = [
        Task(
            id="t1",
            title="read",
            intent="read memory",
            capability_need={"capability": "memory.read", "args": {"key": "k1"}},
        )
    ]
    planning.activate_plan(plan.id)

    provider = MemoryReadProvider(provider_id="mem.local", store={"k1": "v1"})
    registry = CapabilityRegistry()
    registry.register(CapabilityContract(capability_id="memory.read"), provider)
    resolver = CapabilityResolver(registry)

    ledger = ExecutionLedger()
    orch = PlanOrchestrator(
        planning=planning,
        workflow=WorkflowInterface(),
        executor=ResolvingTaskExecutor(resolver),
        ledger=ledger,
    )
    final = orch.run_plan(plan.id)
    assert final.task_states[0].status.value == "done"
    assert final.task_states[0].result == {"data": {"value": "v1"}}
    assert ledger.has((final.id, "t1", 0))
