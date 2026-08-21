"""P9.3 operational monitoring tests (RED -> GREEN).

Adds on top of P8.11:
  * QQ connection state (NapCat heartbeat: connected / disconnected / uptime)
  * dashboard JSON output (full operational panel)
  * alert rules loadable from config dict / file
"""

import json
import pathlib

from anima.pilot.production_monitoring import (
    AlertRule,
    ProductionMonitor,
    alert_rules_from_dict,
)

# ---------------------------------------------------------------------------
# A - QQ connection state
# ---------------------------------------------------------------------------


def test_a_qq_connection_state(tmp_path):
    mon = ProductionMonitor(str(tmp_path / "data"))
    assert mon.qq_connected() is False  # default: unknown = disconnected
    mon.set_qq_connected(True, ws_uptime_sec=123.0)
    assert mon.qq_connected() is True
    snap = mon.snapshot(apply_retention=False)["metrics"]
    assert snap["qq_connected"] == 1
    assert snap["qq_connection_uptime_sec"] == 123.0
    mon.set_qq_connected(False)
    assert mon.qq_connected() is False
    snap2 = mon.snapshot(apply_retention=False)["metrics"]
    assert snap2["qq_connected"] == 0


def test_a2_connection_flap_counter(tmp_path):
    mon = ProductionMonitor(str(tmp_path / "data"))
    mon.set_qq_connected(True)
    mon.set_qq_connected(False)
    mon.set_qq_connected(True)
    snap = mon.snapshot(apply_retention=False)["metrics"]
    assert snap["qq_disconnects"] == 1  # exactly one disconnect event


# ---------------------------------------------------------------------------
# B - dashboard JSON output
# ---------------------------------------------------------------------------


def test_b_dashboard_json(tmp_path):
    mon = ProductionMonitor(str(tmp_path / "data"))
    mon.inc("messages_received", 10)
    mon.inc("messages_answered", 8)
    mon.record_latency(50.0)
    mon.record_latency(150.0)
    mon.set_qq_connected(True, ws_uptime_sec=60.0)
    mon.on_kill_switch_trigger(group_id="123456")
    dash = json.loads(mon.dashboard())
    # required sections
    for key in ("generated_at", "qq_connection", "metrics", "latency",
                "alerts", "retention"):
        assert key in dash, f"dashboard missing section: {key}"
    assert dash["qq_connection"]["connected"] is True
    assert dash["qq_connection"]["uptime_sec"] == 60.0
    assert dash["metrics"]["messages_received"] == 10
    assert dash["latency"]["count"] == 2
    assert dash["latency"]["avg_ms"] == 100.0
    assert dash["latency"]["max_ms"] == 150.0
    assert dash["retention"]["active"] is False


def test_b2_dashboard_file(tmp_path):
    mon = ProductionMonitor(str(tmp_path / "data"))
    out = str(tmp_path / "dash" / "dashboard.json")
    text = mon.dashboard(out)
    assert pathlib.Path(out).exists()
    assert json.loads(text)["metrics"]["messages_received"] == 0


# ---------------------------------------------------------------------------
# C - alert rules from config dict / file
# ---------------------------------------------------------------------------


def test_c_alert_rules_from_dict():
    rules = alert_rules_from_dict(
        [
            {"metric": "error_rate", "threshold": 0.01},
            {"metric": "execution_failure", "threshold": 5, "name": "exec-burst"},
        ]
    )
    assert len(rules) == 2
    assert rules[0].metric == "error_rate"
    assert rules[0].threshold == 0.01
    assert rules[1].name == "exec-burst"


def test_c2_alert_rules_from_file(tmp_path):
    p = tmp_path / "alerts.json"
    p.write_text(
        json.dumps([{"metric": "qq_connected", "threshold": 0, "name": "qq-down"}]),
        encoding="utf-8",
    )
    rules = alert_rules_from_dict(json.loads(p.read_text(encoding="utf-8")))
    assert rules[0].name == "qq-down"
    # empty / None -> []
    assert alert_rules_from_dict(None) == []
    assert alert_rules_from_dict([]) == []


def test_c3_rules_drive_hooks(tmp_path):
    mon = ProductionMonitor(
        str(tmp_path / "data"),
        alert_rules=alert_rules_from_dict(
            [{"metric": "error_rate", "threshold": 0.05}]
        ),
    )
    fired = []
    mon.register_hook(lambda name, value, thr, snap: fired.append(name))
    mon.inc("messages_answered", 100)
    mon.inc("execution_failure", 10)  # error_rate = 10/100 = 0.10 >= 0.05
    mon.snapshot(apply_retention=False)
    assert "error_rate>=0.05" in fired


# ---------------------------------------------------------------------------
# D - static boundary
# ---------------------------------------------------------------------------


def test_d_static_boundary():
    src = pathlib.Path("anima/pilot/production_monitoring.py").read_text(encoding="utf-8")
    parts = src.split('"""')
    body = parts[2] if len(parts) >= 3 else src
    forbidden = (
        "executor", "Executor", "PlanOrchestrator", "pilot_rollout",
        "CapabilityGateway", "gateway", "tools", "approve_and_execute",
        "capability_mode", "webui", "napcat", "subprocess", "os.environ",
        "getenv", "httpx", "ssh", "paramiko",
    )
    for needle in forbidden:
        assert needle not in body, f"forbidden token: {needle}"
    allowed_prefixes = ("anima.pilot.", "anima.runtime.", "anima.constitution.")
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith(("from ", "import ")) and "anima." in stripped:
            target = stripped.split(" import ")[0].replace("from ", "").strip()
            if target.startswith("anima."):
                assert target.startswith(allowed_prefixes), f"import escapes: {target!r}"
