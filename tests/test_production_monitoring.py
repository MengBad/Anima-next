"""P8.11 — Production Monitoring (RED contract).

metrics + structured logs + retention + alert hooks.

Acceptance:
    A  metrics: QQ/model/system counters + latency (delegated base)
    B  conversation_summary: aggregated from the evidence JSONL (count/result)
    C  retention: max_count prunes old records, returns removed
    D  alert rule fires hook when threshold crossed (once per crossing)
    E  alert hook failure is isolated (does not break snapshot)
    F  reset_alerts allows re-fire
    G  static boundary: no System B / gateway / executor
"""
from __future__ import annotations

import pathlib
import re

from anima.pilot.conversation_evidence import (
    ConversationEvidence,
    ConversationEvidenceLog,
    hash_user_id,
)
from anima.pilot.production_monitoring import (
    RETENTION_30D,
    RETENTION_7D,
    RETENTION_72H,
    STANDARD_METRICS,
    AlertRule,
    ProductionMonitor,
    RetentionPolicy,
)


def _seed_evidence(tmp_path, n=4):
    log = ConversationEvidenceLog(str(tmp_path / "data"))
    for i in range(n):
        log.record(ConversationEvidence(
            timestamp=1786450000 + i,
            group_id="123456",
            user_id_hash=hash_user_id(f"u{i}"),
            message_id=f"m-{i}",
            request_id=f"req-{i}",
            intent="ANSWER",
            response="ok",
            execution_key=("r", "t", i),
            result="ANSWERED" if i % 2 == 0 else "DENIED",
            latency_ms=1.0,
        ))
    return log


# ---------------------------------------------------------------------------
# A - metrics delegated
# ---------------------------------------------------------------------------
def test_a_metrics(tmp_path):
    mon = ProductionMonitor(str(tmp_path / "data"))
    mon.inc("messages_received", 3)
    mon.record_latency(5.0)
    metrics = mon.snapshot(apply_retention=False)["metrics"]
    assert metrics["messages_received"] == 3
    assert metrics["reply_latency_ms"]["count"] == 1


# ---------------------------------------------------------------------------
# B - conversation_summary aggregated
# ---------------------------------------------------------------------------
def test_b_conversation_summary(tmp_path):
    _seed_evidence(tmp_path, n=4)
    mon = ProductionMonitor(str(tmp_path / "data"))
    summary = mon.snapshot(apply_retention=False)["metrics"]["conversation_summary"]
    assert summary["count"] == 4
    assert summary["by_result"]["ANSWERED"] == 2
    assert summary["by_result"]["DENIED"] == 2


# ---------------------------------------------------------------------------
# C - retention prunes by max_count
# ---------------------------------------------------------------------------
def test_c_retention(tmp_path):
    _seed_evidence(tmp_path, n=10)
    mon = ProductionMonitor(
        str(tmp_path / "data"),
        retention=RetentionPolicy(max_count=4),
    )
    removed = mon.apply_retention()
    assert removed == 6
    summary = mon.snapshot(apply_retention=False)["metrics"]["conversation_summary"]
    assert summary["count"] == 4


# ---------------------------------------------------------------------------
# D - alert rule fires hook once per crossing
# ---------------------------------------------------------------------------
def test_d_alert_hook(tmp_path):
    mon = ProductionMonitor(
        str(tmp_path / "data"),
        alert_rules=[AlertRule("error_rate", 0.05, "high_error")],
    )
    fired = []
    mon.register_hook(lambda name, value, thr, snap: fired.append(name))
    mon.inc("messages_answered", 100)
    mon.inc("execution_failure", 10)  # error_rate = 0.10 >= 0.05
    mon.snapshot(apply_retention=False)
    assert fired == ["high_error"]
    # second snapshot: same value -> NOT re-fired
    mon.snapshot(apply_retention=False)
    assert fired == ["high_error"]


# ---------------------------------------------------------------------------
# E - hook failure isolated
# ---------------------------------------------------------------------------
def test_e_hook_failure_isolated(tmp_path):
    mon = ProductionMonitor(
        str(tmp_path / "data"),
        alert_rules=[AlertRule("error_rate", 0.01, "r")],
    )

    def bad_hook(name, value, thr, snap):
        raise RuntimeError("hook boom")

    mon.register_hook(bad_hook)
    mon.inc("messages_answered", 10)
    mon.inc("execution_failure", 1)
    snap = mon.snapshot(apply_retention=False)  # must not raise
    assert snap["alerts"][0]["rule"] == "r"


# ---------------------------------------------------------------------------
# F - reset_alerts allows re-fire
# ---------------------------------------------------------------------------
def test_f_reset_alerts(tmp_path):
    mon = ProductionMonitor(
        str(tmp_path / "data"),
        alert_rules=[AlertRule("error_rate", 0.05, "r")],
    )
    fired = []
    mon.register_hook(lambda *a: fired.append(1))
    mon.inc("messages_answered", 10)
    mon.inc("execution_failure", 2)
    mon.snapshot(apply_retention=False)
    assert len(fired) == 1
    mon.reset_alerts()
    mon.snapshot(apply_retention=False)
    assert len(fired) == 2


# ---------------------------------------------------------------------------
# G - static boundary
# ---------------------------------------------------------------------------
def test_g_static_boundary():
    src = pathlib.Path("anima/pilot/production_monitoring.py").read_text(encoding="utf-8")
    parts = src.split('"""')
    code_body = parts[2] if len(parts) >= 3 else src
    forbidden = [
        "CapabilityGateway", "approve_and_execute", "capability_mode",
        "gateway_mode", "webui", "napcat", "subprocess", "os.environ",
        "getenv", "httpx", "ssh", "paramiko",
    ]
    for needle in forbidden:
        assert needle not in code_body, f"forbidden token: {needle}"
    # network clients must not be imported (blocked_requests is a METRIC name,
    # not the requests module — check imports line-by-line)
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")) and re.search(
            r"(^|\s)(requests|urllib|http\.client)\b", stripped
        ):
            raise AssertionError(f"network import: {stripped}")
    allowed_prefixes = ("anima.pilot.", "anima.runtime.", "anima.constitution.")
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith(("from ", "import ")) and "anima." in stripped:
            target = stripped.split(" import ")[0].replace("from ", "").strip()
            if target.startswith("anima."):
                assert target.startswith(allowed_prefixes), f"import escapes: {target!r}"


# ---------------------------------------------------------------------------
# H - P8.11 standard metrics all present in a fresh snapshot
# ---------------------------------------------------------------------------
def test_h_standard_metrics(tmp_path):
    mon = ProductionMonitor(str(tmp_path / "data"))
    metrics = mon.snapshot(apply_retention=False)["metrics"]
    for name in STANDARD_METRICS:
        assert name in metrics, name
    assert metrics["reject_count"] == 0
    assert metrics["safety_block"] == 0
    assert metrics["execution_count"] == 0


# ---------------------------------------------------------------------------
# I - P8.11 retention presets (72h / 7d / 30d) are positive seconds
# ---------------------------------------------------------------------------
def test_i_retention_presets():
    assert RETENTION_72H == 72 * 3600.0
    assert RETENTION_7D == 7 * 24 * 3600.0
    assert RETENTION_30D == 30 * 24 * 3600.0
    assert RETENTION_72H < RETENTION_7D < RETENTION_30D
    # RetentionPolicy accepts the presets as max_age
    r = RetentionPolicy(max_age_sec=RETENTION_72H)
    assert r.active() is True and r.max_age_sec == RETENTION_72H


# ---------------------------------------------------------------------------
# J - P8.11 kill switch trigger hook fires
# ---------------------------------------------------------------------------
def test_j_kill_switch_hook(tmp_path):
    mon = ProductionMonitor(str(tmp_path / "data"))
    fired = []
    mon.register_hook(lambda name, value, thr, snap: fired.append(
        (name, snap.get("group_id"))))
    mon.on_kill_switch_trigger(group_id="123456")
    assert fired and fired[0] == ("kill_switch_trigger", "123456")
    metrics = mon.snapshot(apply_retention=False)["metrics"]
    assert metrics["kill_switch_trigger"] == 1


# ---------------------------------------------------------------------------
# K - P8.11 provider failure hook fires
# ---------------------------------------------------------------------------
def test_k_provider_failure_hook(tmp_path):
    mon = ProductionMonitor(str(tmp_path / "data"))
    fired = []
    mon.register_hook(lambda name, value, thr, snap: fired.append(
        (name, snap.get("capability"))))
    mon.on_provider_failure(capability="sandbox.write", error="disk full")
    assert fired and fired[0] == ("provider_failure", "sandbox.write")
    metrics = mon.snapshot(apply_retention=False)["metrics"]
    assert metrics["provider_failure"] == 1
