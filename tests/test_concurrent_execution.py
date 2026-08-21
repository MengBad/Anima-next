"""P7.17-A Phase 4 — Concurrent Execution Validation (RED).

Scenarios (P7.17-A Phase 4):
    1. SAME execution_key, 100 concurrent requests
       -> the sandbox file is written EXACTLY ONCE (one real side effect);
          the rest are HITs (ledger or external marker), no double write
    2. DIFFERENT execution_keys, concurrent
       -> every write lands in its own file; ledger shows no conflict
    3. marker race: write success + marker failure + restart recovery
       -> the committed fact is recovered, never re-written (P7.8 G4)
Acceptance:
    P  same-key 100 concurrent: exactly one written=True side effect
    P2 same-key: ledger has exactly one fact; all results success
    P3 different keys concurrent: all 100 succeed, files exist
    P4 ledger durable survives concurrent writes (restart read-back)
    P5 marker race: committed file + lost marker -> marker restored,
       written=False on re-execution (no duplicate side effect)
"""
from __future__ import annotations

import concurrent.futures
import pathlib

from anima.core.persistence import PersistenceProvider
from anima.runtime.execution_ledger import ExecutionLedger
from anima.runtime.executor import ExecutionObservation, ExecutionStep
from anima.runtime.providers.sandbox_write_provider import SandboxWriteProvider


def _provider(data_dir: str, counter: list) -> SandboxWriteProvider:
    p = SandboxWriteProvider(sandbox_root=None, data_dir=data_dir)
    original = p.invoke

    def counting(cap, args, key):
        result = original(cap, args, key)
        if result.success and result.data.get("written"):
            counter.append(key)
        return result

    p.invoke = counting
    return p


def _executor(data_dir: str, counter: list):
    from anima.runtime.capability_resolver import (
        CapabilityContract,
        CapabilityRegistry,
        CapabilityResolver,
    )
    from anima.runtime.real_task_executor import RealTaskExecutor

    reg = CapabilityRegistry()
    reg.register(
        CapabilityContract("sandbox.write", side_effect=True, idempotent=True),
        _provider(data_dir, counter),
    )
    return RealTaskExecutor(resolver=CapabilityResolver(reg))


def _step(task_id="t-c", path="out.txt", content="x"):
    return ExecutionStep(
        id=task_id, title="sandbox write", intent="concurrent test",
        capability_need={"capability": "sandbox.write",
                         "args": {"path": path, "content": content}, "context": {}},
        depends_on=[], estimates={},
    )


# ---------------------------------------------------------------------------
# P - same-key 100 concurrent: exactly one written=True side effect
# ---------------------------------------------------------------------------
def test_p_same_key_concurrent_single_write(tmp_path):
    data_dir = str(tmp_path / "data")
    counter: list = []
    exec_ = _executor(data_dir, counter)
    ledger = ExecutionLedger(persistence=PersistenceProvider(data_dir))
    key = ("run-c", "t-c", 0)

    def once(_):
        return exec_.execute(_step(), key, ledger)

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(once, range(100)))

    assert all(r.success for r in results)
    assert len(counter) == 1  # exactly one real side effect
    out = pathlib.Path(data_dir) / "sandbox_write" / "out.txt"
    assert out.read_text(encoding="utf-8") == "x"


# ---------------------------------------------------------------------------
# P2 - same-key: ledger has exactly one fact; all results success
# ---------------------------------------------------------------------------
def test_p2_same_key_ledger_single_fact(tmp_path):
    data_dir = str(tmp_path / "data")
    counter: list = []
    exec_ = _executor(data_dir, counter)
    ledger = ExecutionLedger(persistence=PersistenceProvider(data_dir))
    key = ("run-c2", "t-c2", 0)

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(lambda _: exec_.execute(_step("t-c2"), key, ledger), range(100)))

    assert ledger.get(key) is not None
    assert len(counter) == 1


# ---------------------------------------------------------------------------
# P3 - different keys concurrent: all succeed, each file exists
# ---------------------------------------------------------------------------
def test_p3_different_keys_concurrent(tmp_path):
    data_dir = str(tmp_path / "data")
    counter: list = []
    exec_ = _executor(data_dir, counter)
    ledger = ExecutionLedger(persistence=PersistenceProvider(data_dir))

    def run_one(i):
        key = (f"run-{i}", f"t-{i}", 0)
        obs = exec_.execute(_step(f"t-{i}", f"f{i}.txt", f"content-{i}"), key, ledger)
        return i, obs.success

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        results = dict(pool.map(run_one, range(100)))

    assert all(results.values())  # all 100 succeeded
    assert len(counter) == 100  # each distinct key wrote its own file
    for i in range(100):
        f = pathlib.Path(data_dir) / "sandbox_write" / f"f{i}.txt"
        assert f.read_text(encoding="utf-8") == f"content-{i}"


# ---------------------------------------------------------------------------
# P4 - ledger durable survives concurrent writes (restart read-back)
# ---------------------------------------------------------------------------
def test_p4_durable_after_concurrent(tmp_path):
    data_dir = str(tmp_path / "data")
    counter: list = []
    exec_ = _executor(data_dir, counter)
    ledger = ExecutionLedger(persistence=PersistenceProvider(data_dir))

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(lambda i: exec_.execute(
            _step(f"t-{i}", f"f{i}.txt", "x"), (f"run-{i}", f"t-{i}", 0), ledger
        ), range(50)))

    ledger2 = ExecutionLedger(persistence=PersistenceProvider(data_dir))  # restart
    for i in range(50):
        assert ledger2.get((f"run-{i}", f"t-{i}", 0)) is not None


# ---------------------------------------------------------------------------
# P5 - marker race: committed file + lost marker -> marker restored, no re-write
# ---------------------------------------------------------------------------
def test_p5_marker_race_recovery(tmp_path):
    data_dir = str(tmp_path / "data")
    provider = SandboxWriteProvider(sandbox_root=None, data_dir=data_dir)
    key = ("run-m", "t-m", 0)
    # first invoke writes file + marker
    r1 = provider.invoke("sandbox.write", {"path": "m.txt", "content": "m"}, key)
    assert r1.success and r1.data["written"] is True
    # simulate marker loss (crash window: file committed, marker lost)
    for marker in (pathlib.Path(data_dir) / "sandbox_write" / ".tokens").glob("*.token"):
        marker.unlink()
    # re-invoke: committed content matches -> marker restored, written=False
    r2 = provider.invoke("sandbox.write", {"path": "m.txt", "content": "m"}, key)
    assert r2.success and r2.data["written"] is False
    assert "restored" in r2.data.get("reason", "")
    # marker is back; a third invoke is an external idempotency HIT
    r3 = provider.invoke("sandbox.write", {"path": "m.txt", "content": "m"}, key)
    assert r3.success and r3.data["written"] is False
