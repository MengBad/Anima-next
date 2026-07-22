from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

class AuditLogger:
    def __init__(self, data_dir: str):
        self.root = Path(data_dir) / "audit"
        self.root.mkdir(parents=True, exist_ok=True)

    def emit(self, event_type: str, **fields: Any) -> None:
        path = self.root / (time.strftime("%Y-%m-%d") + ".jsonl")
        row = {"ts": time.time(), "type": event_type, **fields}
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        """读取最近审计事件；坏行隔离，供观测面板安全展示。"""
        rows: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("*.jsonl"), reverse=True):
            try:
                with path.open("r", encoding="utf-8") as file:
                    parsed = []
                    for line in file:
                        try:
                            parsed.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
                    rows.extend(reversed(parsed))
            except OSError:
                continue
            if len(rows) >= limit:
                break
        return rows[:max(0, limit)]
