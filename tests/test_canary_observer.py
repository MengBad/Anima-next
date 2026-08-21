"""P9.5 72h canary observer tests (RED -> GREEN).

The observer periodically snapshots the production monitor dashboard into an
archive and evaluates alert rules, producing a machine-readable observation
file for the 72h window.  It is read-only over the pilot runtime (metrics +
evidence), never touches System B, and fails closed when the monitor cannot
be built.
"""

import json
import pathlib
import time

import pytest

from anima.pilot.canary_observer import CanaryObserver, ObserverConfig


def _cfg(**over):
    base = dict(
        data_dir=str(pathlib.Path("_tmp_obs") / "data"),
        interval_sec=0.05,
        alert_rules=[{"metric": "error_rate", "threshold": 0.01}],
    )
    base.update(over)
    return ObserverConfig(**base)


# ---------------------------------------------------------------------------
# A - observer snapshots the dashboard archive
# ---------------------------------------------------------------------------


def test_a_observer_snapshots(tmp_path):
    cfg = _cfg(data_dir=str(tmp_path / "data"), interval_sec=0.02)
    obs = CanaryObserver(cfg)
    obs._probe_napcat = lambda: True  # deterministic fast probe
    obs.start()
    time.sleep(0.15)  # a few ticks
    obs.stop()
    archive = pathlib.Path(tmp_path / "data" / "logs" / "canary_observations.jsonl")
    assert archive.exists()
    lines = archive.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 2
    first = json.loads(lines[0])
    assert "ts" in first
    assert "metrics" in first
    assert "qq_connection" in first
    assert "alerts" in first


# ---------------------------------------------------------------------------
# B - alerts appear in the observation when thresholds are crossed
# ---------------------------------------------------------------------------


def test_b_observer_alerts(tmp_path):
    cfg = _cfg(data_dir=str(tmp_path / "data"), interval_sec=0.02)
    obs = CanaryObserver(cfg)
    obs.start()
    # force error_rate over the threshold via the monitor counters
    obs._monitor.inc("messages_answered", 10)
    obs._monitor.inc("execution_failure", 5)  # error_rate = 0.5 >= 0.01
    time.sleep(0.12)
    obs.stop()
    archive = pathlib.Path(tmp_path / "data" / "logs" / "canary_observations.jsonl")
    alerts_seen = False
    for line in archive.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        if rec.get("alerts"):
            alerts_seen = True
            assert any(a["rule"] == "error_rate>=0.01" for a in rec["alerts"])
    assert alerts_seen


# ---------------------------------------------------------------------------
# C - observer fails closed when data dir is invalid
# ---------------------------------------------------------------------------


def test_c_observer_fail_closed(tmp_path):
    with pytest.raises(ValueError):
        CanaryObserver(_cfg(data_dir="/opt/anima"))  # forbidden root


# ---------------------------------------------------------------------------
# D - static boundary: no System B / gateway / env bypass
# ---------------------------------------------------------------------------


def test_d_static_boundary():
    src = pathlib.Path("anima/pilot/canary_observer.py").read_text(encoding="utf-8")
    parts = src.split('"""')
    body = parts[2] if len(parts) >= 3 else src
    forbidden = (
        "CapabilityGateway", "approve_and_execute", "capability_mode",
        "gateway_mode", "webui", "napcat", "subprocess", "os.environ",
        "getenv", "requests", "httpx", "ssh", "paramiko",
        "capability.governance", "capability.health", "anima.tools",
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
