"""Runtime recovery bootstrap (F4.6a) — aggregate recovery of persisted runs.

Provides ``recover_all`` — the runtime-layer entry point that walks every persisted
``WorkflowRun`` snapshot and restores the non-terminal ones. It is the *aggregation*
of the F4.4 ``recover_run`` primitive:

    enumerate snapshots -> filter terminal -> recover_run(each) -> list of recovered

Design rules (frozen in F4.5 / F4.6a authorization):

- **Delegation, not re-implementation.** Bootstrap never copies recovery logic: each
  non-terminal run is handed to ``recover_run`` (the single recovery entry point).
  There is exactly one recovery implementation in the system.
- **Enumeration via public surface only.** ``WorkflowRunStore`` exposes save/load/exists
  (no ``list()`` — and the store API is not to be changed, F4.6a). The snapshot
  directory is reached through the store's PUBLIC ``persistence.data_dir`` handle and
  the documented ``workflow_runs/*.json`` layout. No private attribute is accessed.
- **Terminal runs are never recovered** (F4.5 freeze): completed / failed / cancelled
  snapshots are skipped — a terminal run must not be resurrected (§7.1 terminal has no
  outgoing transition).
- **Failure isolation.** A single run's recovery failure is logged and does not block
  the rest: the recovered-successful runs are returned, failures are recorded.
- **Authority boundary.** Bootstrap writes no run state: no ``_registry``, no
  ``_transition``, no ``.status =``, no ``task_states =``. Every state change happens
  inside the Workflow Authority via ``recover_run``'s public-API replay.
"""
from __future__ import annotations

import logging

from anima.constitution.state import RunStatus
from anima.runtime.persistence import WorkflowRunStore
from anima.runtime.recovery import recover_run

logger = logging.getLogger("anima.runtime.bootstrap")

#: Run states that must NOT be recovered (F4.5 freeze: terminal runs never resurrect).
_TERMINAL_RUN_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
)


def recover_all(store: WorkflowRunStore, workflow, planning) -> list:
    """Restore every non-terminal persisted run; return the successfully recovered runs.

    Steps:

    1. **Enumerate** — scan ``{store.persistence.data_dir}/workflow_runs/*.json``
       (the documented snapshot layout; public handle only).
    2. **Filter** — skip terminal runs (completed / failed / cancelled); recover
       pending / running / suspended.
    3. **Delegate** — each non-terminal snapshot goes through ``recover_run`` (the
       single recovery implementation; this module never re-implements it).
    4. **Isolate** — a failed recovery (e.g. no ACTIVE Plan for the run's goal, or a
       corrupted snapshot) is logged and skipped; the remaining runs still recover.

    The returned list is the recovered runs in snapshot-file order (sorted by name).
    """
    recovered: list = []
    snapshots_dir = store.persistence.data_dir / "workflow_runs"
    if not snapshots_dir.is_dir():
        return recovered

    for snapshot_path in sorted(snapshots_dir.glob("*.json")):
        run_id = snapshot_path.stem
        try:
            persisted = store.load(run_id)
        except Exception:  # noqa: BLE001 - a corrupt/unreadable snapshot is isolated
            logger.exception("recover_all: failed to load snapshot %r, skipping", run_id)
            continue

        if persisted.status in _TERMINAL_RUN_STATUSES:
            continue  # terminal runs never resurrect

        try:
            recovered.append(recover_run(store, workflow, planning, persisted))
        except Exception as exc:  # noqa: BLE001 - single-run failure isolation
            logger.warning(
                "recover_all: run %r recovery failed (%s), continuing with the rest",
                run_id, exc,
            )

    return recovered
