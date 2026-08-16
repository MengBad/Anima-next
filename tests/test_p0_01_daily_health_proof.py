"""P0-01 — ANIMA Daily Health Proof 单元测试（确定性，无 LLM）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from anima.os.reliability import (
    PROOF_DEGRADED,
    PROOF_HEALTHY,
    HealthCheck,
    ProofSource,
    answer_was_anomaly_yesterday,
    build_health_proof,
    persist_proof,
)


class _FakeAudit:
    """可注入的审计日志：按 ts + outcome 记录。"""

    def __init__(self, rows=None):
        self._rows = rows or []

    def recent(self, limit=100):
        return list(self._rows)


class _FakePerm:
    def __init__(self, admins):
        self._admins = admins

    def admins(self):
        return sorted(self._admins)


class _FakeCore:
    def __init__(self, admins=None, registry=None):
        self.permission_store = _FakePerm(admins or [])
        self.agent_registry = registry


class _FakeRegistry:
    def __init__(self, agents):
        self._agents = agents

    def list(self):
        return list(self._agents)


def _write_version(tmp_path: Path, commit: str) -> str:
    p = tmp_path / "version.json"
    p.write_text(json.dumps({"commit": commit}), encoding="utf-8")
    return str(p)


def _write_core_file(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 1. 全绿证明（HEALTHY）
# ---------------------------------------------------------------------------
def test_proof_healthy_all_checks_pass(tmp_path):
    version_path = _write_version(tmp_path, "abc123")
    f1 = _write_core_file(tmp_path, "core_a.py", "print('a')")
    core = _FakeCore(admins=[10001])
    audit = _FakeAudit([{"ts": 1_700_000_000, "type": "owner_cmd", "outcome": "allowed"}])

    proof = build_health_proof(ProofSource(
        version_path=version_path,
        config_text="owner: 10001\nrbac: on\n",
        core_files=[f1],
        core=core,
        audit=audit,
    ))

    assert proof.overall == PROOF_HEALTHY
    assert proof.anomalies() == []
    assert proof.version == "abc123"
    assert proof.config_hash_value.startswith("sha256:")
    assert proof.file_hashes[str(f1)].startswith("sha256:")
    assert proof.proof_sha256.startswith("sha256:")


# ---------------------------------------------------------------------------
# 2. 漂移检测 → DEGRADED（R6.2 approved_sha 比对）
# ---------------------------------------------------------------------------
def test_proof_degraded_on_drift(tmp_path):
    version_path = _write_version(tmp_path, "runningSHA")
    core = _FakeCore(admins=[10001])
    audit = _FakeAudit([])

    proof = build_health_proof(ProofSource(
        version_path=version_path,
        core_files=[_write_core_file(tmp_path, "a.py", "x")],
        core=core,
        audit=audit,
        approved_sha="approvedSHA",  # 与 runningSHA 不一致
    ))

    assert proof.overall == PROOF_DEGRADED
    drift_checks = [c for c in proof.checks if c.name == "runtime_drift"]
    assert drift_checks and not drift_checks[0].ok
    assert proof.drift["status"] == "DRIFT DETECTED"


# ---------------------------------------------------------------------------
# 3. 权限边界失效 → DEGRADED
# ---------------------------------------------------------------------------
def test_proof_degraded_on_empty_admins(tmp_path):
    version_path = _write_version(tmp_path, "abc123")
    core = _FakeCore(admins=[])  # Owner 边界失效
    audit = _FakeAudit([])

    proof = build_health_proof(ProofSource(
        version_path=version_path,
        core_files=[_write_core_file(tmp_path, "a.py", "x")],
        core=core,
        audit=audit,
    ))

    assert proof.overall == PROOF_DEGRADED
    perm = [c for c in proof.checks if c.name == "permissions"][0]
    assert not perm.ok


# ---------------------------------------------------------------------------
# 4. 审计不可用 → DEGRADED（可证明自身的前提）
# ---------------------------------------------------------------------------
def test_proof_degraded_on_missing_audit(tmp_path):
    version_path = _write_version(tmp_path, "abc123")
    core = _FakeCore(admins=[10001])

    proof = build_health_proof(ProofSource(
        version_path=version_path,
        core_files=[_write_core_file(tmp_path, "a.py", "x")],
        core=core,
        audit=None,  # 审计 sink 缺失
    ))

    assert proof.overall == PROOF_DEGRADED
    audit_check = [c for c in proof.checks if c.name == "audit"][0]
    assert not audit_check.ok


# ---------------------------------------------------------------------------
# 5. 审计异常计数（近 24h DENY 聚合）
# ---------------------------------------------------------------------------
def test_audit_denial_counting(tmp_path, monkeypatch):
    version_path = _write_version(tmp_path, "abc123")
    core = _FakeCore(admins=[10001])
    now = 1_700_000_000
    monkeypatch.setattr("anima.os.reliability.time.time", lambda: now)
    rows = [
        {"ts": now - 100, "type": "agent_stage", "outcome": "denied"},
        {"ts": now - 200, "type": "rbac_decision", "outcome": "denied"},
        {"ts": now - 90000, "type": "agent_stage", "outcome": "denied"},  # 超出 24h 窗口
        {"ts": now - 300, "type": "owner_cmd", "outcome": "allowed"},
    ]
    audit = _FakeAudit(rows)

    proof = build_health_proof(ProofSource(
        version_path=version_path,
        config_text="owner: 10001\nrbac: on\n",
        core_files=[_write_core_file(tmp_path, "a.py", "x")],
        core=core,
        audit=audit,
    ), clock=lambda: now)

    assert proof.audit_summary["denials_24h"] == 2  # 仅统计 24h 内
    assert proof.overall == PROOF_HEALTHY  # 有 DENY 但审计本身可用


# ---------------------------------------------------------------------------
# 6. 核心文件被篡改/缺失 → DEGRADED（fail-closed）
# ---------------------------------------------------------------------------
def test_proof_degraded_on_missing_core_file(tmp_path):
    version_path = _write_version(tmp_path, "abc123")
    missing = tmp_path / "does_not_exist.py"
    core = _FakeCore(admins=[10001])

    proof = build_health_proof(ProofSource(
        version_path=version_path,
        core_files=[missing],
        core=core,
        audit=_FakeAudit([]),
    ))

    assert proof.overall == PROOF_DEGRADED
    fc = [c for c in proof.checks if c.name == "file_hashes"][0]
    assert not fc.ok


# ---------------------------------------------------------------------------
# 7. Agent 状态：注册表缺失 → DEGRADED；社会角色缺 → 仍 HEALTHY（非致命）
# ---------------------------------------------------------------------------
def test_agent_registry_missing_is_informational(tmp_path):
    version_path = _write_version(tmp_path, "abc123")
    proof = build_health_proof(ProofSource(
        version_path=version_path,
        config_text="owner: 10001\nrbac: on\n",
        core_files=[_write_core_file(tmp_path, "a.py", "x")],
        core=_FakeCore(admins=[10001]),  # 无 agent_registry
        audit=_FakeAudit([]),
    ))
    # 注册表缺失为观测性（不计入异常，避免误报噪声）
    agent_check = [c for c in proof.checks if c.name == "agents"][0]
    assert agent_check.ok
    assert proof.overall == PROOF_HEALTHY


def test_agent_society_some_roles_missing_still_healthy(tmp_path):
    from anima.agent.orchestrator import AgentIdentity

    version_path = _write_version(tmp_path, "abc123")
    reg = _FakeRegistry([AgentIdentity(agent_id="coding", role="coder", capabilities={})])
    proof = build_health_proof(ProofSource(
        version_path=version_path,
        config_text="owner: 10001\nrbac: on\n",
        core_files=[_write_core_file(tmp_path, "a.py", "x")],
        core=_FakeCore(admins=[10001], registry=reg),
        audit=_FakeAudit([]),
    ))
    assert proof.overall == PROOF_HEALTHY
    agent_check = [c for c in proof.checks if c.name == "agents"][0]
    assert "architect" in agent_check.extra["missing_society_roles"]


# ---------------------------------------------------------------------------
# 8. 落盘 + 「昨天有没有异常？」问答
# ---------------------------------------------------------------------------
def test_persist_and_answer_no_anomaly_yesterday(tmp_path):
    version_path = _write_version(tmp_path, "abc123")
    core = _FakeCore(admins=[10001])
    src = ProofSource(
        version_path=version_path,
        config_text="owner: 10001\nrbac: on\n",
        core_files=[_write_core_file(tmp_path, "a.py", "x")],
        core=core,
        audit=_FakeAudit([]),
    )

    day1 = build_health_proof(src, clock=lambda: 1_700_000_000)
    day2 = build_health_proof(src, clock=lambda: 1_700_086_400)  # +1 天
    persist_proof(day1, str(tmp_path / "data"))
    persist_proof(day2, str(tmp_path / "data"))

    ans = answer_was_anomaly_yesterday(str(tmp_path / "data"))
    assert ans["answer"] == "no"
    assert ans["yesterday_anomaly_count"] == 0
    assert "证明" in ans["message"]


def test_answer_unknown_when_no_history(tmp_path):
    ans = answer_was_anomaly_yesterday(str(tmp_path / "data"))
    assert ans["answer"] == "unknown"
    assert ans["yesterday_anomaly_count"] is None
