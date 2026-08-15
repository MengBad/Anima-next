"""R6.2-03 — 审计持久性：防篡改 hash chain。

每条审计事件追加到仅可追加的 hash chain。每条记录携带前一条记录的哈希
（previous_hash）以及自身哈希（event_hash）。通过重放 chain 并对每条重新计算
event_hash（依赖 previous_hash + 规范化负载）即可检测：删除 / 修改 / 重排。

事件记录字段（依 R6.2 规格）：
    timestamp, capability, principal, decision, reason,
    execution_id, previous_hash, event_hash
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Optional

GENESIS = "GENESIS"


def _canonical(record: dict) -> str:
    """用于哈希的稳定 JSON 序列化（仅非哈希字段，key 排序）。"""
    keys = ["timestamp", "capability", "principal", "decision",
            "reason", "execution_id", "previous_hash"]
    payload = {k: record.get(k) for k in keys}
    return json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


class HashChainAuditSink:
    """AudicSink 兼容的防篡改审计下沉（callable：emit(event_type, **fields)）。"""

    def __init__(self, clock: Any = None) -> None:
        self._records: list[dict] = []
        self._head = GENESIS
        self._clock = clock or time.time

    def __call__(self, event_type: str, **fields: Any) -> None:
        self.emit(event_type, **fields)

    def emit(self, event_type: str, **fields: Any) -> None:
        ts = fields.pop("timestamp", None) or self._iso_now()
        decision = fields.pop("decision", None) or event_type
        rec = {
            "timestamp": ts,
            "capability": fields.get("capability"),
            "principal": fields.get("principal"),
            "decision": decision,
            "reason": fields.get("reason")
                      or fields.get("denied_reason")
                      or fields.get("error"),
            "execution_id": fields.get("execution_id"),
            "previous_hash": self._head,
        }
        rec["event_hash"] = self._hash(rec)
        self._records.append(rec)
        self._head = rec["event_hash"]

    def _iso_now(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._clock()))

    @staticmethod
    def _hash(rec: dict) -> str:
        return "sha256:" + hashlib.sha256(
            _canonical(rec).encode("utf-8")).hexdigest()

    # --- chain 访问 / 校验 ---
    def records(self) -> list[dict]:
        return list(self._records)

    def head(self) -> str:
        return self._head

    def verify(self) -> tuple[bool, str]:
        """重放 chain，确认每个 link 完整。

        返回 (ok, detail)。可检测删除 / 修改 / 重排：每条 event_hash 依赖
        (previous_hash + 规范化负载)，且每条记录的 previous_hash 需与真实前驱一致。
        """
        prev = GENESIS
        for i, rec in enumerate(self._records):
            if rec.get("previous_hash") != prev:
                return False, f"link broken at index {i}: previous_hash mismatch"
            expected = self._hash(rec)
            if rec.get("event_hash") != expected:
                return False, f"tamper detected at index {i}: event_hash mismatch"
            prev = rec["event_hash"]
        return True, f"chain intact ({len(self._records)} records)"
