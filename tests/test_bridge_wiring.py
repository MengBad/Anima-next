"""P7.6 — Execution Bridge Wiring Implementation (RED contract).

Controlled wiring: PlanOrchestrator -> RealTaskExecutor -> sandbox.write -> ledger.

Acceptance (all GREEN):
    A  Injection wiring   - orchestrator drives RealTaskExecutor; sandbox file written
    B  Legacy preservation- executor=None still uses the ExecutionRuntime legacy path
    C  Key propagation    - execution_run_id reaches key -> ledger -> provider verbatim
    D  Ledger gate        - 1st write=True, 2nd (same key) write=False, no double side effect
    E  Sandbox boundary   - ../ absolute escape paths are refused (FAILED observation)
    F  Non-escalation     - execution facts never become authorized/production/deployed
"""
from __future__ import annotations

import pathlib
import re

import pytest

from anima.constitution.execution import ExecutionRuntime
from anima.constitution.planning_interface import PlanningInterface
from anima.constitution.state import (
    Drive,
    Goal,
    GoalLifecycle,
    OrientationAxis,
    PlannerContext,
    RunStatus,
    Task,
)
from anima.constitution.workflow import WorkflowInterface
from anima.runtime.execution_ledger import ExecutionLedger


def _active_plan(planning: PlanningInterface) -> str:
    """Create + activate a canonical Plan via the REAL Planning Authority path."""
    goal = Goal(
        target="understand X",
        success_criterion="can explain X",
        horizon="weeks",
        priority=0.8,
        lifecycle=GoalLifecycle.ACTIVE,
    )
    drive = Drive(
        orientation={
            OrientationAxis.APPROACH: 0.6,
            OrientationAxis.AVOID: 0.1,
            OrientationAxis.FOCUS: 0.5,
        }
    )
    ctx = PlannerContext(capabilities=["sandbox"], environment={"safe_to_retry": True})
    plan = planning.request_plan("goal#understand-X", goal=goal, drive=drive, context=ctx)
    planning.activate_plan(plan.id)
    return plan.id


def _sandbox_plan(planning: PlanningInterface, path="out.txt", content="hello", **need_overrides) -> str:
    """ACTIVE plan whose single Task targets sandbox.write (test-only whitebox)."""
    from dataclasses import replace

    plan_id = _active_plan(planning)
    plan = planning.read_plan(plan_id)
    need = {"capability": "sandbox.write", "args": {"path": path, "content": content}, "context": {}}
    need.update(need_overrides)
    rewritten = replace(
        plan,
        task_graph=[
            Task(
                id="t-sw-1",
                title="sandbox write",
                intent="persist derived note",
                capability_need=need,
                depends_on=[],
                estimates={},
            )
        ],
    )
    planning._registry[plan_id] = rewritten  # whitebox injection (test-only)
    return plan_id


def _build(sandbox_root, **kw):
    """Controlled bootstrap helper (RED until implemented)."""
    from anima.runtime.bridge_bootstrap import build_controlled_orchestrator

    return build_controlled_orchestrator(sandbox_root, **kw)


# ---------------------------------------------------------------------------
# A - Injection wiring (orchestrator drives RealTaskExecutor, file written)
# ---------------------------------------------------------------------------
def test_a_injection_wiring(tmp_path):
    planning = PlanningInterface()
    plan_id = _sandbox_plan(planning)
    orch = _build(tmp_path, planning=planning)

    final = orch.run_plan(plan_id, None)

    assert final.status is RunStatus.COMPLETED
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hello"


# ---------------------------------------------------------------------------
# B - Legacy preservation (executor=None -> ExecutionRuntime legacy path)
# ---------------------------------------------------------------------------
def test_b_legacy_preservation():
    planning = PlanningInterface()
    plan_id = _active_plan(planning)
    orch = _build(
        None, planning=planning, executor=None,  # legacy fallback (no executor)
    )

    final = orch.run_plan(plan_id, None)

    assert final.status is RunStatus.COMPLETED  # legacy simulated path still works
    assert orch._executor is None


# ---------------------------------------------------------------------------
# C - Key propagation (execution_run_id -> key -> ledger -> provider verbatim)
# ---------------------------------------------------------------------------
def test_c_key_propagation(tmp_path):
    planning = PlanningInterface()
    plan_id = _sandbox_plan(planning)
    orch, (executor, ledger) = _build(tmp_path, planning=planning, return_parts=True)

    final = orch.run_plan(plan_id, None)

    # the F5 key = (run_id, task.id, attempts) reached the ledger
    key = (final.id, "t-sw-1", 0)
    cached = ledger.get(key)
    assert cached is not None and cached.success is True
    # the C2 external token in the sandbox derived from the SAME key
    from anima.runtime.providers.sandbox_write_provider import SandboxWriteProvider
    from anima.runtime.capability_resolver import CapabilityResolver

    resolver = executor.resolver
    assert isinstance(resolver, CapabilityResolver)
    assert (tmp_path / ".tokens").exists()  # marker created under the sandbox root


# ---------------------------------------------------------------------------
# D - Ledger gate (1st write=True, 2nd same key write=False, no double side effect)
# ---------------------------------------------------------------------------
def test_d_ledger_gate(tmp_path):
    planning = PlanningInterface()
    plan_id = _sandbox_plan(planning)
    orch, (executor, ledger) = _build(tmp_path, planning=planning, return_parts=True)

    final = orch.run_plan(plan_id, None)

    # ledger HIT on the ORIGINAL run key: re-execute with a tampered content is
    # served from the cache -> no provider call, file unchanged.
    from anima.runtime.executor import ExecutionStep

    step = ExecutionStep(
        id="t-sw-1", title="sandbox write", intent="persist",
        capability_need={
            "capability": "sandbox.write",
            "args": {"path": "out.txt", "content": "CHANGED"},
            "context": {},
        },
        depends_on=[],
        estimates={},
    )
    key = (final.id, "t-sw-1", 0)
    cached = executor.execute(step, key, ledger)
    assert cached.success is True
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hello"  # unchanged

    # provider-level C2 external idempotency: same key -> written=False (authoritative dedupe)
    from anima.runtime.providers.sandbox_write_provider import SandboxWriteProvider

    provider = SandboxWriteProvider(str(tmp_path))
    first = provider.invoke(
        "sandbox.write", {"path": "dup.txt", "content": "v1"}, ("run-1", "t3", 0)
    )
    second = provider.invoke(
        "sandbox.write", {"path": "dup.txt", "content": "v2"}, ("run-1", "t3", 0)
    )
    assert first.data["written"] is True
    assert second.data["written"] is False
    assert (tmp_path / "dup.txt").read_text(encoding="utf-8") == "v1"


# ---------------------------------------------------------------------------
# E - Sandbox boundary (escape paths -> FAILED observation)
# ---------------------------------------------------------------------------
def test_e_sandbox_boundary(tmp_path):
    planning = PlanningInterface()
    plan_id = _sandbox_plan(planning, path="../evil.txt", content="x")
    orch = _build(tmp_path, planning=planning)

    final = orch.run_plan(plan_id, None)

    # the escape is a FAILED task -> run FAILED (F-04 concludes on first failure)
    assert final.status is RunStatus.FAILED
    assert not (tmp_path.parent / "evil.txt").exists()


# ---------------------------------------------------------------------------
# F - Non-escalation (execution facts never become authorization)
# ---------------------------------------------------------------------------
def test_f_non_escalation(tmp_path):
    planning = PlanningInterface()
    plan_id = _sandbox_plan(planning)
    orch, (executor, ledger) = _build(tmp_path, planning=planning, return_parts=True)

    final = orch.run_plan(plan_id, None)

    vocab = " ".join(
        (final.status.value, str([s.status.value for s in final.task_states]))
    ).lower()
    for token in ("approved", "authorized", "deployed", "production"):
        assert token not in vocab
    cached = ledger.get((final.id, "t-sw-1", 0))
    assert cached is not None and cached.success is True
    payload = " ".join((str(cached.result), cached.error, str(cached.metadata))).lower()
    for token in ("approved", "authorized", "deployed", "production"):
        assert token not in payload


# ---------------------------------------------------------------------------
# G - Static boundary (bootstrap helper stays in F5 domain)
# ---------------------------------------------------------------------------
def test_g_static_boundary():
    src = pathlib.Path("anima/runtime/bridge_bootstrap.py").read_text(encoding="utf-8")
    parts = src.split('"""')
    code_body = parts[2] if len(parts) >= 3 else src
    forbidden = [
        "CapabilityGateway", "gateway", "tools", "approve_and_execute",
        "capability_mode", "webui", "napcat", r"\brequests\b", "httpx",
        "subprocess", r"\bssh\b", "paramiko",
    ]
    for needle in forbidden:
        assert not re.search(needle, code_body), f"forbidden token: {needle}"
    allowed_prefixes = ("anima.runtime.", "anima.constitution.")
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith(("from ", "import ")):
            target = stripped.split(" import ")[0].replace("from ", "").strip()
            if target.startswith("anima."):
                assert target.startswith(allowed_prefixes), f"import escapes F5: {target!r}"
