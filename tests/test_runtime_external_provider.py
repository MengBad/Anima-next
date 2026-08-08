"""F5.7b — External Side Effect Boundary: provider + idempotency + retry + ledger.

Drives the REAL authorities (ResolvingTaskExecutor + CapabilityResolver +
ExecutionLedger) with the sandbox MockHttpMutationProvider to prove the four frozen
F5.7a contracts end-to-end, WITHOUT any real external system:

  C1 External Mutation Provider Contract  (I2 preserved: provider owns no ledger/run/wf)
  C2 Idempotency Token Contract           (execution_key == external idempotency token)
  C3 Retry Ownership Matrix               (no blind retry; resolver owns transient retry)
  C4 Ledger Ordering Decision             (executor gate + external dedupe, no provider→ledger)

These are the PM's frozen F5.7b acceptance criteria:

  1. normal mutation            -> external_count == 1
  2. ledger HIT                 -> provider not called, external_count == 1
  3. ledger MISS + token HIT    -> provider called, external dedupe, external_count == 1
  4. unknown post-commit        -> no blind retry
  5. I1 / I2 / I3 regression
"""
from __future__ import annotations

import ast
import pathlib

from anima.constitution.execution import ExecutionStep
from anima.runtime.capability_resolver import (
    CapabilityContract,
    CapabilityRegistry,
    CapabilityResolver,
)
from anima.runtime.execution_ledger import ExecutionLedger
from anima.runtime.providers.mock_http_mutation_provider import (
    MockHttpMutationProvider,
)
from anima.runtime.resolving_executor import ResolvingTaskExecutor


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
def _step(capability="mock.http.mutate", args=None):
    return ExecutionStep(
        id="t1",
        title="mutate",
        intent="external mutation",
        depends_on=[],
        capability_need={"capability": capability, "args": args or {}},
        estimates={},
    )


def _wire(provider, retry=2):
    registry = CapabilityRegistry()
    registry.register(
        CapabilityContract(
            "mock.http.mutate",
            side_effect=True,
            idempotent=provider.idempotent,
            permission="capability:invoke",
            retry=retry,
        ),
        provider,
    )
    resolver = CapabilityResolver(registry)
    executor = ResolvingTaskExecutor(resolver)
    return executor


def _external_count(provider):
    """How many actual external mutations the provider applied (the dedup-invariant)."""
    return provider.apply_count


# --------------------------------------------------------------------------- #
# 1. normal mutation
# --------------------------------------------------------------------------- #
def test_normal_mutation_records_exactly_once():
    provider = MockHttpMutationProvider(idempotent=True)
    executor = _wire(provider)
    ledger = ExecutionLedger()

    obs = executor.execute(_step(), ("run-1", "t1", 0), ledger=ledger)

    assert obs.success is True
    assert _external_count(provider) == 1
    # fact recorded under the verbatim execution_key (C2)
    assert ledger.get(("run-1", "t1", 0)) is not None
    assert ledger.get(("run-1", "t1", 0)).success is True


# --------------------------------------------------------------------------- #
# 2. ledger HIT: provider not called again
# --------------------------------------------------------------------------- #
def test_ledger_hit_skips_provider():
    provider = MockHttpMutationProvider(idempotent=True)
    executor = _wire(provider)
    ledger = ExecutionLedger()

    executor.execute(_step(), ("run-1", "t1", 0), ledger=ledger)
    assert _external_count(provider) == 1

    # resume on the SAME attempt with the SAME ledger -> HIT, no provider call
    executor.execute(_step(), ("run-1", "t1", 0), ledger=ledger)
    assert _external_count(provider) == 1  # still exactly one external apply


# --------------------------------------------------------------------------- #
# 3. ledger MISS + external token HIT (the crash window)
# --------------------------------------------------------------------------- #
def test_ledger_miss_external_token_dedupe():
    # the external store survives the "crash" — it IS the external system
    external_store = {}
    provider = MockHttpMutationProvider(idempotent=True, external_store=external_store)
    executor = _wire(provider)

    # FIRST call with ledger=None: the external commit happens, but no ledger fact is
    # written (simulates crash between external commit and ledger.put — the C4 window).
    executor.execute(_step(), ("run-1", "t1", 0), ledger=None)
    assert _external_count(provider) == 1

    # RESUME with a fresh ledger: ledger MISS -> provider invoked again, but the external
    # token is already present -> dedupe, no re-apply.
    ledger2 = ExecutionLedger()
    obs = executor.execute(_step(), ("run-1", "t1", 0), ledger=ledger2)
    assert _external_count(provider) == 1  # no duplicate side effect
    assert obs.success is True
    # the fresh ledger now records the (deduped) fact
    assert ledger2.get(("run-1", "t1", 0)) is not None


# --------------------------------------------------------------------------- #
# 4a. post-commit transient, idempotent -> resolver retry is SAFE
# --------------------------------------------------------------------------- #
def test_post_commit_transient_safe_retry_when_idempotent():
    provider = MockHttpMutationProvider(
        idempotent=True, post_commit_transient_first_call=True
    )
    executor = _wire(provider, retry=2)
    ledger = ExecutionLedger()

    obs = executor.execute(_step(), ("run-1", "t1", 0), ledger=ledger)

    # the resolver retried on TransientError; the external dedupe absorbed the retry
    assert obs.success is True
    assert _external_count(provider) == 1  # applied once, never twice
    assert provider.invocation_count >= 2  # a retry actually occurred


# --------------------------------------------------------------------------- #
# 4b. post-commit unknown, NON-idempotent -> NO blind retry
# --------------------------------------------------------------------------- #
def test_unknown_post_commit_no_blind_retry_when_not_idempotent():
    provider = MockHttpMutationProvider(
        idempotent=False, post_commit_transient_first_call=True
    )
    executor = _wire(provider, retry=2)
    ledger = ExecutionLedger()

    obs = executor.execute(_step(), ("run-1", "t1", 0), ledger=ledger)

    # provider refused to raise TransientError (would double-apply a non-idempotent call);
    # the resolver therefore did NOT retry -> exactly one external apply, no false success.
    assert _external_count(provider) == 1
    assert obs.success is False
    assert "unknown_post_commit" in obs.error


# --------------------------------------------------------------------------- #
# 5. I1 / I2 / I3 regression guards
# --------------------------------------------------------------------------- #
def _code_without_docstrings(path: pathlib.Path) -> str:
    """Parse a module and blank out standalone string expressions (docstrings) so that
    invariant scans cannot trip on prose describing the pass-through flow."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            node.value.value = ""
    return ast.unparse(tree)


def test_i1_executor_depends_on_resolver_not_registry():
    """I1: the executor selects via the Resolver; it must NOT name a provider registry.

    The scan runs on code with docstrings blanked, so the I3 prose
    ("passed verbatim through ``resolve`` → ``provider.invoke``") does not trip the
    "never calls a provider directly" guard — that phrase describes the resolver→provider
    pass-through, not a direct executor→provider call.
    """
    src = _code_without_docstrings(
        pathlib.Path(__file__).resolve().parents[1]
        / "anima" / "runtime" / "resolving_executor.py"
    )
    # selection authority lives in the resolver: the executor holds and calls the resolver
    assert "CapabilityResolver" in src and "self._resolver" in src
    assert "self._resolver.resolve" in src  # it delegates selection, not calls a provider
    # the executor owns NO provider registry / provider list of its own
    assert "CapabilityRegistry" not in src
    assert "self._providers" not in src
    # and it never invokes a provider directly (no `provider.invoke(` call expression)
    assert "provider.invoke(" not in src


def test_i2_provider_touches_no_ledger_run_workflow():
    """I2: a side-effecting provider must not reference ledger / run / workflow state."""
    src = (
        pathlib.Path(__file__).resolve().parents[1]
        / "anima" / "runtime" / "providers" / "mock_http_mutation_provider.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "execution_ledger",
        "ExecutionLedger",
        "WorkflowRunStore",
        "report_task_outcome",
        "store.save_run",
        "workflow.transition",
    ):
        assert forbidden not in src, f"provider must not reference {forbidden!r} (I2)"


def test_i3_execution_key_passed_verbatim_to_provider():
    """I3: the orchestrator's execution identity reaches the provider unchanged."""
    captured = {}
    provider = MockHttpMutationProvider(idempotent=True)
    orig = provider.invoke

    def spy(capability, arguments, execution_key):
        captured["key"] = execution_key
        return orig(capability, arguments, execution_key)

    provider.invoke = spy
    executor = _wire(provider)
    key = ("run-X", "t1", 0)
    executor.execute(_step(), key, ledger=ExecutionLedger())

    assert captured["key"] == key  # verbatim, unchanged
    assert len(captured["key"]) == 3  # exactly (run_id, task_id, attempt)
    assert _external_count(provider) == 1
