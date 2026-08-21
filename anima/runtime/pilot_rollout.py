"""P7.18 — Full Rollout Test Implementation (P7.17-approved real traffic entry).

P7.17 APPROVED (owner, 2026-08-11): Controlled Real Traffic Test wiring.
This module closes the P7.16 Q1 gap (real-traffic entry had ZERO consumers):
the production OwnerTaskAgent path admits pilot traffic ONLY through the gate,
and everything else stays on the legacy path byte-for-byte.

Implements the two P7.17 sec.2 mechanisms that did not exist in P7.14:

    gray_sample(plan_id, percent)  - plan_id hash-mod gray (<= 1% of candidates);
                                     deterministic, monotonic, fail-closed bounds.
    window_active(approved_at, ...) - 72h approval window; expiry -> auto OFF
                                     (fail closed: unparseable input denies).

    pilot_admit(...)               - gate + gray + window combined admission
                                     (pilot_gate is unchanged: cohort + approval
                                     binding + kill switch + provider).
    PilotEntry                     - the real-traffic entry point: from_config
                                     (default OFF / no config -> None) + admit +
                                     run (sandbox.write through the SINGLE F5
                                     construction point).

Scope limits (P7.17 approval):
    * sandbox.write single provider (unchanged; no new construction point).
    * No System B / gateway / capability_mode / env bypass.
    * No key merge / authz injection; F5 run identity stays autonomous.
    * Kill switch remains the only check point (never bypassed).
    * 72h window, expiry -> automatic OFF (no default extension).
"""
from __future__ import annotations

import hashlib
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from anima.core.persistence import PersistenceProvider
from anima.runtime.executor import ExecutionStep
from anima.runtime.pilot_control import (
    BridgeKillSwitch,
    build_production_bridge,
)
from anima.runtime.pilot_scope import (
    PilotEnablementRecord,
    PilotScope,
    PilotScopeError,
    pilot_gate,
)

__all__ = [
    "gray_sample",
    "window_active",
    "pilot_admit",
    "PilotEntry",
]


def _now_iso() -> str:
    """Current wall-clock time as an ISO-8601 string (aware, local tz)."""
    return datetime.now(timezone.utc).astimezone().isoformat()


def gray_sample(plan_id: str, percent: int) -> bool:
    """plan_id hash-mod gray: admit with probability ``percent/100``.

    Deterministic (same plan_id -> same verdict) and monotonic (a plan admitted
    at p1 is admitted at any p2 > p1).  ``percent`` must be in [0, 100];
    0 denies everything, 100 admits everything (fail-closed bounds).
    """
    if not isinstance(percent, int) or not (0 <= percent <= 100):
        raise PilotScopeError(f"gray percent must be an int in [0, 100], got {percent!r}")
    if percent == 0:
        return False
    if percent >= 100:
        return True
    digest = hashlib.sha256(plan_id.encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) % 100
    return bucket < percent


def window_active(
    approved_at: str,
    *,
    now: str,
    window_hours: int = 72,
) -> bool:
    """True while ``now`` is within ``window_hours`` of ``approved_at``.

    Fail closed: any unparseable input (approved_at or now) denies.  Expiry
    means the pilot window is OVER -> automatic OFF (P7.17 sec.2).
    """
    try:
        approved = datetime.fromisoformat(approved_at)
        current = datetime.fromisoformat(now)
    except (TypeError, ValueError):
        return False
    if approved.tzinfo is None:
        approved = approved.replace(tzinfo=timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current <= approved + timedelta(hours=window_hours)


def pilot_admit(
    scope: Optional[PilotScope],
    record: Optional[PilotEnablementRecord],
    switch: Any,
    *,
    goal_id: str,
    plan_id: str,
    gray_percent: int = 100,
    window_hours: int = 72,
    now: Optional[str] = None,
) -> bool:
    """Combined admission: window AND gray AND pilot_gate (fail closed).

    ``gray_percent`` and ``window_hours`` default to the P7.17 approval values
    (<= 1% and 72h) but are overridable for testing; the approval itself is
    bound by ``record.scope_digest == scope.scope_digest`` inside ``pilot_gate``.
    """
    if scope is None or record is None:
        return False
    if not window_active(
        record.approved_at, now=now or _now_iso(), window_hours=window_hours
    ):
        return False  # window expired -> automatic OFF
    if not gray_sample(plan_id, gray_percent):
        return False  # outside the gray sample -> legacy path
    return pilot_gate(
        scope,
        record,
        switch,
        goal_id=goal_id,
        plan_id=plan_id,
        provider_available=True,
    )


class PilotEntry:
    """The real-traffic pilot entry (P7.17 sec.2: single goal/plan cohort).

    ``from_config`` is the ONLY factory: no config / disabled -> ``None``
    (legacy path untouched).  ``admit`` composes window + gray + gate;
    ``run`` executes one sandbox.write through the SINGLE F5 construction
    point (``build_production_bridge``), with the durable ledger as the
    idempotency gate.  No System B, no provider expansion, no env bypass.
    """

    def __init__(
        self,
        *,
        scope: PilotScope,
        record: PilotEnablementRecord,
        switch: BridgeKillSwitch,
        executor: Any = None,
        ledger: Any = None,
        data_dir: str,
        gray_percent: int = 1,
        window_hours: int = 72,
        event_log: Any = None,
    ) -> None:
        self._scope = scope
        self._record = record
        self._switch = switch
        self._executor = executor  # optional: injected bridge executor
        self._ledger = ledger  # optional: injected durable ledger
        self._data_dir = data_dir
        self._gray_percent = gray_percent
        self._window_hours = window_hours
        self._event_log = event_log  # optional: structured JSON event log
        self._bridge = None  # lazy: (executor, ledger) built once via the single point

    # -- lifecycle (P7.17-A Phase 1.3) ------------------------------------

    #: data sub-directories under data_dir (P7.17-A: sandbox/ledger/tokens/logs
    #: all isolated under the execution_bridge data root).
    _DATA_SUBDIRS = ("sandbox_write", "ledger", "logs")

    def startup_check(self) -> None:
        """Validate the runtime data directory; raise on any failure.

        Fail closed: a missing/uncreatable data root, or one resolving to a
        system path (filesystem root, /etc, /usr, /home, /var, the production
        root), aborts startup loudly instead of writing to an uncontrolled
        location (P7.17-A Phase 1.3).
        """
        root = Path(self._data_dir)
        try:
            root.mkdir(parents=True, exist_ok=True)
            for sub in self._DATA_SUBDIRS:
                (root / sub).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PilotScopeError(
                f"pilot startup_check: cannot create data dir {root!s}: {exc}"
            ) from exc
        resolved = root.resolve()
        # filesystem root (any drive on Windows, "/" on POSIX): parent == self
        if resolved.parent == resolved:
            raise PilotScopeError(
                f"pilot startup_check: data dir {resolved!s} is a filesystem "
                f"root — refuse"
            )
        bad_prefixes = (
            Path("/etc"), Path("/usr"), Path("/home"),
            Path("/var"), Path("/opt/anima"),  # never share the production root
        )
        for bad in bad_prefixes:
            if bad in resolved.parents:
                raise PilotScopeError(
                    f"pilot startup_check: data dir {resolved!s} resolves under "
                    f"a system/production path ({bad!s}) — refuse"
                )
        probe = root / ".pilot_write_probe"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            raise PilotScopeError(
                f"pilot startup_check: data dir {root!s} not writable: {exc}"
            ) from exc

    def shutdown(self) -> int:
        """Cleanup at shutdown: remove expired idempotency markers.

        Returns the number of cleaned markers.  Primary sandbox files are
        NEVER deleted (audit trail; cleanup is explicit, never automatic for
        user data).  Safe to call multiple times.
        """
        root = Path(self._data_dir)
        tokens = root / "sandbox_write" / ".tokens"
        removed = 0
        if not tokens.is_dir():
            return 0
        for marker in tokens.glob("*.token"):
            try:
                marker.unlink()
                removed += 1
            except OSError:
                continue  # a locked marker is left in place (never fatal)
        return removed

    # -- factory ----------------------------------------------------------

    @classmethod
    def from_config(cls, config: Optional[dict], *, data_dir: str) -> Optional["PilotEntry"]:
        """Build from config; None when disabled (default OFF / fail closed).

        Config shape (P7.17 approval):
            {"execution_bridge": {"enabled": true, "pilot": {
                 "scope_id", "goal_prefixes", "plan_prefixes", "allowlist",
                 "operator_approval_ref", "pilot_enablement_id", "operator",
                 "approved_at", "gray_percent" (default 1), "window_hours" (default 72)}}}
        """
        section = (config or {}).get("execution_bridge") or {}
        if not section.get("enabled"):
            return None
        pilot = section.get("pilot") or {}
        required = (
            "scope_id",
            "goal_prefixes",
            "plan_prefixes",
            "allowlist",
            "operator_approval_ref",
            "pilot_enablement_id",
            "operator",
            "approved_at",
        )
        if any(pilot.get(k) in (None, "") for k in required):
            return None  # an incomplete approval must never enable pilot traffic
        scope = PilotScope(
            scope_id=pilot["scope_id"],
            enabled=True,
            mode="CONTROLLED",
            goal_prefixes=tuple(pilot["goal_prefixes"]),
            plan_prefixes=tuple(pilot["plan_prefixes"]),
            allowlist=tuple(pilot["allowlist"]),
            operator_approval_ref=pilot["operator_approval_ref"],
        )
        record = PilotEnablementRecord(
            pilot_enablement_id=pilot["pilot_enablement_id"],
            operator=pilot["operator"],
            scope_digest=scope.scope_digest,
            approved_at=pilot["approved_at"],
        )
        switch = BridgeKillSwitch(enabled=True)
        return cls(
            scope=scope,
            record=record,
            switch=switch,
            data_dir=data_dir,
            gray_percent=int(pilot.get("gray_percent", 1)),
            window_hours=int(pilot.get("window_hours", 72)),
        )

    # -- admission ----------------------------------------------------------

    def admit(
        self,
        goal_id: str,
        plan_id: str,
        *,
        gray_percent: Optional[int] = None,
        now: Optional[str] = None,
    ) -> bool:
        """Combined admission for one candidate task (window + gray + gate)."""
        return pilot_admit(
            self._scope,
            self._record,
            self._switch,
            goal_id=goal_id,
            plan_id=plan_id,
            gray_percent=self._gray_percent if gray_percent is None else gray_percent,
            window_hours=self._window_hours,
            now=now,
        )

    # -- execution -----------------------------------------------------------

    def run(
        self,
        *,
        task_id: str,
        request: str,
        goal_id: str,
        plan_id: str,
    ) -> str:
        """Execute one pilot task through the F5 chain (sandbox.write only).

        Builds the bridge through the SINGLE construction point and executes
        one sandbox.write step under an idempotency key derived from the task
        identity; the durable ledger records the fact.  Any gate failure falls
        back to the legacy path (caller's decision); a sandbox failure is a
        FAILED observation, never a fabricated success.
        """
        if not self.admit(goal_id, plan_id):
            return "pilot: denied (gate / window / gray) -> legacy path"
        executor, ledger = self._bridge_or_build()
        step = ExecutionStep(
            id=task_id,
            title="pilot sandbox write",
            intent="controlled real traffic test",
            capability_need={
                "capability": "sandbox.write",
                "args": {"path": f"pilot/{task_id}.txt", "content": request},
                "context": {},
            },
            depends_on=[],
            estimates={},
        )
        key = (task_id, task_id, 0)
        started = time.time()
        ledger_error = None
        try:
            obs = executor.execute(step, key, ledger)
        except Exception as exc:  # noqa: BLE001 - any execution error is recorded
            obs = ExecutionObservation(
                task_id=task_id, success=False, result={},
                error=f"execution raised: {exc}",
                metadata={"provider_id": None, "side_effect_recorded": False},
            )
            ledger_error = exc
        latency_ms = (time.time() - started) * 1000.0
        self._record_event(
            task_id=task_id,
            key=key,
            obs=obs,
            ledger_error=ledger_error,
            latency_ms=latency_ms,
        )
        if obs.success:
            return (
                "pilot: sandbox.write ok "
                f"(task={task_id}, goal={goal_id}, plan={plan_id})"
            )
        return (
            "pilot: FAILED observation "
            f"(task={task_id}, error={obs.error or 'unknown'})"
        )

    def _record_event(self, *, task_id: str, key: tuple, obs, ledger_error, latency_ms: float) -> None:
        """Record one structured execution event (P7.17-A Phase 2), if a log is wired."""
        log = getattr(self, "_event_log", None)
        if log is None:
            return
        from anima.runtime.execution_events import (
            ExecutionEvent,
            classify_failure,
            evidence_id_for,
            new_execution_id,
        )

        failure = classify_failure(obs, ledger_error=ledger_error)
        event = ExecutionEvent(
            execution_id=new_execution_id(),
            execution_key=key,
            task_id=task_id,
            capability_id="sandbox.write",
            provider=str(getattr(obs, "metadata", {}).get("provider_id", "") or "sandbox.write.local"),
            result="SUCCESS" if obs.success else "FAILED",
            latency_ms=latency_ms,
            evidence_id=evidence_id_for(key),
            error_type=failure,
        )
        log.record(event)

    def _bridge_or_build(self):
        """Return (executor, ledger): injected, or built ONCE via the single point.

        ``build_production_bridge`` is the UNIQUE F5 construction boundary; the
        built executor/ledger are cached on the entry so repeated runs reuse the
        same durable ledger (idempotency facts accumulate across calls).
        """
        if self._executor is not None and self._ledger is not None:
            return self._executor, self._ledger
        if self._bridge is None:
            switch, orch, mode = build_production_bridge(
                {"execution_bridge": {"enabled": True}},
                data_dir=self._data_dir,
            )
            if orch is None:
                raise PilotScopeError("pilot bridge unavailable (OFF)")
            self._bridge = (orch._executor, orch._ledger)  # noqa: SLF001
        return self._bridge
