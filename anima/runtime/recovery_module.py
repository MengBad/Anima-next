"""RecoveryModule (F4.7b) — application-level recovery wiring for AnimaCore.

Bridges the runtime recovery bootstrap (``recover_all``) into the real application
startup WITHOUT touching ``anima/core/*``: it is a core ``Module`` whose ``restore()``
delegates to the runtime recovery chain, so the existing startup lifecycle

    core.start()
        -> for m in self.modules: await m.restore()

runs recovery automatically after the core is ready — reusing the host's own hook.

Wiring (cli.py, F4.7b):
    core.register(RecoveryModule(store=..., workflow=..., planning=...))

Design rules (frozen in F4.5 / F4.7 / F4.7b):

- **Delegation, not re-implementation.** ``restore()`` is a thin async wrapper around
  ``recover_all`` — the single recovery implementation. Nothing here copies recovery
  logic (no Plan resolution, no report replay, no filtering).
- **Layering.** This module lives in ``anima/runtime`` and imports only the ``Module``
  ABC from ``anima.core.module`` for the contract (type only); it never imports
  ``anima.core`` behavior, so no core -> runtime dependency is introduced (core stays
  the host; runtime stays above constitution).
- **Authority boundary.** The module writes no run state: no ``_registry``, no
  ``_transition``, no ``run.status =``, no ``task_states =``. Every state change happens
  inside the Workflow Authority via ``recover_run``'s public-API replay (AST guard).
- **Failure isolation is inherited.** ``recover_all`` already isolates single-run
  failures; ``restore()`` propagates nothing — a partial recovery is logged there and
  the rest proceeds.
- **Terminal runs never resurrect** (F4.5 freeze) — inherited from ``recover_all``.
"""
from __future__ import annotations

from anima.core.module import Module
from anima.runtime.bootstrap import recover_all


class RecoveryModule(Module):
    """Core Module that runs runtime recovery on startup (F4.7b).

    ``restore()`` is called by AnimaCore's startup loop; it delegates to
    ``recover_all`` so every non-terminal persisted run is restored after the core
    is ready. ``init`` / ``persist`` / ``shutdown`` are intentionally no-op: recovery
    is a startup act, not a running service.
    """

    name: str = "runtime_recovery"

    def __init__(self, store, workflow, planning) -> None:
        self._store = store          # WorkflowRunStore (runtime persistence)
        self._workflow = workflow    # WorkflowInterface (public API only)
        self._planning = planning    # PlanningInterface (public API only)
        self.last_recovered: list = []   # audit: runs recovered by the last restore()

    async def restore(self) -> None:
        """Recover every non-terminal persisted run (async wrapper over recover_all)."""
        self.last_recovered = recover_all(self._store, self._workflow, self._planning)

    # ---- lifecycle no-ops: recovery is a startup act, not a running service ----
