"""P7.10 — L1 Durable Ledger + L2 Retention (RED contract).

L1 (durable backend): ExecutionLedger(persistence=PersistenceProvider) must
survive restart, replay-hit on the same key, and fail closed on a corrupt
record; put must never OVERWRITE an existing key (key check, no overwrite
without check).

L2 (retention): ledger.cleanup(before) removes only COMPLETED (success=True)
records; failed / recoverable / unknown states are preserved.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from anima.core.persistence import PersistenceProvider
from anima.runtime.execution_ledger import ExecutionLedger, _key_path
from anima.runtime.executor import ExecutionObservation


def _obs(task_id="t1", success=True, **kw):
    d = dict(task_id=task_id, success=success, result={"v": 1}, error="", metadata={})
    d.update(kw)
    return ExecutionObservation(**d)


# ---------------------------------------------------------------------------
# L1-A memory backend compatibility (F5.1 behavior unchanged)
# ---------------------------------------------------------------------------
def test_l1a_memory_compat():
    ledger = ExecutionLedger()  # memory-only
    key = ("run-1", "t1", 0)
    assert ledger.get(key) is None
    ledger.put(key, _obs())
    assert ledger.has(key) is True
    assert ledger.get(key).success is True


# ---------------------------------------------------------------------------
# L1-B durable backend: reload after restart returns the fact
# ---------------------------------------------------------------------------
def test_l1b_durable_restart(tmp_path):
    data = str(tmp_path / "ledger")
    ledger1 = ExecutionLedger(persistence=PersistenceProvider(data))
    key = ("run-1", "t1", 0)
    ledger1.put(key, _obs())

    # simulate restart: a fresh ledger over the same data dir
    ledger2 = ExecutionLedger(persistence=PersistenceProvider(data))
    cached = ledger2.get(key)
    assert cached is not None
    assert cached.success is True
    assert cached.task_id == "t1"


# ---------------------------------------------------------------------------
# L1-C same execution_key replay hit (no second side effect)
# ---------------------------------------------------------------------------
def test_l1c_replay_hit(tmp_path):
    ledger = ExecutionLedger(persistence=PersistenceProvider(str(tmp_path / "ledger")))
    key = ("run-1", "t1", 0)
    ledger.put(key, _obs(success=True))
    # a second put of the SAME key is a no-op (key check, no overwrite)
    ledger.put(key, _obs(success=False, result={"evil": True}))
    cached = ledger.get(key)
    assert cached.success is True  # original fact preserved
    assert cached.result == {"v": 1}


# ---------------------------------------------------------------------------
# L1-D corrupt record fails closed (get returns None, no cache pollution)
# ---------------------------------------------------------------------------
def test_l1d_corrupt_fails_closed(tmp_path):
    data = str(tmp_path / "ledger")
    ledger = ExecutionLedger(persistence=PersistenceProvider(data))
    key = ("run-1", "t1", 0)
    ledger.put(key, _obs())
    # corrupt the durable record
    path = pathlib.Path(data) / _key_path(key)
    path.write_text("{not-json", encoding="utf-8")

    fresh = ExecutionLedger(persistence=PersistenceProvider(data))
    assert fresh.get(key) is None  # fail closed, no raise, no polluted cache


# ---------------------------------------------------------------------------
# L2-E expired completed records removed
# ---------------------------------------------------------------------------
def test_l2e_completed_removed(tmp_path):
    data = str(tmp_path / "ledger")
    ledger = ExecutionLedger(persistence=PersistenceProvider(data))
    ledger.put(("run-1", "t1", 0), _obs(success=True))
    ledger.put(("run-2", "t1", 0), _obs(success=True, task_id="t1"))

    removed = ledger.cleanup(before=None)  # remove all completed
    assert removed == 2
    assert ledger.has(("run-1", "t1", 0)) is False
    assert ledger.has(("run-2", "t1", 0)) is False


# ---------------------------------------------------------------------------
# L2-F active (failed) records preserved
# ---------------------------------------------------------------------------
def test_l2f_failed_preserved(tmp_path):
    data = str(tmp_path / "ledger")
    ledger = ExecutionLedger(persistence=PersistenceProvider(data))
    ledger.put(("run-1", "t1", 0), _obs(success=True))
    ledger.put(("run-2", "t1", 0), _obs(success=False, task_id="t2"))

    removed = ledger.cleanup(before=None)
    assert removed == 1  # only the completed one
    assert ledger.has(("run-1", "t1", 0)) is False
    assert ledger.has(("run-2", "t1", 0)) is True  # failed preserved


# ---------------------------------------------------------------------------
# L2-G recovery (skipped/unknown) records preserved
# ---------------------------------------------------------------------------
def test_l2g_recovery_preserved(tmp_path):
    data = str(tmp_path / "ledger")
    ledger = ExecutionLedger(persistence=PersistenceProvider(data))
    ledger.put(("run-1", "t1", 0), _obs(success=True))
    # recoverable/unknown states: skipped metadata, empty (unknown) fact
    ledger.put(("run-2", "t1", 0), _obs(success=False, metadata={"skipped": True}))
    ledger.put(("run-3", "t1", 0), _obs(success=False, result={}))

    removed = ledger.cleanup(before=None)
    assert removed == 1
    assert ledger.has(("run-2", "t1", 0)) is True
    assert ledger.has(("run-3", "t1", 0)) is True
