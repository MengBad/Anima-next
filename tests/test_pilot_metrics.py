"""P9.5 monitoring-chain fix tests (RED -> GREEN).

Closes the gap found in the 72h canary: the pilot runtime's processing
events never reached the observer's monitor (all metrics stayed zero while
real traffic flowed).  Fix:
  * PilotRuntime writes one JSON line per handled event to
    logs/pilot_metrics.jsonl (cross-process source of truth)
  * ProductionMonitor aggregates it into the dashboard metrics
  * CanaryObserver probes the NapCat WS port to report qq_connected
"""

import json
import pathlib
import time

from anima.pilot.pilot_runtime import LivePilotConfig, PilotRuntime
from anima.pilot.production_monitoring import ProductionMonitor

DATA_DIR = str(pathlib.Path("_tmp_metrics") / "data")


def _ev(**overrides):
    ev = {
        "topic": "event.message.group",
        "user_id": 10001,
        "group_id": "123456",
        "message_id": "m-1",
        "text": "什么是量子计算？",
        "time": 1786450000,
        "mentions_self": True,
        "raw": {"message_type": "group", "sender": {"nickname": "u1"}},
    }
    ev.update(overrides)
    return ev


def _runtime(tmp_path):
    import os
    os.makedirs(str(tmp_path / "data" / "logs"), exist_ok=True)
    return PilotRuntime(
        config=LivePilotConfig(allowed_groups=("123456",), min_interval_sec=0.0),
        data_dir=str(tmp_path / "data"),
        metrics_log=str(tmp_path / "data" / "logs" / "pilot_metrics.jsonl"),
    )


# ---------------------------------------------------------------------------
# A - PilotRuntime records one metrics line per handled event
# ---------------------------------------------------------------------------


def test_a_metrics_written(tmp_path):
    rt = _runtime(tmp_path)
    # gate passed (ANSWER -> llm_answer, still a handled decision)
    rt.handle(_ev())
    # passive deny (no @) -> handled=False, still recorded
    rt.handle(_ev(mentions_self=False, message_id="m-2"))
    # dangerous -> DENIED, recorded
    rt.handle(_ev(text="删除服务器文件", message_id="m-3"))
    p = pathlib.Path(tmp_path / "data" / "logs" / "pilot_metrics.jsonl")
    assert p.exists()
    lines = [json.loads(l) for l in p.read_text(encoding="utf-8").strip().splitlines()]
    assert len(lines) == 3
    assert lines[0]["result"] == "ANSWERED"
    assert lines[0]["latency_ms"] >= 0
    assert lines[1]["denied_reason"] == "passive_not_triggered"
    assert lines[2]["result"] == "DENIED"
    # qq user id never stored raw (privacy)
    assert all("10001" not in json.dumps(l, ensure_ascii=False) for l in lines)


def test_a2_no_metrics_log_by_default(tmp_path):
    rt = PilotRuntime(
        config=LivePilotConfig(allowed_groups=("123456",), min_interval_sec=0.0),
        data_dir=str(tmp_path / "data"),
    )  # no metrics_log -> nothing written (backward compatible)
    rt.handle(_ev())
    assert not (pathlib.Path(tmp_path / "data") / "logs" / "pilot_metrics.jsonl").exists()


# ---------------------------------------------------------------------------
# B - ProductionMonitor aggregates pilot metrics into the dashboard
# ---------------------------------------------------------------------------


def test_b_dashboard_aggregates(tmp_path):
    rt = _runtime(tmp_path)
    rt.handle(_ev())
    rt.handle(_ev(text="删除服务器文件", message_id="m-2"))
    mon = ProductionMonitor(str(tmp_path / "data"))
    dash = json.loads(mon.dashboard())
    m = dash["metrics"]
    assert m["messages_received"] == 2
    assert m["messages_answered"] == 1
    assert m["reject_count"] == 1  # DENIED counted as reject
    assert dash["latency"]["count"] == 2


# ---------------------------------------------------------------------------
# C - CanaryObserver probes NapCat port -> qq_connected
# ---------------------------------------------------------------------------


def test_c_observer_probes_connection(tmp_path):
    from anima.pilot.canary_observer import CanaryObserver, ObserverConfig

    cfg = ObserverConfig(data_dir=str(tmp_path / "data"), interval_sec=0.02)
    obs = CanaryObserver(cfg)
    # point the probe at a reachable port (this python process's own port is
    # not listening; use the classic always-open loopback: 127.0.0.1:80 is not
    # reliable, so simulate by overriding _probe_napcat)
    obs._probe_napcat = lambda: True
    obs.tick()
    obs.stop()
    rec = obs.read_observations()[-1]
    assert rec["qq_connection"]["connected"] is True
    # and a dead endpoint reports disconnected (fail closed)
    obs2 = CanaryObserver(ObserverConfig(
        data_dir=str(tmp_path / "d2"), interval_sec=0.02))
    obs2._probe_napcat = lambda: False
    obs2.tick()
    obs2.stop()
    rec2 = obs2.read_observations()[-1]
    assert rec2["qq_connection"]["connected"] is False


# ---------------------------------------------------------------------------
# D - static boundary
# ---------------------------------------------------------------------------


def test_d_static_boundary():
    src = pathlib.Path("anima/pilot/pilot_runtime.py").read_text(encoding="utf-8")
    parts = src.split('"""')
    body = parts[2] if len(parts) >= 3 else src
    forbidden = (
        "CapabilityGateway", "approve_and_execute", "capability_mode",
        "gateway_mode", "webui", "napcat", "subprocess", "os.environ",
        "getenv", "requests", "httpx", "ssh", "paramiko",
        "capability.governance", "capability.health",
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
