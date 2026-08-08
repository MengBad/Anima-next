"""F5.6 — Startup Recovery Coordinator: discover interrupted runs at boot.

F5.5 gave a coordinator that resumes EXACTLY ONE crashed run. F5.6 closes gap C:
at process startup, enumerate every persisted run left in a non-terminal (RUNNING)
state, and recover_and_resume each through the F5.5 ResumeCoordinator.

    restart
        |
        v
    StartupRecoveryCoordinator.recover_interrupted_runs()
        |
        v  (for each persisted RUNNING run_id)
    ResumeCoordinator.recover_and_resume(store, planning, run_id)
        |
        v
    PlanOrchestrator._resume_run (F5.3 executor dispatch + ledger idempotency gate)

Design rules (consistent with the frozen boundary set + ADR-0003):

- **Separate module, not recovery.py.** This coordinator only CALLS ``recover_run`` (via
  the F5.5 ``ResumeCoordinator``) — it never redefines or alters F4 recovery behavior, and
  it does NOT start a scheduler inside ``recovery.py`` (F4 primitive purity preserved; the
  PM's boundary rule: "allow bootstrap call coordinator; forbid recovery.py start
  scheduler").
- **Discovery without modifying the store.** ``WorkflowRunStore`` is a frozen snapshot
  primitive with no list API (F4.1). We discover run ids by globbing the store's persisted
  directory (the store's own path convention ``workflow_runs/{run_id}.json``) and load each
  via ``store.load`` (proper deserialization + deep copy). We do NOT add a list method to
  the frozen store, nor do we reach into ``PersistenceProvider`` write paths.
- **Interrupted = persisted status RUNNING.** A crash leaves the last snapshot RUNNING
  (F5.3 RUNNING durability: RUNNING is persisted BEFORE the side effect, so a crash mid-run
  leaves a recoverable RUNNING, never an invisible PENDING). At startup no run is in-flight,
  so any RUNNING run is a crash survivor. Terminal states (COMPLETED / FAILED / ABANDONED)
  are skipped — nothing to resume.
- **I1 / I2 / I3 untouched.** This coordinator only enumerates + delegates; it performs no
  capability selection, writes no run/workflow state, produces no Observation, and preserves
  execution identity (delegates ``source_run_id`` = original run id verbatim).
- **Per-run isolation.** One failing run must not block recovery of the others; results are
  collected and returned with failures recorded for operator review.
- **Idempotency / orphan note (deferred, NOT a correctness defect).** ``recover_and_resume``
  issues a NEW run_id and leaves the ORIGINAL crash snapshot RUNNING on disk (the frozen
  store has no status-writer, by design). So the original RUNNING record is re-enumerated on
  every boot. This is SAFE (ledger dedup guarantees no duplicate side effect — I3) and the
  original record is preserved as forensic crash evidence; a future slice may add a recovery
  marker. We do NOT write ``run.status`` (that would bypass the Workflow Authority).

Public surface for bootstrap:

    recover_interrupted_runs(store, planning, orchestrator)

No new orchestrator public method, no ``recovery.py`` change, no ``bootstrap.py`` change.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, NamedTuple

from anima.runtime.resume_coordinator import ResumeCoordinator


class RunRecoveryResult(NamedTuple):
    """Outcome of recovering one interrupted run at startup."""

    original_run_id: str
    status: str                       # "recovered" | "failed"
    final_status: "str | None" = None
    error: "str | None" = None


class StartupRecoveryCoordinator:
    """Enumerate interrupted runs at startup and recover_and_resume each.

    Discovery is read-only against the store's persisted directory; recovery is fully
    delegated to the F5.5 ResumeCoordinator (which owns the recover_run -> _resume_run
    chain). The coordinator never writes run or workflow state itself.
    """

    #: persisted run snapshot file name convention (mirrors WorkflowRunStore._path)
    _RUN_DIR = "workflow_runs"
    _RUN_SUFFIX = ".json"

    @classmethod
    def _discover_run_ids(cls, store) -> List[str]:
        """Glob the store's persisted run directory for run ids.

        Does NOT add a list API to the frozen ``WorkflowRunStore`` — we read the directory
        the store itself writes to (``workflow_runs/{run_id}.json``) and let ``store.load``
        perform the actual deserialization. Run ids may contain dots, so we strip exactly
        the ``.json`` suffix rather than using ``Path.stem``.
        """
        base = Path(store.persistence.data_dir) / cls._RUN_DIR
        if not base.exists():
            return []
        ids: List[str] = []
        for p in sorted(base.glob(f"*{cls._RUN_SUFFIX}")):
            ids.append(p.name.removesuffix(cls._RUN_SUFFIX))
        return ids

    def recover_interrupted_runs(self, store, planning, coordinator) -> List[RunRecoveryResult]:
        """Find every persisted RUNNING run and recover_and_resume it.

        Returns one :class:`RunRecoveryResult` per discovered interrupted run. Failures are
        captured, not raised, so one bad run does not block recovery of the others. Terminal
        (non-RUNNING) runs are skipped silently — they are already concluded.
        """
        results: List[RunRecoveryResult] = []
        for run_id in self._discover_run_ids(store):
            try:
                run = store.load(run_id)
            except Exception as exc:  # noqa: BLE001 — unloadable snapshot is an anomaly
                results.append(RunRecoveryResult(
                    original_run_id=run_id, status="failed",
                    error=f"cannot load run snapshot: {exc}",
                ))
                continue
            # Interrupted = persisted RUNNING (F5.3 durability). Everything else is terminal
            # or otherwise concluded and is not ours to resume.
            if getattr(run.status, "value", None) != "running":
                continue
            try:
                final = coordinator.recover_and_resume(store, planning, run_id)
                results.append(RunRecoveryResult(
                    original_run_id=run_id,
                    status="recovered",
                    final_status=getattr(getattr(final, "status", None), "value", None),
                ))
            except Exception as exc:  # noqa: BLE001 — surface, don't abort the batch
                results.append(RunRecoveryResult(
                    original_run_id=run_id, status="failed", error=str(exc)
                ))
        return results


def recover_interrupted_runs(store, planning, orchestrator) -> List[RunRecoveryResult]:
    """Bootstrap convenience entry point: build the F5.5 ResumeCoordinator from a
    fully-wired orchestrator (executor + ledger) and recover every interrupted run.

    Raises :class:`ResumeError` if the orchestrator is not wired with BOTH executor and
    ledger (recovery has nothing to dispatch a PENDING task through) — surfaced as a
    startup configuration error rather than a silent no-op.
    """
    coordinator = ResumeCoordinator(orchestrator)
    return StartupRecoveryCoordinator().recover_interrupted_runs(store, planning, coordinator)
