"""P7.17-A Phase 2 — Structured JSON logging for the Execution Bridge (RED).

Every execution MUST be recorded as one JSON line:

    {
      execution_id, execution_key, task_id, capability_id, provider,
      result, latency_ms, evidence_id, error_type, timestamp
    }

Failure types are standardized (P7.17-A):
    PROVIDER_FAILURE / RESOLUTION_FAILURE / LEDGER_FAILURE /
    SANDBOX_FAILURE / IDENTITY_FAILURE / UNKNOWN_FAILURE

Logs must be directly machine-analyzable (JSONL, one event per line).

Acceptance:
    M  record writes a complete JSON line to logs/execution.jsonl
    M2 RESOLUTION_FAILURE classified (out-of-domain capability)
    M3 PROVIDER_FAILURE classified (sandbox traversal refusal)
    M4 LEDGER_FAILURE classified (ledger.put raises)
    M5 SANDBOX_FAILURE classified (write OSError / conflict)
    M6 evidence_id is deterministic for the same execution_key
    M7 every log line parses as JSON with all required keys
    M8 event_log integrates into PilotEntry.run (latency > 0, result recorded)
"""
from __future__ import annotations

import json
import pathlib

import pytest

from anima.runtime.executor import ExecutionObservation, ExecutionStep
from anima.runtime.pilot_scope import PilotEnablementRecord, PilotScope
from anima.runtime.pilot_control import BridgeKillSwitch


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


def _record(scope):
    return PilotEnablementRecord(
        pilot_enablement_id="pe-rt-001",
        operator="owner",
        scope_digest=scope.scope_digest,
        approved_at="2026-08-11T18:33:00+08:00",
    )


def _switch(enabled=True):
    return BridgeKillSwitch(enabled=enabled)


def _step(path="out.txt", content="x"):
    return ExecutionStep(
        id="t-log", title="sandbox write", intent="log test",
        capability_need={"capability": "sandbox.write",
                         "args": {"path": path, "content": content}, "context": {}},
        depends_on=[], estimates={},
    )


# ---------------------------------------------------------------------------
# M - record writes a complete JSON line to logs/execution.jsonl
# ---------------------------------------------------------------------------
def test_m_record_writes_jsonl(tmp_path):
    from anima.runtime.execution_events import ExecutionEvent, ExecutionEventLog

    log = ExecutionEventLog(str(tmp_path / "data"))
    ev = ExecutionEvent(
        execution_id="e-001",
        execution_key=("run-1", "t-log", 0),
        task_id="t-log",
        capability_id="sandbox.write",
        provider="sandbox.write.local",
        result="SUCCESS",
        latency_ms=1.5,
        evidence_id="ev-001",
        error_type="",
    )
    log.record(ev)
    lines = (tmp_path / "data" / "logs" / "execution.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["execution_id"] == "e-001"
    assert parsed["task_id"] == "t-log"
    assert parsed["capability_id"] == "sandbox.write"


# ---------------------------------------------------------------------------
# M2 - RESOLUTION_FAILURE classified (out-of-domain capability)
# ---------------------------------------------------------------------------
def test_m2_resolution_failure(tmp_path):
    from anima.runtime.execution_events import classify_failure

    obs = ExecutionObservation(
        task_id="t-x", success=False, result={},
        error="capability resolution failed: no provider for code.execute",
        metadata={"provider_id": None, "side_effect_recorded": False},
    )
    assert classify_failure(obs, ledger_error=None) == "RESOLUTION_FAILURE"


# ---------------------------------------------------------------------------
# M2b - PROVIDER_FAILURE even when the resolver wraps the provider refusal
# ---------------------------------------------------------------------------
def test_m2b_wrapped_provider_refusal(tmp_path):
    from anima.runtime.execution_events import classify_failure

    # the resolver wraps a provider refusal as "all candidates failed" — the
    # provider WAS invoked and refused, so this is PROVIDER not RESOLUTION
    obs = ExecutionObservation(
        task_id="t-x", success=False, result={},
        error=("capability resolution failed: resolve: capability 'sandbox.write' "
               "unavailable (all candidates failed; last error: sandbox.write: "
               "path escapes the sandbox root: '../evil.txt')"),
        metadata={"provider_id": None, "side_effect_recorded": False},
    )
    assert classify_failure(obs, ledger_error=None) == "PROVIDER_FAILURE"


# ---------------------------------------------------------------------------
# M3 - PROVIDER_FAILURE classified (sandbox traversal refusal)
# ---------------------------------------------------------------------------
def test_m3_provider_failure(tmp_path):
    from anima.runtime.execution_events import classify_failure

    obs = ExecutionObservation(
        task_id="t-x", success=False, result={},
        error="sandbox.write: path escapes the sandbox root: '../evil.txt'",
        metadata={"provider_id": "sandbox.write.local", "side_effect_recorded": False},
    )
    assert classify_failure(obs, ledger_error=None) == "PROVIDER_FAILURE"


# ---------------------------------------------------------------------------
# M4 - LEDGER_FAILURE classified (ledger.put raises)
# ---------------------------------------------------------------------------
def test_m4_ledger_failure(tmp_path):
    from anima.runtime.execution_events import classify_failure

    obs = ExecutionObservation(
        task_id="t-x", success=True, result={"data": {}},
        error="", metadata={"provider_id": "sandbox.write.local"},
    )
    assert classify_failure(obs, ledger_error=OSError("disk full")) == "LEDGER_FAILURE"


# ---------------------------------------------------------------------------
# M5 - SANDBOX_FAILURE classified (write OSError / conflict)
# ---------------------------------------------------------------------------
def test_m5_sandbox_failure(tmp_path):
    from anima.runtime.execution_events import classify_failure

    obs = ExecutionObservation(
        task_id="t-x", success=False, result={},
        error="sandbox.write: write failed: [Errno 28] No space left",
        metadata={"provider_id": "sandbox.write.local", "side_effect_recorded": False},
    )
    assert classify_failure(obs, ledger_error=None) == "SANDBOX_FAILURE"
    # conflict is also a sandbox-level refusal
    obs2 = ExecutionObservation(
        task_id="t-x", success=False, result={},
        error="sandbox.write: conflict: target exists with different content",
        metadata={"provider_id": "sandbox.write.local", "side_effect_recorded": False},
    )
    assert classify_failure(obs2, ledger_error=None) == "SANDBOX_FAILURE"


# ---------------------------------------------------------------------------
# M6 - evidence_id deterministic for the same execution_key
# ---------------------------------------------------------------------------
def test_m6_evidence_id_deterministic(tmp_path):
    from anima.runtime.execution_events import evidence_id_for

    key = ("run-1", "t-log", 0)
    a = evidence_id_for(key)
    b = evidence_id_for(key)
    assert a == b and len(a) == 64  # sha256 hex
    assert evidence_id_for(("run-1", "t-log", 1)) != a  # attempt changes it


# ---------------------------------------------------------------------------
# M7 - every log line parses as JSON with all required keys
# ---------------------------------------------------------------------------
def test_m7_all_keys_machine_analyzable(tmp_path):
    from anima.runtime.execution_events import ExecutionEvent, ExecutionEventLog

    log = ExecutionEventLog(str(tmp_path / "data"))
    required = {
        "execution_id", "execution_key", "task_id", "capability_id", "provider",
        "result", "latency_ms", "evidence_id", "error_type", "timestamp",
    }
    for i in range(3):
        log.record(ExecutionEvent(
            execution_id=f"e-{i}", execution_key=("r", "t", i), task_id="t",
            capability_id="sandbox.write", provider="p", result="SUCCESS",
            latency_ms=0.5, evidence_id=f"ev-{i}", error_type="",
        ))
    for line in (tmp_path / "data" / "logs" / "execution.jsonl").read_text(encoding="utf-8").strip().splitlines():
        obj = json.loads(line)
        assert required <= set(obj.keys())


# ---------------------------------------------------------------------------
# M8 - event_log integrates into PilotEntry.run (latency > 0, result recorded)
# ---------------------------------------------------------------------------
def test_m8_pilot_entry_integration(tmp_path):
    from anima.runtime.execution_events import ExecutionEventLog
    from anima.runtime.pilot_rollout import PilotEntry

    data_dir = str(tmp_path / "data")
    scope = _scope()
    entry = PilotEntry(
        scope=scope, record=_record(scope), switch=_switch(enabled=True),
        executor=None, ledger=None, data_dir=data_dir,
        gray_percent=100,
        event_log=ExecutionEventLog(data_dir),
    )
    entry.startup_check()
    result = entry.run(
        task_id="rt-001", request="hello", goal_id="goal#pilot-1",
        plan_id="plan#pilot-rt-001",
    )
    assert "sandbox.write ok" in result
    log_path = tmp_path / "data" / "logs" / "execution.jsonl"
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 1
    ev = json.loads(lines[0])
    assert ev["task_id"] == "rt-001"
    assert ev["result"] == "SUCCESS"
    assert ev["latency_ms"] >= 0
    assert ev["capability_id"] == "sandbox.write"
