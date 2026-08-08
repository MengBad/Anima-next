"""F7.3b — RecoveryMarkerStore unit tests (marker lifecycle + atomic persistence).

The marker is a SIDE RECORD: it must never be run-state and must survive a fresh
provider reload (atomic write_json). No on-disk migration is required — a missing
``recovery_markers/`` directory is simply "no marker yet" (current-behavior compatible).
"""
from __future__ import annotations

import tempfile

from anima.core.persistence import PersistenceProvider
from anima.runtime.recovery_marker_store import (
    MARKER_SCHEMA_VERSION,
    MarkerPhase,
    RecoveryMarker,
    RecoveryMarkerStore,
)


def _store():
    return RecoveryMarkerStore(PersistenceProvider(tempfile.mkdtemp()))


def test_write_recovering_then_mark_recovered_transitions_phase():
    ms = _store()
    rid = "run#crash-1"
    ms.write_recovering(rid, "", rid)
    assert ms.get(rid).phase is MarkerPhase.RECOVERING
    ms.mark_recovered(rid, "run#new-1", rid)
    m = ms.get(rid)
    assert m.phase is MarkerPhase.RECOVERED
    assert m.new_run_id == "run#new-1"
    assert m.original_run_id == rid
    assert m.source_run_id == rid


def test_mark_failed_captures_error_and_preserves_ids():
    ms = _store()
    rid = "run#crash-2"
    ms.write_recovering(rid, "run#new-2", rid)
    ms.mark_failed(rid, RuntimeError("boom"), new_run_id="run#new-2", source_run_id=rid)
    m = ms.get(rid)
    assert m.phase is MarkerPhase.FAILED
    assert "boom" in (m.error or "")
    assert m.new_run_id == "run#new-2"
    assert m.source_run_id == rid


def test_mark_failed_without_prior_marker_keeps_empty_ids():
    ms = _store()
    rid = "run#crash-3"
    ms.mark_failed(rid, RuntimeError("no prior"), new_run_id="", source_run_id="")
    m = ms.get(rid)
    assert m.phase is MarkerPhase.FAILED
    assert m.new_run_id == ""
    assert m.source_run_id == ""


def test_get_missing_returns_none_no_migration():
    ms = _store()
    # No marker yet -> None. Upgrading an existing deployment (no recovery_markers dir)
    # must behave exactly like today (re-enumerate RUNNING + ledger dedup兜底).
    assert ms.get("run#never") is None
    assert not ms.exists("run#never")
    assert not ms.is_recovered("run#never")


def test_missing_markers_dir_auto_created_on_write():
    ms = _store()
    rid = "run#crash-4"
    ms.write_recovering(rid, "", rid)  # write_json mkdirs the parent recovery_markers/
    assert ms.get(rid).phase is MarkerPhase.RECOVERING


def test_is_recovered_only_true_for_recovered_phase():
    ms = _store()
    rid = "run#crash-5"
    ms.write_recovering(rid, "", rid)
    assert not ms.is_recovered(rid)
    ms.mark_recovered(rid, "run#new-5", rid)
    assert ms.is_recovered(rid)
    ms.mark_failed(rid, RuntimeError("x"))
    assert not ms.is_recovered(rid)


def test_persistence_across_provider_reload():
    with tempfile.TemporaryDirectory() as td:
        pp1 = PersistenceProvider(td)
        ms1 = RecoveryMarkerStore(pp1)
        rid = "run#crash-6"
        ms1.mark_recovered(rid, "run#new-6", rid)
        # A brand-new provider on the SAME data dir must see the marker (no loss).
        pp2 = PersistenceProvider(td)
        ms2 = RecoveryMarkerStore(pp2)
        m = ms2.get(rid)
        assert m is not None
        assert m.phase is MarkerPhase.RECOVERED
        assert m.new_run_id == "run#new-6"


def test_delete_removes_marker():
    ms = _store()
    rid = "run#crash-7"
    ms.write_recovering(rid, "", rid)
    assert ms.exists(rid)
    ms.delete(rid)
    assert not ms.exists(rid)
    ms.delete(rid)  # idempotent: no error on missing


def test_from_dict_tolerates_legacy_phase_string_and_missing_fields():
    # Backward-compat: an old/unknown phase string or missing schema_version must not raise.
    m = RecoveryMarker.from_dict({
        "original_run_id": "run#old",
        "new_run_id": "run#new",
        "source_run_id": "run#old",
        "phase": "RECOVERED",
    })
    assert m.phase is MarkerPhase.RECOVERED
    assert m.schema_version == MARKER_SCHEMA_VERSION
    assert m.completed_at is None
    # unknown phase string -> defaults to RECOVERING (safe retry, never a silent skip)
    m2 = RecoveryMarker.from_dict({
        "original_run_id": "run#old2",
        "new_run_id": "",
        "source_run_id": "run#old2",
        "phase": "WEIRD",
    })
    assert m2.phase is MarkerPhase.RECOVERING
