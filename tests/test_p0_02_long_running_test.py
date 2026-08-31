"""P0-02 — Long Running Test 单元测试（确定性，无 LLM）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from anima.os import long_running as lr
from anima.os.reliability import HealthProof, HealthCheck, build_health_proof, persist_proof, ProofSource


class _FakeAudit:
    def __init__(self, rows=None):
        self._rows = rows or []

    def recent(self, limit=100):
        return list(self._rows)


def _write_proof(data_dir: Path, day: int, overall: str, anomaly: int, now: float) -> None:
    """直接写一个最小健康证明行（绕过整份构建，专注 P0-02 聚合）。"""
    root = Path(data_dir) / "health_proof"
    root.mkdir(parents=True, exist_ok=True)
    proof = {
        "schema": "anima.health_proof/v1",
        "generated_at": now,
        "overall": overall,
        "anomaly_count": anomaly,
        "proof_sha256": "sha256:fake",
    }
    day_str = "2026-08-%02d" % day
    (root / f"{day_str}.jsonl").write_text(json.dumps(proof) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. 7 天窗口聚合：有证明覆盖 + 无异常 → 稳定
# ---------------------------------------------------------------------------
def test_window_7_stable(tmp_path):
    now = 1_700_000_000
    for d in range(1, 8):
        _write_proof(tmp_path / "data", d, "HEALTHY", 0, now - (8 - d) * 86400)
    rec = lr.collect_stability(str(tmp_path / "data"), window_days=7, audit=_FakeAudit([]), now=now)
    assert rec.proofs_total == 7
    assert rec.days_covered == 7
    assert rec.uptime_ratio == pytest.approx(1.0)
    assert rec.degraded_days == 0
    assert rec.anomaly_total == 0
    assert rec.data_complete is True
    assert rec.stable is True


# ---------------------------------------------------------------------------
# 2. 含降级天 / 异常 → 不稳定，但 data_complete 仍 True
# ---------------------------------------------------------------------------
def test_window_7_with_degraded(tmp_path):
    now = 1_700_000_000
    for d in range(1, 8):
        overall = "DEGRADED" if d == 3 else "HEALTHY"
        anomaly = 2 if d == 3 else 0
        _write_proof(tmp_path / "data", d, overall, anomaly, now - (8 - d) * 86400)
    rec = lr.collect_stability(str(tmp_path / "data"), window_days=7, audit=_FakeAudit([]), now=now)
    assert rec.degraded_days == 1
    assert rec.anomaly_total == 2
    assert rec.stable is False  # 有降级天


# ---------------------------------------------------------------------------
# 3. 审计事件分类：权限漂移 / 恢复 / 演化计数
# ---------------------------------------------------------------------------
def test_audit_event_classification(tmp_path):
    now = 1_700_000_000
    _write_proof(tmp_path / "data", 1, "HEALTHY", 0, now)
    rows = [
        {"ts": now - 100, "type": "permission_revoked", "outcome": "denied"},
        {"ts": now - 200, "type": "permission_granted", "outcome": "allowed"},
        {"ts": now - 300, "type": "recovery_plan", "outcome": "proposed"},
        {"ts": now - 400, "type": "evolution_approved", "outcome": "approved"},
        {"ts": now - 8 * 86400, "type": "permission_revoked"},  # 超出 7 天窗口
    ]
    rec = lr.collect_stability(str(tmp_path / "data"), window_days=7, audit=_FakeAudit(rows), now=now)
    assert rec.permission_drift_count == 2
    assert rec.recovery_count == 1
    assert rec.evolution_count == 1


# ---------------------------------------------------------------------------
# 4. 审计缺失 → data_complete=False（fail-closed 诚实，不编造）
# ---------------------------------------------------------------------------
def test_audit_missing_data_incomplete(tmp_path):
    now = 1_700_000_000
    _write_proof(tmp_path / "data", 1, "HEALTHY", 0, now)
    rec = lr.collect_stability(str(tmp_path / "data"), window_days=7, audit=None, now=now)
    assert rec.data_complete is False
    assert rec.permission_drift_count == 0
    assert rec.recovery_count == 0
    assert rec.evolution_count == 0


# ---------------------------------------------------------------------------
# 5. 30 / 90 天窗口：无数据 → days_covered=0, uptime_ratio=0, stable=False
# ---------------------------------------------------------------------------
def test_window_30_empty(tmp_path):
    now = 1_700_000_000
    rec = lr.collect_stability(str(tmp_path / "data"), window_days=30, audit=_FakeAudit([]), now=now)
    assert rec.proofs_total == 0
    assert rec.days_covered == 0
    assert rec.uptime_ratio == 0.0
    assert rec.stable is False


def test_window_90_empty(tmp_path):
    now = 1_700_000_000
    rec = lr.collect_stability(str(tmp_path / "data"), window_days=90, audit=_FakeAudit([]), now=now)
    assert rec.window_days == 90
    assert rec.stable is False


# ---------------------------------------------------------------------------
# 6. 落盘 + 读取最近一条
# ---------------------------------------------------------------------------
def test_persist_and_latest(tmp_path):
    now = 1_700_000_000
    for d in range(1, 8):
        _write_proof(tmp_path / "data", d, "HEALTHY", 0, now - (8 - d) * 86400)
    rec = lr.collect_stability(str(tmp_path / "data"), window_days=7, audit=_FakeAudit([]), now=now)
    path = lr.persist_stability(rec, str(tmp_path / "data"))
    assert path.exists()
    latest = lr.latest_stability(str(tmp_path / "data"), 7)
    assert latest is not None
    assert latest.proofs_total == 7
    assert latest.proof_sha256.startswith("sha256:")


# ---------------------------------------------------------------------------
# 7. 与 P0-01 真实证明链路打通（build → persist → 聚合）
# ---------------------------------------------------------------------------
def test_end_to_end_with_p0_01_proof(tmp_path):
    now = 1_700_000_000
    src = ProofSource(
        version_path=str(tmp_path / "version.json"),
        config_text="owner: 10001\nrbac: on\n",
        core_files=[tmp_path / "a.py"],
        core=_FakeCoreP0(admins=[10001]),
        audit=_FakeAudit([]),
    )
    # 直接写一个真实版本文件占位（避免再构造）
    (tmp_path / "version.json").write_text(json.dumps({"commit": "abc"}), encoding="utf-8")
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    for d in range(1, 8):
        proof = build_health_proof(src, clock=lambda: now - (8 - d) * 86400)
        persist_proof(proof, str(tmp_path / "data"))
    rec = lr.collect_stability(str(tmp_path / "data"), window_days=7, audit=_FakeAudit([]), now=now)
    assert rec.proofs_total == 7
    assert rec.stable is True


class _FakeCoreP0:
    def __init__(self, admins):
        from functools import partial
        self.permission_store = type("P", (), {"admins": staticmethod(lambda: sorted(admins))})()


# ---------------------------------------------------------------------------
# P2-1: health_proof 主动告警 detect_alerts
# ---------------------------------------------------------------------------
def _rec(**kw):
    base = dict(
        window_days=7, generated_at=0.0, proofs_total=7, days_covered=7,
        uptime_ratio=1.0, anomaly_total=0, degraded_days=0,
        permission_drift_count=0, recovery_count=1, evolution_count=0,
        data_complete=True,
    )
    base.update(kw)
    return lr.StabilityRecord(**base)


def test_detect_alerts_stable_returns_empty():
    assert lr.detect_alerts(_rec()) == []


def test_detect_alerts_degraded_and_drift_warn():
    rec = _rec(degraded_days=2, permission_drift_count=2, anomaly_total=5)
    alerts = lr.detect_alerts(rec)
    types = {a["type"] for a in alerts}
    assert {"degraded", "permission_drift", "anomaly_spike"} <= types
    # warn 级（触发 owner 待办）
    assert all(a["severity"] == "warn" for a in alerts if a["type"] != "recovery_spike")


def test_detect_alerts_recovery_spike_info():
    alerts = lr.detect_alerts(_rec(recovery_count=8))
    spike = [a for a in alerts if a["type"] == "recovery_spike"]
    assert len(spike) == 1 and spike[0]["severity"] == "info"


def test_detect_alerts_incomplete_source():
    alerts = lr.detect_alerts(_rec(data_complete=False))
    assert any(a["type"] == "incomplete" for a in alerts)
