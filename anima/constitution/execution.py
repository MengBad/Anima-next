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

from dataclasses import dataclass

from anima.constitution.errors import ConstitutionViolation
from anima.constitution.state import (
    ExecutionContext,
    ExecutionResult,
    ExecutionStatus,
    Plan,
    Task,
    TaskRunStatus,
    TaskState,
)


@dataclass
class ExecutionStep:
    """Runtime-side representation of a single carried Task (M1.7 carrier internal).

    This is the *adapter output*, not the canonical Plan contract. It is produced from a
    canonical RFC-0002 §5 ``Task`` by ``task_to_execution_step`` so the Execution Runtime
    reads a stable, typed shape. The canonical ``Plan.task_graph`` stays ``list[Task]``
    (RFC-0002 §5); a dict is never re-introduced as the canonical representation (T4.1 rule).
    """
    id: str
    title: str
    intent: str
    depends_on: list
    capability_need: dict
    estimates: dict


def task_to_execution_step(task: Task) -> ExecutionStep:
    """Explicit, auditable mapping: canonical ``Task`` -> runtime execution representation.

    Every field is assigned by name (no ``ExecutionStep(**task.__dict__)`` implicit
    unpacking) so the conversion stays reviewable as the canonical §5 contract evolves.
    """
    return ExecutionStep(
        id=task.id,
        title=task.title,
        intent=task.intent,
        depends_on=list(task.depends_on),
        capability_need=dict(task.capability_need),
        estimates=dict(task.estimates),
    )


def execute_plan(plan: Plan, context: ExecutionContext) -> ExecutionResult:
    """Carry a Plan's Tasks into the (simulated) Universe — the consumption act.

    Reads the Plan read-only; emits an ``ExecutionResult``. Must NOT mutate ``plan``, the Goal
    behind it, Drive, or ``context`` (RFC-0107 C1/C2/C3). The carrying is simulated for M1.7:
    we walk ``plan.task_graph`` (canonical RFC-0002 §5 Tasks) and accumulate a lesson per
    carried Task. A real carrier would dispatch each Task via Capability→Provider
    (RFC-0100 §1.18–§1.19); that infra is deliberately out of scope here (RFC-0107 NG1).
    """
    # Carry each canonical Task through the explicit adapter (RFC-0002 §5 -> runtime step).
    # The adapter owns the representation boundary: the canonical Plan stays list[Task]; the
    # Runtime reads a typed ExecutionStep, never a dict (T4.1). We read task_graph read-only
    # and never write the Plan.
    carried_steps = [task_to_execution_step(t) for t in plan.task_graph]
    carried = [step.id for step in carried_steps]
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


# ---------------------------------------------------------------------------
# E1 — the ExecutionResult -> TaskState projection (RFC-0003 §5 bridge, GAP E-A)
# ---------------------------------------------------------------------------
#: Execution statuses the adapter MAY project onto per-Task TaskStates (E1 ruling).
#: SUCCESS and FAILED are run-level verdicts that project uniformly; every other status
#: is non-determinate at the Task level — PARTIAL cannot say *which* Tasks succeeded, and
#: PENDING / RUNNING are not verdicts at all. The adapter refuses rather than guesses.
_PROJECTABLE_RESULT_STATUSES: frozenset[ExecutionStatus] = frozenset(
    {ExecutionStatus.SUCCESS, ExecutionStatus.FAILED}
)


def execution_result_to_task_states(
    result: ExecutionResult, task_ids: list[str]
) -> list[TaskState]:
    """Project an aggregate ``ExecutionResult`` onto one ``TaskState`` per ``task_id`` (E1).

    The bridge RFC-0003 §5 expects: it turns the Execution Runtime's *run-level* verdict into
    the *per-Task* snapshots ``report_task_outcome`` consumes. It is a **projection**, not a
    bijection — an aggregate result carries no per-Task truth (GAP E-A), so every Task in
    ``task_ids`` receives the same verdict, and fields the aggregate cannot know (``attempts``,
    ``result``, ``error``) are left at their initial values.

    Projection rules (fixed in the E1 ruling, RFC-0107 / RFC-0003 boundary):
    - ``SUCCESS`` -> every ``task_id`` gets a ``TaskState`` with ``status=DONE``.
    - ``FAILED``  -> every ``task_id`` gets a ``TaskState`` with ``status=FAILED``.
    - ``PARTIAL`` / ``PENDING`` / ``RUNNING`` -> refused with ``ConstitutionViolation``:
      an aggregate PARTIAL cannot name which Tasks succeeded, and PENDING/RUNNING are not
      verdicts; projecting a guess would fabricate run-state, so the adapter refuses.
    - empty ``task_ids`` -> ``[]`` (nothing to project; the verdict is still validated).

    Boundary guarantees (same discipline as D3–D5b, carried into the Execution side):
    - **Takes ``task_ids``, never a ``Plan``.** ``start_run`` consumed the Plan once and
      dropped it (F-02) — nobody in the run domain holds the Plan, and the adapter must not
      force a caller back into the Planning registry. ``task_ids`` come naturally from the
      run's own ``task_states[*].task_id`` (the set ``_require_task_state`` already trusts).
    - **Reads, never writes.** A fresh ``TaskState`` is constructed per id; nothing is mutated:
      no ``result``/``run`` attribute assignment, no Plan touch, no lifecycle call. This is a
      pure ``Execution -> state`` mapping — it grants Execution no run-state writer, and it is
      one-way (only here does Execution name ``TaskState``; Workflow never imports this).
    - **No lifecycle authority.** It never calls ``report_task_outcome`` / ``_transition`` or
      any Workflow operation; wiring who *reports* these snapshots is a later slice (E2+).
    """
    if result.status not in _PROJECTABLE_RESULT_STATUSES:
        allowed = "/".join(sorted(s.value.upper() for s in _PROJECTABLE_RESULT_STATUSES))
        raise ConstitutionViolation(
            f"execution_result_to_task_states: cannot project {result.status.value!r} "
            f"ExecutionResult onto TaskStates; the aggregate carries no per-Task truth "
            f"(projectable: {allowed})"
        )
    target = (
        TaskRunStatus.DONE
        if result.status is ExecutionStatus.SUCCESS
        else TaskRunStatus.FAILED
    )
    return [
        TaskState(
            task_id=task_id,
            status=target,
            attempts=0,   # aggregate carries no retry count
            result={},    # aggregate carries no per-Task payload
            error="",     # aggregate carries no per-Task failure reason
        )
        for task_id in task_ids
    ]


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
