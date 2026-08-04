"""Execution Engine (M1.7) — the Execution Runtime's consumption act.

RFC-0107: ``Execution`` is a **Process/Event** (an act carrying a Plan's Tasks into the
Universe), *not* a Constitution State. The Execution Runtime is an **infrastructure carrier**,
explicitly **not** a 7th Constitution Authority (the ``X`` firewall, RFC-0107 §6.1/§6.2).

- ``execute_plan(plan, context)`` is the consumption act: it *reads* the Plan read-only and
  *emits* an ``ExecutionResult`` (event output). It is deterministic given ``(plan, context)``
  and **mutates nothing** — not the Plan, not the Goal, not Drive, not the Context (RFC-0107
  C1/C2/C3). The real carrier would invoke Capability→Provider per Task; M1.7 *simulates* the
  carrying to prove the boundary without building infra (scheduler / tool-executor / retry
  policy are explicitly NOT in scope, RFC-0107 NG1).
- ``ExecutionRuntime`` is the carrier class: it holds runtime capabilities (infra config) and
  ``run(plan, context)`` delegates to ``execute_plan``. It owns the *act*, never a Constitution
  state, and never edits Goal/Drive/Plan (RFC-0107 C2).

**0100C watchpoint (RFC-0107 C4):** transaction / failure / rollback / atomicity live HERE at
build time. This implementation needs *no* transaction boundary — carrying is modeled as a
pure read → emit, and the Plan is consumed (not mutated). Therefore **no observed contradiction**
surfaces: RFC-0100C stays deferred (7th). If a future, *real* carrier needs atomicity the
Foundation cannot express, that routes via Foundation Evolution F1 — not a silent expansion here.
"""
from __future__ import annotations

from anima.constitution.errors import ConstitutionViolation
from anima.constitution.state import (
    ExecutionContext,
    ExecutionResult,
    ExecutionStatus,
    Plan,
)


def execute_plan(plan: Plan, context: ExecutionContext) -> ExecutionResult:
    """Carry a Plan's Tasks into the (simulated) Universe — the consumption act.

    Reads the Plan read-only; emits an ``ExecutionResult``. Must NOT mutate ``plan``, the Goal
    behind it, Drive, or ``context`` (RFC-0107 C1/C2/C3). The carrying is simulated for M1.7:
    we walk ``plan.ordering`` and accumulate a lesson per carried Task. A real carrier would
    dispatch each Task via Capability→Provider (RFC-0100 §1.18–§1.19); that infra is deliberately
    out of scope here (RFC-0107 NG1).
    """
    # Walk the planned ordering read-only — we read plan.ordering, never write the Plan.
    carried = list(plan.ordering)
    status = ExecutionStatus.SUCCESS if carried else ExecutionStatus.FAILED

    lessons = [
        f"carried {len(carried)} task(s) toward '{plan.goal_target}'",
        f"drive emphasis at plan time = {plan.emphasis!r}",
    ]
    if plan.branching:
        lessons.append(f"branched on {sorted(plan.branching)}")
    if plan.rollback:
        lessons.append(f"rollback plan ready: {sorted(plan.rollback)}")

    # Deterministic ref of the consumed Plan shape (audit only — NOT a Goal reference, C1).
    plan_ref = "|".join(carried) or plan.context_ref or plan.goal_target

    return ExecutionResult(
        plan_ref=plan_ref,
        status=status,
        outcome=f"executed plan for '{plan.goal_target}'",
        lessons=lessons,
    )


class ExecutionRuntime:
    """Infrastructure carrier of the Execution act (RFC-0107 §6.1).

    NOT a Constitution Authority (the ``X`` firewall, §6.2). Owns the *act* of carrying a Plan
    via Capability→Provider; owns NO Constitution state. It reads Plan + Context read-only and
    emits an ``ExecutionResult``. It may retry/route via infra, but never edits Goal/Drive/Plan
    (RFC-0107 C2). The 0100C transaction question lives HERE at build time (C4): if atomicity /
    rollback cannot be expressed by the Foundation, that is an *observed contradiction* →
    Foundation Evolution F1, not a silent expansion in code.
    """

    def __init__(self, capabilities: list[str] | None = None) -> None:
        self._capabilities = list(capabilities or [])

    def run(self, plan: Plan, context: ExecutionContext | None = None) -> ExecutionResult:
        """Consume ``plan`` and carry it, emitting a Result. Does not mutate the Plan."""
        ctx = context or ExecutionContext(capabilities=list(self._capabilities))
        return execute_plan(plan, ctx)

    def consume_plan(self, plan: Plan, context: ExecutionContext | None = None) -> ExecutionResult:
        """Alias for ``run`` — names the consumption contract explicitly (RFC-0107 §5.1)."""
        return self.run(plan, context)
