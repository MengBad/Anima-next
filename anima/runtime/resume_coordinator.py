"""F5.5 — Resume Coordinator: the missing caller that bridges F4 recovery to F5 resume.

F4 ``recover_run`` restores a crashed run's lifecycle but deliberately leaves its
PENDING/RUNNING tasks for a CALLER to resume (F4 froze recovery as a *restore
primitive* that must not own execution scheduling). F5.3 gave the orchestrator a
private ``_resume_run`` that drives those tasks through the TaskExecutor with RUNNING
durability + a ledger idempotency gate. This module is the caller the F5.3 design
left open.

    startup / crash
        |
        v
    recover_run()                 # F4 restore primitive (issues a NEW run_id)
        |
        v
    ResumeCoordinator             # F5.5 — THIS slice (separate runtime module)
        |
        v
    PlanOrchestrator._resume_run  # F5.3 per-task executor dispatch

Design rules (consistent with the frozen boundary set):

- **Separate module, not recovery.py.** ``recovery.py`` is frozen; this coordinator only
  *calls* ``recover_run`` (a read import of the primitive) — it never redefines or alters
  recovery behavior. It also does not modify the orchestrator's public surface (still
  exactly ``run_plan``); it calls the private ``_resume_run`` as the designated resume path.
- **I1 — Executor != Capability Selector:** untouched. The coordinator only delegates; it
  performs no capability selection of its own.
- **I2 — Provider != Workflow Authority:** untouched. The coordinator never writes run or
  workflow state; it produces no Observation and issues no ``report_task_outcome``.
- **I3 — execution identity unchanged:** the coordinator passes the ORIGINAL crashed run id
  as ``source_run_id`` so the ledger key's run-component survives ``recover_run``'s NEW
  run_id and pre-crash side-effect facts are found (not duplicated) on resume.

The coordinator handles exactly ONE crashed run per call. Enumerating which crashed run
ids to recover (e.g. from the store at startup) is the caller's job — typically a thin
startup loop that, for each persisted run left RUNNING, calls ``recover_and_resume``.
"""
from __future__ import annotations

from anima.constitution.planning_interface import PlanningInterface
from anima.runtime.orchestrator import PlanOrchestrator
from anima.runtime.persistence import WorkflowRunStore
from anima.runtime.recovery import recover_run


class ResumeError(Exception):
    """Setup/configuration error for the ResumeCoordinator (not a constitution violation)."""


class ResumeCoordinator:
    """Turn a recovered run into a finished run by delegating to the F5 executor.

    Construction requires an orchestrator already wired with an executor + ledger (the
    F5.3 RUNNING durability + F5.4b real Capability Boundary). Without them, resume would
    have nothing to dispatch a PENDING task through, so we refuse early rather than crash
    mid-recovery.
    """

    def __init__(self, orchestrator: "PlanOrchestrator") -> None:
        if orchestrator._executor is None or orchestrator._ledger is None:
            raise ResumeError(
                "ResumeCoordinator requires an orchestrator wired with BOTH executor "
                "and ledger (F5.3 RUNNING durability + F5.4b Capability Boundary); "
                "resume has nothing to dispatch a PENDING task through."
            )
        self._orchestrator = orchestrator

    def resume(self, run, *, source_run_id: str | None = None):
        """Resume ONE already-recovered run through the F5 executor.

        ``source_run_id`` MUST be the ORIGINAL crashed run's id (I3): it seeds the ledger
        key's run-component so side effects recorded before the crash are still found after
        recovery (which issues a NEW run_id) and are not duplicated. Omit it only for a
        non-recovery resume where the run id is unchanged (e.g. after a suspend).
        """
        return self._orchestrator._resume_run(run, source_run_id=source_run_id)

    def recover_and_resume(
        self,
        store: "WorkflowRunStore",
        planning: "PlanningInterface",
        original_run_id: str,
        marker_store=None,
    ):
        """Full startup chain for ONE crashed run: recover_run -> resume.

        Loads the persisted crash snapshot, restores it via the F4 primitive (new run_id,
        terminal tasks replayed), then resumes the PENDING tasks through the F5 executor
        with the ORIGINAL run id as the ledger seed (I3). This is the missing caller the
        F5.3 design left open: recovery restores, the coordinator resumes.

        Callers enumerate which crashed run ids to recover (e.g. from the store at
        startup); this method handles exactly one.

        If ``marker_store`` (a ``RecoveryMarkerStore``) is provided, the recovery lifecycle
        is recorded as a SIDE RECORD (F7.3b): RECOVERING before recovery, RECOVERED on
        success, FAILED on any exception. The marker is NEVER run-state and is never written
        through the Workflow Authority. The ledger (I3) remains the authoritative idempotency
        guard; the marker is forensic + an optimization signal for startup re-enumeration.
        """
        if marker_store is not None:
            # RECOVERING written up-front (new_run_id unknown until recover_run returns);
            # mark_recovered below fills it. If recovery itself fails, the marker stays
            # RECOVERING/FAILED and a later boot retries (ledger still兜底).
            marker_store.write_recovering(original_run_id, "", original_run_id)
        try:
            persisted = store.load(original_run_id)
            restored = recover_run(
                store, self._orchestrator._workflow, planning, persisted
            )
            result = self._orchestrator._resume_run(restored, source_run_id=original_run_id)
            if marker_store is not None:
                marker_store.mark_recovered(original_run_id, restored.id, original_run_id)
            return result
        except Exception as exc:
            if marker_store is not None:
                marker_store.mark_failed(original_run_id, exc)
            raise
