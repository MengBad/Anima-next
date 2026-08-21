"""P8.4 — Conversation Evidence Logging.

Records one evidence record per REAL conversation (admitted message + response):

    timestamp / group_id / user_id HASH / message_id / request_id /
    intent / response / execution_key / result / latency

Privacy (P8.0):
    * user id is stored ONLY as a salted digest — never the raw QQ id
    * unrelated chat is not saved (only admitted pilot conversations)
    * records are deletable (explicit cleanup / retention)

Design:
    * append-only JSONL under data_dir/logs/conversation_evidence.jsonl
    * one line per admitted conversation (message + response pair)
    * a record carries the execution facts (execution_key / result / latency)
      WITHOUT merging identity domains (qq hash stays separate from F5 key)

Scope limits (P8.0): no System B, no gateway, no capability activation, no
env bypass, no raw user id persistence.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

__all__ = ["ConversationEvidence", "ConversationEvidenceLog", "hash_user_id"]


def hash_user_id(user_id: str, salt: Optional[str] = None) -> str:
    """Salted one-way digest of a QQ user id (privacy: never stored raw).

    A per-record random salt is mixed in, so two records of the same user are
    NOT linkable by the stored hash — the hash is only for "this record's
    user", not for cross-record correlation (stronger privacy).
    """
    s = salt or secrets.token_hex(8)
    return hashlib.sha256(f"{s}:{user_id}".encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True, slots=True)
class ConversationEvidence:
    """One admitted real conversation, recorded for analysis/audit."""

    timestamp: float
    group_id: str
    user_id_hash: str            # salted digest — never the raw QQ id
    message_id: str
    request_id: str
    intent: str
    response: str
    execution_key: tuple
    result: str                  # SUCCESS / FAILED / DENIED / BLOCKED
    latency_ms: float
    platform: str = "qq"

    def to_json(self) -> str:
        payload = {
            "timestamp": self.timestamp,
            "group_id": self.group_id,
            "user_id_hash": self.user_id_hash,
            "message_id": self.message_id,
            "request_id": self.request_id,
            "intent": self.intent,
            "response": self.response,
            "execution_key": json.dumps(list(self.execution_key), ensure_ascii=False),
            "result": self.result,
            "latency_ms": round(self.latency_ms, 3),
            "platform": self.platform,
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


class ConversationEvidenceLog:
    """Append-only JSONL conversation evidence store."""

    def __init__(self, data_dir: str) -> None:
        self._path = Path(data_dir) / "logs" / "conversation_evidence.jsonl"
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # record() fails loudly if the dir is unwritable

    @property
    def path(self) -> Path:
        return self._path

    def record(self, evidence: ConversationEvidence) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(evidence.to_json() + "\n")
            fh.flush()

    def read(self) -> list[dict]:
        if not self._path.exists():
            return []
        out = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def clear(self) -> int:
        """Delete ALL conversation evidence (P8.4: records are deletable)."""
        if self._path.exists():
            n = len(self.read())
            self._path.unlink()
            return n
        return 0
