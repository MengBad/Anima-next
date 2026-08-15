"""R6.5 —— Execution Evidence：完整行为证据系统验证（trace→artifact→audit→replay）。

覆盖 R6.5 完成标准（任何 Agent 行为可以事后还原）：
- execution trace：一次行为完整轨迹（execution_id/principal/capability/decision/provider/sandbox/result）
- artifact：行为产出工件（execution_id 关联 + 哈希指纹）
- audit：trace 投影出审计事件（谁/什么/结果）
- replay：由 execution_id 还原全过程，产出可读报告
- 反伪造：不存在的 execution_id 还原 → None（不伪造"可能发生过"）
- 指纹：参数只留哈希指纹，不存原始敏感参数
- fail-open：证据存储写失败只计数，不阻断

纪律（R6.5 anchored）：只写新增模块 + 测试，不修改共享核心。
"""
from __future__ import annotations

import time

import pytest

from anima.capability.evidence import (
    ArtifactRecord,
    EvidenceSink,
    EvidenceStore,
    ExecutionTrace,
    ReplayEngine,
    args_fingerprint,
)


def _trace(execution_id="exec-1", **kw) -> ExecutionTrace:
    base = dict(
        execution_id=execution_id,
        principal="root",
        capability="python.exec",
        decision="allowed",
        provider="python.exec",
        sandbox="iso-user",
        result="success",
        args_hash="sha256:abc",
        started_at=1000.0,
        finished_at=1000.3,
        resource_account={"cpu_seconds": 0.3, "wall_seconds": 0.3},
    )
    base.update(kw)
    return ExecutionTrace(**base)


# --------------------------------------------------------------------------
# 一、execution trace：行为完整轨迹
# --------------------------------------------------------------------------

def test_trace_fields_complete():
    t = _trace()
    assert t.execution_id == "exec-1"
    assert t.principal == "root"
    assert t.capability == "python.exec"
    assert t.decision == "allowed"
    assert t.provider == "python.exec"
    assert t.sandbox == "iso-user"
    assert t.result == "success"
    assert t.latency == pytest.approx(0.3)
    assert t.success is True


def test_trace_denied_result():
    t = _trace(result="denied", decision="denied", denied_reason="principal_not_authorized")
    assert t.success is False
    assert t.result == "denied"


# --------------------------------------------------------------------------
# 二、args fingerprint：只留指纹，不存敏感参数
# --------------------------------------------------------------------------

def test_args_fingerprint_is_hash_not_raw():
    fp = args_fingerprint({"code": "import os; os.system('rm -rf /')", "principal": "hacker"})
    assert fp.startswith("sha256:")
    assert "rm -rf" not in fp      # 不落原始敏感参数
    assert "hacker" not in fp
    # 确定性
    assert fp == args_fingerprint({"code": "import os; os.system('rm -rf /')", "principal": "hacker"})


def test_args_fingerprint_deterministic_regardless_of_order():
    a = args_fingerprint({"x": 1, "y": 2})
    b = args_fingerprint({"y": 2, "x": 1})
    assert a == b


# --------------------------------------------------------------------------
# 三、persistence：trace + artifact 落盘可读
# --------------------------------------------------------------------------

def test_evidence_store_roundtrip(tmp_path):
    store = EvidenceStore(str(tmp_path / "evidence.jsonl"))
    store.append_trace(_trace())
    store.append_artifact(ArtifactRecord(
        execution_id="exec-1", name="stdout", kind="stdout", sha256="sha256:out", size=42))
    records = store.read_all()
    assert len(records) == 2
    assert records[0]["kind"] == "trace"
    assert records[0]["payload"]["execution_id"] == "exec-1"
    assert records[1]["kind"] == "artifact"
    assert records[1]["payload"]["name"] == "stdout"


def test_evidence_store_skips_bad_lines(tmp_path):
    path = tmp_path / "evidence.jsonl"
    path.write_text("bad-json\n{\"schema_version\":1,\"kind\":\"trace\",\"payload\":{\"execution_id\":1}}\n",
                    encoding="utf-8")
    store = EvidenceStore(str(path))
    records = store.read_all()
    assert store.parse_failures >= 1
    # 不抛即通过


# --------------------------------------------------------------------------
# 四、replay：由 execution_id 还原（事后还原）
# --------------------------------------------------------------------------

def test_replay_reconstructs_full_behavior(tmp_path):
    store = EvidenceStore(str(tmp_path / "evidence.jsonl"))
    sink = EvidenceSink(store)
    sink.record_trace(_trace())
    sink.record_artifact(ArtifactRecord(
        execution_id="exec-1", name="stdout", kind="stdout",
        sha256="sha256:out", size=42))
    sink.record_artifact(ArtifactRecord(
        execution_id="exec-1", name="tmp.py", kind="file", sha256="sha256:f", size=8))

    r = ReplayEngine(store).replay("exec-1")
    assert r is not None
    assert r["reconstructable"] is True
    assert r["trace"]["principal"] == "root"
    assert r["trace"]["capability"] == "python.exec"
    assert len(r["artifacts"]) == 2  # stdout + file
    assert r["audit"]["decision"] == "allowed"
    assert r["audit"]["provider"] == "python.exec"


def test_replay_missing_execution_returns_none(tmp_path):
    """不存在的 execution_id → None（不伪造"可能发生过"）。"""
    store = EvidenceStore(str(tmp_path / "evidence.jsonl"))
    store.append_trace(_trace(execution_id="exec-a"))
    r = ReplayEngine(store).replay("does-not-exist")
    assert r is None


def test_replay_report_human_readable(tmp_path):
    store = EvidenceStore(str(tmp_path / "evidence.jsonl"))
    store.append_trace(_trace())
    report = ReplayEngine(store).replay_report("exec-1")
    assert "[replay] execution exec-1" in report
    assert "principal : root" in report
    assert "capability: python.exec" in report


# --------------------------------------------------------------------------
# 五、fail-open：证据写失败只计数，不阻断
# --------------------------------------------------------------------------

def test_evidence_sink_fail_open(monkeypatch, tmp_path):
    store = EvidenceStore(str(tmp_path / "evidence.jsonl"))
    sink = EvidenceSink(store)

    def _boom(*a, **k):
        raise OSError("evidence store down")

    monkeypatch.setattr(store, "append", _boom)
    sink.record_trace(_trace())
    assert sink.write_failures == 1


# --------------------------------------------------------------------------
# 六、与 R6.2/3/4 组合：完整证据链（trace 吃 RBAC 决策 + 隔离事实）
# --------------------------------------------------------------------------

def test_evidence_composes_rbac_and_isolation(tmp_path):
    """R6.5 trace 可消费 R6.3 RBAC 决策 + R6.4 隔离事实，构成完整证据链。"""
    from anima.capability.rbac import RBACPolicyEngine, Role
    from anima.capability.isolation import isolation_limits, ResourceAccount

    # R6.3 RBAC 决策
    eng = RBACPolicyEngine(
        roles={"owner": Role(id="owner", allowed={"python.exec": frozenset({"exec"})})},
        principal_roles={"root": ["owner"]},
    )
    dec = eng.evaluate("root", "python.exec", ["exec"])

    # R6.4 隔离事实（owner + network scope → owner 隔离档）
    limits = isolation_limits(4, scopes=frozenset({"network"}))
    acct = ResourceAccount(cpu_seconds=0.3, wall_seconds=0.3)

    # 装配成 R6.5 trace 落盘
    store = EvidenceStore(str(tmp_path / "evidence.jsonl"))
    sink = EvidenceSink(store)
    sink.record_trace(ExecutionTrace(
        execution_id="exec-chain-1",
        principal="root",
        capability="python.exec",
        decision=dec.decision,
        provider="python.exec",
        sandbox=limits.isolator_name,
        result="success" if dec.allowed else "denied",
        args_hash=args_fingerprint({"code": "print(1)"}),
        started_at=100.0,
        finished_at=100.3,
        resource_account={"cpu_seconds": acct.cpu_seconds, "wall_seconds": acct.wall_seconds},
        role=dec.role,
        granted_scopes=tuple(dec.granted_scopes),
    ))

    r = ReplayEngine(store).replay("exec-chain-1")
    assert r is not None
    assert r["trace"]["decision"] == "allowed"
    assert r["trace"]["sandbox"] == "iso-owner"
    assert r["trace"]["resource_account"]["cpu_seconds"] == 0.3
    assert r["audit"]["result"] == "success"