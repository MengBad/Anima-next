"""Agent 操作审计 —— 统一接口与规范化 Phase 5。

每次 Agent 工具调用 / 步骤执行记录到 data/agent_audit.jsonl（追加写入，不改动其它数据结构）。
复用既有 AuditLogger 的坏行隔离思路，供 WebUI 安全回放。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class AgentAuditor:
    def __init__(self, data_dir: str):
        self.root = Path(data_dir)
        self.path = self.root / "agent_audit.jsonl"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def emit(self, event_type: str, **fields: Any) -> None:
        row = {"ts": time.time(), "type": event_type, **fields}
        try:
            with self.path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def recent(self, limit: int = 200) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not self.path.exists():
            return rows
        try:
            with self.path.open("r", encoding="utf-8") as file:
                for line in file:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return rows
        return rows[-limit:]


def get_auditor(core: Any) -> "AgentAuditor | None":
    """从 core 获取 AgentAuditor（装配时挂到 core.agent_auditor）。"""
    return getattr(core, "agent_auditor", None)
