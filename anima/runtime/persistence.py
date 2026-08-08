"""Runtime Persistence primitive — durable snapshot of a ``WorkflowRun`` (F4.1).

Implements the F3 Persistence Boundary decision:

- **Durable object is ONLY ``WorkflowRun``** (RFC-0003 §8 G4: "Run-state is persisted for
  recovery"). Plan / ``ExecutionResult`` / ``WorkflowExecutionResult`` stay transient —
  Plan is recomputed (RFC-0106 V5), envelopes/ledgers are observations, never a recovery
  source.
- **Recovery source is ONLY the persisted ``WorkflowRun``** (its ``task_states``). The
  store provides ``save`` / ``load``; the *timing policy* (when to save) and the *recovery
  orchestration* (how a crash restores a run) are deliberately OUT of scope here (F4.2 /
  F4.3) — this slice is the durable representation primitive only.
- **Persistence is NOT a second state machine.** This module has no lifecycle mutation
  authority: it reads a ``WorkflowRun`` and serializes it; it never writes ``run.status``,
  never calls ``_transition``, never offers ``update_status`` / ``complete`` / ``transition``
  style operations. The single writer of run-state stays ``WorkflowInterface._transition``;
  the store only makes its output durable.
- **Snapshot, not handle.** Serialization is an explicit by-name mapping (mirroring the
  ``plan_to_run_tasks`` / ``outcome_to_task_state`` reviewable-adapter discipline in
  ``workflow.py``): no ``asdict`` wholesale, no ``**dict`` unpacking, so the mapping stays
  auditable as the §5 contract evolves. ``result`` payloads are deep-copied so a loaded run
  never aliases the serialized dict.

Storage: a single JSON file per run under a configurable directory, written atomically
via the existing ``PersistenceProvider`` (tempfile + fsync + os.replace).
"""
from __future__ import annotations

from dataclasses import dataclass

from anima.constitution.state import (
    PlanRef,
    RunStatus,
    TaskRunStatus,
    TaskState,
    WorkflowRun,
)


@dataclass
class WorkflowRunStore:
    """Durable snapshot primitive for ``WorkflowRun`` (F4.1).

    Reads run-state and writes a durable representation; it owns no lifecycle authority.
    """

    persistence: object  # PersistenceProvider (duck-typed to keep this slice dependency-light)

    #: JSON file name for a run's snapshot: f"workflow_runs/{run_id}.json"
    def _path(self, run_id: str) -> str:
        return f"workflow_runs/{run_id}.json"

    # ---- serialization (explicit by-name mapping, reviewable) ----

    def _run_to_dict(self, run: WorkflowRun) -> dict:
        return {
            "id": run.id,
            "contract_version": run.contract_version,
            "plan_ref": {
                "plan_id": run.plan_ref.plan_id,
                "contract_version": run.plan_ref.contract_version,
            },
            "goal_ref": run.goal_ref,
            "status": run.status.value,
            "task_states": [
                {
                    "task_id": state.task_id,
                    "status": state.status.value,
                    "attempts": state.attempts,
                    "result": dict(state.result),      # deep copy: no alias into the run
                    "error": state.error,
                }
                for state in run.task_states
            ],
            "current_task_id": run.current_task_id,
            "checkpoint_ref": run.checkpoint_ref,
            "retry_policy": dict(run.retry_policy),
            "schedule_ref": run.schedule_ref,
            "budget_ref": run.budget_ref,
            "context_ref": run.context_ref,
            "created_at": run.created_at,
            "completed_at": run.completed_at,
        }

    def _dict_to_run(self, data: dict) -> WorkflowRun:
        plan_ref = PlanRef(
            plan_id=data["plan_ref"]["plan_id"],
            contract_version=data["plan_ref"]["contract_version"],
        )
        task_states = [
            TaskState(
                task_id=state["task_id"],
                status=TaskRunStatus(state["status"]),
                attempts=int(state["attempts"]),
                result=dict(state["result"]),
                error=state["error"],
            )
            for state in data["task_states"]
        ]
        return WorkflowRun(
            id=data["id"],
            contract_version=data["contract_version"],
            plan_ref=plan_ref,
            goal_ref=data["goal_ref"],
            status=RunStatus(data["status"]),
            task_states=task_states,
            current_task_id=data["current_task_id"],
            checkpoint_ref=data["checkpoint_ref"],
            retry_policy=dict(data["retry_policy"]),
            schedule_ref=data["schedule_ref"],
            budget_ref=data["budget_ref"],
            context_ref=data["context_ref"],
            created_at=data["created_at"],
            completed_at=data["completed_at"],
        )

    # ---- operations (snapshot, never lifecycle mutation) ----

    def save(self, run: WorkflowRun) -> None:
        """Persist a snapshot of the run. Read-only w.r.t. run-state: never writes status."""
        self.persistence.write_json(self._path(run.id), self._run_to_dict(run))

    def load(self, run_id: str) -> WorkflowRun:
        """Restore the run snapshot, or raise ``KeyError`` if none was persisted.

        Restoring a run is a *read* — the restored object's ``status`` is whatever the
        snapshot recorded; this store never re-transitions it (the Workflow Authority,
        or a future F4.3 recovery orchestrator, decides how a restored run resumes).
        """
        data = self.persistence.read_json(self._path(run_id))
        if data is None:
            raise KeyError(f"WorkflowRun snapshot not found: {run_id!r}")
        return self._dict_to_run(data)

    def exists(self, run_id: str) -> bool:
        """Whether a snapshot exists for this run (read-only probe)."""
        return self.persistence.read_json(self._path(run_id)) is not None
