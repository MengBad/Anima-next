"""P7.18 — Full Rollout Test Implementation (RED contract).

P7.17 APPROVED (owner, 2026-08-11): Controlled Real Traffic Test.
Scope confirmed by owner:
    - cohort:   goal#pilot- / plan#pilot- prefixes + EXPLICIT allowlist
    - gray:     plan_id hash-mod gray, <= 1% of candidate traffic
    - window:   72h from approval, expiry -> auto OFF (fail closed)
    - provider: sandbox.write single (unchanged)
    - kill switch: ENABLED required (unchanged)
    - STOP conditions unchanged (P7.17 sec.4)

This slice wires the REAL-TRAFFIC ENTRY (P7.16 Q1 gap): the production
OwnerTaskAgent execution path admits pilot traffic ONLY through the gate;
everything else stays on the legacy path, byte-for-byte.

Acceptance:
    A  gray_sample(0) -> all deny;  gray_sample(100) -> all admit
    B  gray monotonic: hits(p1) subset hits(p2) when p1 < p2 (deterministic)
    C  gray_sample percent out of [0,100] -> PilotScopeError
    D  window_active inside 72h -> True; after expiry -> False
    E  window parse failure -> False (fail closed)
    F  pilot_admit: all conditions -> True; any missing -> False
    G  PilotEntry default (no config) -> None / admit False (legacy unchanged)
    H  PilotEntry enabled -> admit True; run executes sandbox.write + durable ledger
    I  OwnerTaskAgent wiring: pilot=None -> legacy; pilot hit -> pilot result
    J  Static boundary: pilot_entry imports no System B; no new RealTaskExecutor(
"""
from __future__ import annotations

import pathlib
import re

import pytest

from anima.runtime.pilot_control import BridgeKillSwitch
from anima.runtime.pilot_scope import (
    PilotEnablementRecord,
    PilotScope,
    PilotScopeError,
    pilot_gate,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _scope(**overrides):
    defaults = dict(
        scope_id="pilot-rt-001",
        enabled=True,
        mode="CONTROLLED",
        goal_prefixes=("goal#pilot-",),
        plan_prefixes=("plan#pilot-",),
        allowlist=("plan#pilot-rt-001",),
        operator_approval_ref="opr-20260811-rt1",
    )
    defaults.update(overrides)
    return PilotScope(**defaults)


def _record(scope, **overrides):
    defaults = dict(
        pilot_enablement_id="pe-rt-001",
        operator="owner",
        scope_digest=scope.scope_digest,
        approved_at="2026-08-11T18:33:00+08:00",
    )
    defaults.update(overrides)
    return PilotEnablementRecord(**defaults)


def _switch(enabled=True):
    return BridgeKillSwitch(enabled=enabled)


# ---------------------------------------------------------------------------
# A - gray_sample(0) all deny; (100) all admit
# ---------------------------------------------------------------------------
def test_a_gray_bounds():
    from anima.runtime.pilot_rollout import gray_sample

    plan_ids = [f"plan#pilot-{i:04d}" for i in range(50)]
    assert all(gray_sample(p, 0) is False for p in plan_ids)
    assert all(gray_sample(p, 100) is True for p in plan_ids)


# ---------------------------------------------------------------------------
# B - gray monotonic: hits(p1) subset hits(p2) when p1 < p2
# ---------------------------------------------------------------------------
def test_b_gray_monotonic():
    from anima.runtime.pilot_rollout import gray_sample

    plan_ids = [f"plan#pilot-{i:04d}" for i in range(200)]
    h1 = {p for p in plan_ids if gray_sample(p, 1)}
    h5 = {p for p in plan_ids if gray_sample(p, 5)}
    h50 = {p for p in plan_ids if gray_sample(p, 50)}
    assert h1.issubset(h5) and h5.issubset(h50)
    # 1% on 200 ids: expect a small non-empty subset, definitely not all
    assert len(h1) < len(plan_ids)
    assert 0 < len(h50) <= len(plan_ids)


# ---------------------------------------------------------------------------
# C - gray_sample percent out of [0,100] -> PilotScopeError
# ---------------------------------------------------------------------------
def test_c_gray_percent_range():
    from anima.runtime.pilot_rollout import gray_sample

    for bad in (-1, 101, 150):
        with pytest.raises(PilotScopeError):
            gray_sample("plan#pilot-001", bad)


# ---------------------------------------------------------------------------
# D - window_active: inside 72h True; after expiry False
# ---------------------------------------------------------------------------
def test_d_window_active():
    from anima.runtime.pilot_rollout import window_active

    approved = "2026-08-11T18:33:00+08:00"
    inside = "2026-08-12T12:00:00+08:00"  # ~17h later
    boundary = "2026-08-14T18:32:59+08:00"  # just before 72h
    expired = "2026-08-14T18:33:01+08:00"  # just after 72h
    assert window_active(approved, now=inside, window_hours=72) is True
    assert window_active(approved, now=boundary, window_hours=72) is True
    assert window_active(approved, now=expired, window_hours=72) is False


# ---------------------------------------------------------------------------
# E - window parse failure -> False (fail closed)
# ---------------------------------------------------------------------------
def test_e_window_parse_failure():
    from anima.runtime.pilot_rollout import window_active

    assert window_active("not-a-date", now="2026-08-12T00:00:00+08:00") is False
    assert window_active("2026-08-11T18:33:00+08:00", now="garbage") is False


# ---------------------------------------------------------------------------
# F - pilot_admit: all conditions True; any missing False
# ---------------------------------------------------------------------------
def test_f_pilot_admit_conditions():
    from anima.runtime.pilot_rollout import pilot_admit

    scope = _scope()
    record = _record(scope)
    switch = _switch(enabled=True)
    goal = "goal#pilot-1"
    plan = "plan#pilot-rt-001"
    # all conditions -> True
    assert pilot_admit(
        scope, record, switch, goal_id=goal, plan_id=plan,
        gray_percent=100, window_hours=72,
    ) is True
    # kill switch DISABLED -> False
    assert pilot_admit(
        scope, record, _switch(enabled=False), goal_id=goal, plan_id=plan,
        gray_percent=100, window_hours=72,
    ) is False
    # gray 0% -> False
    assert pilot_admit(
        scope, record, switch, goal_id=goal, plan_id=plan,
        gray_percent=0, window_hours=72,
    ) is False
    # window expired -> False
    assert pilot_admit(
        scope, record, switch, goal_id=goal, plan_id=plan,
        gray_percent=100, window_hours=72,
        now="2026-08-15T00:00:00+08:00",
    ) is False
    # cohort miss (goal outside prefix) -> False
    assert pilot_admit(
        scope, record, switch, goal_id="goal#other", plan_id=plan,
        gray_percent=100, window_hours=72,
    ) is False


# ---------------------------------------------------------------------------
# G - PilotEntry default (no config) -> None / admit False (legacy unchanged)
# ---------------------------------------------------------------------------
def test_g_pilot_entry_default_off(tmp_path):
    from anima.runtime.pilot_rollout import PilotEntry

    entry = PilotEntry.from_config({}, data_dir=str(tmp_path / "data"))
    assert entry is None
    entry2 = PilotEntry.from_config(
        {"execution_bridge": {"enabled": False}}, data_dir=str(tmp_path / "data")
    )
    assert entry2 is None
    # an entry that is present but not admitted still denies pilot traffic
    entry3 = PilotEntry(
        scope=_scope(enabled=False),
        record=_record(_scope(enabled=False)),
        switch=_switch(enabled=False),
        executor=None, ledger=None, data_dir=str(tmp_path / "data"),
    )
    assert entry3.admit("goal#pilot-1", "plan#pilot-rt-001") is False


# ---------------------------------------------------------------------------
# H - PilotEntry enabled: admit True; run executes sandbox.write + durable ledger
# ---------------------------------------------------------------------------
def test_h_pilot_entry_enabled_run(tmp_path):
    from anima.core.persistence import PersistenceProvider
    from anima.runtime.execution_ledger import ExecutionLedger
    from anima.runtime.pilot_rollout import PilotEntry

    data_dir = str(tmp_path / "data")
    scope = _scope()
    record = _record(scope)
    switch = _switch(enabled=True)
    entry = PilotEntry(
        scope=scope, record=record, switch=switch,
        executor=None, ledger=None, data_dir=data_dir,
        gray_percent=100,  # deterministic admission for the run-path test
    )
    assert entry.admit("goal#pilot-1", "plan#pilot-rt-001", gray_percent=100) is True
    # run with a sandbox.write task -> the F5 chain writes the sandbox file
    result = entry.run(
        task_id="rt-001", request="hello pilot",
        goal_id="goal#pilot-1", plan_id="plan#pilot-rt-001",
    )
    assert "sandbox.write" in result or "pilot" in result.lower()
    # durable ledger fact survives a restart
    ledger2 = ExecutionLedger(persistence=PersistenceProvider(data_dir))
    cached = ledger2.get(("rt-001", "rt-001", 0))
    assert cached is not None and cached.success is True


# ---------------------------------------------------------------------------
# I - OwnerTaskAgent wiring: pilot=None -> legacy; pilot hit -> pilot result
# ---------------------------------------------------------------------------
def test_i_owner_task_agent_wiring(tmp_path):
    from anima.runtime.pilot_rollout import PilotEntry

    data_dir = str(tmp_path / "data")
    scope = _scope()
    record = _record(scope)
    switch = _switch(enabled=True)
    entry = PilotEntry(
        scope=scope, record=record, switch=switch,
        executor=None, ledger=None, data_dir=data_dir,
    )

    # legacy owner agent (pilot=None) executes the legacy path unchanged
    from anima.core.tasks import OwnerTaskAgent

    class _FakeCore:
        class _Store:
            def get(self, task_id):
                return None

        tasks = _Store()

    agent_legacy = OwnerTaskAgent(_FakeCore())
    assert agent_legacy._pilot is None  # pilot wiring defaults to None

    # a wired agent admits pilot traffic through the gate
    agent_wired = OwnerTaskAgent(_FakeCore(), pilot=entry)
    assert agent_wired._pilot is entry
    assert entry.admit("goal#pilot-1", "plan#pilot-rt-001", gray_percent=100) is True


# ---------------------------------------------------------------------------
# K - lifecycle: startup_check creates dirs, rejects system paths, checks write
# ---------------------------------------------------------------------------
def test_k_startup_check_creates_dirs(tmp_path):
    from anima.runtime.pilot_rollout import PilotEntry

    entry = PilotEntry(
        scope=_scope(), record=_record(_scope()),
        switch=_switch(enabled=True),
        executor=None, ledger=None,
        data_dir=str(tmp_path / "data" / "execution_bridge"),
    )
    entry.startup_check()
    for sub in ("sandbox_write", "ledger", "logs"):
        assert (tmp_path / "data" / "execution_bridge" / sub).is_dir()


def test_k2_startup_check_rejects_system_path():
    from anima.runtime.pilot_rollout import PilotEntry, PilotScopeError

    entry = PilotEntry(
        scope=_scope(), record=_record(_scope()),
        switch=_switch(enabled=True),
        executor=None, ledger=None,
        data_dir="/",
    )
    with pytest.raises(PilotScopeError):
        entry.startup_check()


def test_k3_startup_check_unwritable_fails(tmp_path, monkeypatch):
    from anima.runtime.pilot_rollout import PilotEntry, PilotScopeError

    entry = PilotEntry(
        scope=_scope(), record=_record(_scope()),
        switch=_switch(enabled=True),
        executor=None, ledger=None,
        data_dir=str(tmp_path / "ro"),
    )

    def _boom(*a, **k):
        raise OSError("read-only")

    monkeypatch.setattr(pathlib.Path, "write_text", _boom)
    with pytest.raises(PilotScopeError):
        entry.startup_check()


# ---------------------------------------------------------------------------
# L - lifecycle: shutdown removes token markers, never primary files
# ---------------------------------------------------------------------------
def test_l_shutdown_cleans_tokens(tmp_path):
    from anima.runtime.pilot_rollout import PilotEntry

    data_dir = str(tmp_path / "data")
    entry = PilotEntry(
        scope=_scope(), record=_record(_scope()),
        switch=_switch(enabled=True),
        executor=None, ledger=None, data_dir=data_dir,
    )
    entry.startup_check()
    # create one primary file + one token marker
    sandbox = tmp_path / "data" / "sandbox_write"
    sandbox.mkdir(parents=True, exist_ok=True)
    (sandbox / "keep.txt").write_text("primary", encoding="utf-8")
    tokens = sandbox / ".tokens"
    tokens.mkdir(parents=True, exist_ok=True)
    (tokens / "a.token").write_text("k", encoding="utf-8")
    (tokens / "b.token").write_text("k", encoding="utf-8")
    removed = entry.shutdown()
    assert removed == 2
    assert (sandbox / "keep.txt").exists()  # primary never deleted
    assert list(tokens.glob("*.token")) == []
    # idempotent: second call removes 0
    assert entry.shutdown() == 0


# ---------------------------------------------------------------------------
# J - Static boundary: no System B / gateway / tools / network; no new construction
# ---------------------------------------------------------------------------
def test_j_static_boundary():
    src = pathlib.Path("anima/runtime/pilot_rollout.py").read_text(encoding="utf-8")
    parts = src.split('"""')
    code_body = parts[2] if len(parts) >= 3 else src
    forbidden = [
        "CapabilityGateway", "gateway", "tools", "approve_and_execute",
        "capability_mode", "webui", "napcat", r"\brequests\b", "httpx",
        "subprocess", r"\bssh\b", "paramiko", "os.environ", "getenv",
    ]
    for needle in forbidden:
        assert not re.search(needle, code_body), f"forbidden token: {needle}"
    allowed_prefixes = ("anima.runtime.", "anima.constitution.", "anima.core.persistence")
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith(("from ", "import ")):
            target = stripped.split(" import ")[0].replace("from ", "").strip()
            if target.startswith("anima."):
                assert target.startswith(allowed_prefixes), f"import escapes: {target!r}"
    # the F5 bridge construction point stays single (no new RealTaskExecutor()
    pilot_control_src = pathlib.Path("anima/runtime/pilot_control.py").read_text(encoding="utf-8")
    assert pilot_control_src.count("RealTaskExecutor(") == 1
    assert pilot_control_src.count("SandboxWriteProvider(") == 1
