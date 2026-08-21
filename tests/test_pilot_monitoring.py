"""P8.8 — Live Pilot Monitoring (RED contract).

QQ indicators: messages_received / messages_answered / reply_latency /
error_rate / blocked_requests.  Model indicators: wrong_answer_report /
hallucination_report / tool_failure / execution_failure.  System: CPU /
Memory / Queue / Ledger / Storage.

Acceptance:
    A  counters start at 0; inc() increments
    B  latency stats (avg/p95/max) computed from recorded latencies
    C  error_rate = (execution_failure + tool_failure) / answered
    D  snapshot includes QQ + model + system gauges
    E  ledger_facts gauge counts durable facts
    F  dump() writes JSON with timestamp + metrics
    G  static boundary: no System B / gateway / executor import
"""
from __future__ import annotations

import json
import pathlib

from anima.core.persistence import PersistenceProvider
from anima.runtime.execution_ledger import ExecutionLedger
from anima.runtime.executor import ExecutionObservation
from anima.pilot.monitoring import PilotMonitor


def _monitor(tmp_path):
    return PilotMonitor(str(tmp_path / "data"))


# ---------------------------------------------------------------------------
# A - counters start at 0, inc increments
# ---------------------------------------------------------------------------
def test_a_counters():
    m = _monitor(__import__("pathlib").Path(".") / "_tmp_m")
    assert m._counters["messages_received"] == 0
    m.inc("messages_received")
    m.inc("messages_received", 3)
    assert m._counters["messages_received"] == 4


# ---------------------------------------------------------------------------
# B - latency stats
# ---------------------------------------------------------------------------
def test_b_latency_stats(tmp_path):
    m = _monitor(tmp_path)
    for v in (10, 20, 30, 40, 100):
        m.record_latency(v)
    snap = m.snapshot().to_dict()["metrics"]
    assert snap["reply_latency_ms"]["count"] == 5
    assert snap["reply_latency_ms"]["avg"] == 40.0
    assert snap["reply_latency_ms"]["max"] == 100.0


# ---------------------------------------------------------------------------
# C - error_rate
# ---------------------------------------------------------------------------
def test_c_error_rate(tmp_path):
    m = _monitor(tmp_path)
    assert m.snapshot().to_dict()["metrics"]["error_rate"] == 0.0
    m.inc("messages_answered", 100)
    m.inc("execution_failure", 2)
    m.inc("tool_failure", 1)
    assert m.snapshot().to_dict()["metrics"]["error_rate"] == 0.03


# ---------------------------------------------------------------------------
# D - snapshot includes QQ + model + system gauges
# ---------------------------------------------------------------------------
def test_d_snapshot_sections(tmp_path):
    m = _monitor(tmp_path)
    m.inc("messages_received", 10)
    m.inc("messages_answered", 9)
    m.inc("blocked_requests", 1)
    m.inc("wrong_answer_report")
    m.inc("hallucination_report")
    m.inc("tool_failure")
    m.inc("execution_failure")
    metrics = m.snapshot().to_dict()["metrics"]
    for key in ("messages_received", "messages_answered", "blocked_requests",
                "wrong_answer_report", "hallucination_report", "tool_failure",
                "execution_failure", "reply_latency_ms", "error_rate", "system"):
        assert key in metrics
    assert "ledger_facts" in metrics["system"]


# ---------------------------------------------------------------------------
# E - ledger_facts gauge counts durable facts
# ---------------------------------------------------------------------------
def test_e_ledger_facts(tmp_path):
    from anima.runtime.execution_ledger import ExecutionLedger

    ledger = ExecutionLedger(persistence=PersistenceProvider(str(tmp_path / "data")))
    ledger.put(("r1", "t1", 0), ExecutionObservation(task_id="t1", success=True))
    ledger.put(("r1", "t2", 0), ExecutionObservation(task_id="t2", success=True))
    m = _monitor(tmp_path)
    gauges = m.snapshot().to_dict()["metrics"]["system"]
    assert gauges["ledger_facts"] == 2


# ---------------------------------------------------------------------------
# F - dump writes JSON
# ---------------------------------------------------------------------------
def test_f_dump(tmp_path):
    m = _monitor(tmp_path)
    m.inc("messages_received", 5)
    out = tmp_path / "monitor.json"
    text = m.dump(str(out))
    obj = json.loads(text)
    assert obj["metrics"]["messages_received"] == 5
    assert (tmp_path / "monitor.json").exists()


# ---------------------------------------------------------------------------
# G - static boundary
# ---------------------------------------------------------------------------
def test_g_static_boundary():
    src = pathlib.Path("anima/pilot/monitoring.py").read_text(encoding="utf-8")
    parts = src.split('"""')
    code_body = parts[2] if len(parts) >= 3 else src
    forbidden = [
        "executor", "Executor", "PlanOrchestrator", "pilot_rollout",
        "CapabilityGateway", "gateway", "tools", "approve_and_execute",
        "capability_mode", "webui", "napcat", "subprocess", "os.environ",
        "getenv", "requests", "httpx", "ssh", "paramiko",
    ]
    for needle in forbidden:
        assert needle not in code_body, f"forbidden token: {needle}"
    allowed_prefixes = ("anima.pilot.", "anima.runtime.", "anima.constitution.")
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith(("from ", "import ")) and "anima." in stripped:
            target = stripped.split(" import ")[0].replace("from ", "").strip()
            if target.startswith("anima."):
                assert target.startswith(allowed_prefixes), f"import escapes: {target!r}"
