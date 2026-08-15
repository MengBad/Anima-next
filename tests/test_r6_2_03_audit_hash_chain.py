"""R6.2-03 Audit durability — hash chain 完整性测试。

验证：
  * 连续事件形成 hash chain（previous_hash 链接）。
  * 修改任一记录字段 → verify() 失败。
  * 删除任一记录 → verify() 失败。
  * 重排记录 → verify() 失败。
  * 事件记录含规格要求字段：timestamp / capability / principal /
    decision / reason / execution_id / previous_hash / event_hash。
"""
from __future__ import annotations

import time

from anima.capability.governance.audit_chain import HashChainAuditSink


def _sink():
    clock = [1700000000.0]
    def tick():
        clock[0] += 1.0
        return clock[0]
    return HashChainAuditSink(clock=tick)


def _emit(sink, n=3):
    for i in range(n):
        sink.emit(
            "capability_invocation",
            capability="python.exec",
            principal=f"user:{1000 + i}",
            decision="ALLOW" if i % 2 == 0 else "DENY",
            reason="owner_authorized" if i % 2 == 0 else "principal_not_authorized",
            execution_id=f"exec-{i}",
        )


def test_chain_integrity_ok():
    s = _sink()
    _emit(s, 3)
    ok, detail = s.verify()
    assert ok is True
    assert "intact" in detail


def test_record_fields_present():
    s = _sink()
    s.emit("capability_invocation", capability="python.exec",
           principal="user:1", decision="DENY",
           reason="principal_not_authorized", execution_id="exec-x")
    rec = s.records()[0]
    for f in ["timestamp", "capability", "principal", "decision",
              "reason", "execution_id", "previous_hash", "event_hash"]:
        assert f in rec, f"missing field {f}"
    assert rec["previous_hash"].startswith("GENESIS") or ":" in rec["previous_hash"]
    assert rec["event_hash"].startswith("sha256:")


def test_tamper_modification_detected():
    s = _sink()
    _emit(s, 3)
    s.records()[1]["reason"] = "TAMPERED"
    ok, detail = s.verify()
    assert ok is False
    assert "tamper" in detail


def test_tamper_deletion_detected():
    s = _sink()
    _emit(s, 3)
    s._records.pop(1)  # 删除中间记录
    ok, detail = s.verify()
    assert ok is False
    assert "link broken" in detail


def test_tamper_reorder_detected():
    s = _sink()
    _emit(s, 3)
    recs = s._records
    recs[0], recs[1] = recs[1], recs[0]  # 重排
    ok, detail = s.verify()
    assert ok is False
