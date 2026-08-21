"""P9.5 — 72h canary observer.

A periodic, read-only sampler that snapshots the production monitor
dashboard into an append-only JSONL archive (``logs/canary_observations.jsonl``)
so the full 72h window is machine-readable:

    {"ts": float, "metrics": {...}, "qq_connection": {...},
     "latency": {...}, "alerts": [...], "alert_rules": [...]}

The observer:
  * builds a ProductionMonitor over the canary data dir (fail closed on
    invalid dirs — never a system / production root)
  * evaluates the configured alert rules on every tick
  * never touches System B / gateway / capability activation
  * is strictly read-only over the pilot runtime state

CLI: ``python -m anima.pilot.canary_observer --data-dir DIR --minutes 4320``
(4320 min = 72h).
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path
from typing import Optional

__all__ = ["CanaryObserver", "ObserverConfig"]


class ObserverConfig:
    def __init__(
        self,
        *,
        data_dir: str,
        interval_sec: float = 60.0,
        alert_rules: Optional[list] = None,
    ) -> None:
        self.data_dir = data_dir
        self.interval_sec = float(interval_sec)
        self.alert_rules = list(alert_rules or [])


class CanaryObserver:
    """Periodic dashboard sampler for the 72h canary observation window."""

    def __init__(self, config: ObserverConfig) -> None:
        from anima.pilot.canary import validate_canary_data_dir
        from anima.pilot.production_monitoring import (
            ProductionMonitor,
            alert_rules_from_dict,
        )

        validate_canary_data_dir(config.data_dir)
        self._config = config
        self._monitor = ProductionMonitor(
            config.data_dir,
            alert_rules=alert_rules_from_dict(config.alert_rules),
        )
        # P9.5: NapCat WS endpoint probed each tick (cross-process truth).
        # configurable for tests via _napcat_probe.
        self._napcat_probe = ("127.0.0.1", 3001)
        self._archive = (
            Path(config.data_dir) / "logs" / "canary_observations.jsonl"
        )
        self._archive.parent.mkdir(parents=True, exist_ok=True)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="canary-observer", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._config.interval_sec + 2.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - observation must never die
                import logging

                logging.getLogger("anima.pilot.observer").exception(
                    "canary observer tick failed"
                )
            self._stop.wait(self._config.interval_sec)

    # -- sampling ------------------------------------------------------------

    def _probe_napcat(self) -> bool:
        """Probe the NapCat WS port (cross-process connectivity truth).
        Fail closed: any probe error reports disconnected."""
        import socket

        host, port = self._napcat_probe
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2.0)
            try:
                sock.connect((host, port))
                return True
            finally:
                sock.close()
        except OSError:
            return False

    def tick(self) -> None:
        """Append one dashboard snapshot to the archive."""
        # report the NapCat gateway connectivity (cross-process probe)
        self._monitor.set_qq_connected(self._probe_napcat())
        text = self._monitor.dashboard()
        snap = json.loads(text)
        snap["ts"] = time.time()
        with self._archive.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(snap, ensure_ascii=False) + "\n")

    def read_observations(self) -> list[dict]:
        if not self._archive.exists():
            return []
        out: list[dict] = []
        for line in self._archive.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def summarize(self) -> dict:
        """Aggregate the observation archive into a compact summary."""
        obs = self.read_observations()
        if not obs:
            return {"observations": 0}
        last = obs[-1]
        metrics = last.get("metrics", {})
        alerts_total = sum(len(o.get("alerts", [])) for o in obs)
        return {
            "observations": len(obs),
            "window_seconds": round(last.get("ts", 0) - obs[0].get("ts", 0), 3),
            "metrics": metrics,
            "alerts_total": alerts_total,
            "last_alerts": last.get("alerts", []),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="P9.5 canary observer")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--minutes", type=float, default=4320.0, help="72h = 4320")
    args = parser.parse_args()
    observer = CanaryObserver(
        ObserverConfig(
            data_dir=args.data_dir,
            interval_sec=args.interval,
            alert_rules=[
                {"metric": "error_rate", "threshold": 0.01},
                # qq-down fires only when a DISCONNECT is counted (the
                # connected gauge is 1 while up; a >= threshold on it would
                # fire spuriously).
                {"metric": "qq_disconnects", "threshold": 1, "name": "qq-down"},
            ],
        )
    )
    observer.start()
    deadline = time.time() + args.minutes * 60.0
    try:
        while time.time() < deadline:
            time.sleep(min(30.0, max(1.0, args.interval)))
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        print(json.dumps(observer.summarize(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
