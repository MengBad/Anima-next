from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class AuditLogger:
    """逐日 JSONL 审计日志（坏行隔离），面向生产可观测性与可回溯性。

    R16 闭环5（audit continuity）：在原有 recent(limit) 基础上补充
    - query()：跨日文件按时间窗查询，支持按类型过滤；
    - prune()：按保留期清理过期审计文件，防止小盘 VPS 上日志无限膨胀。
    二者均为只读/维护操作，不修改历史记录内容。
    """

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

    def query(self, since: float | None = None, until: float | None = None,
              types: set[str] | None = None, limit: int = 500) -> list[dict[str, Any]]:
        """跨日文件按时间窗查询审计事件（坏行隔离，按 ts 升序返回）。

        - since/until：Unix 秒；缺省代表无界。窗口外的事件被过滤。
        - types：仅保留指定事件类型（子集匹配）。
        - limit：最多返回窗口内最新 limit 条（升序切片尾部）。
        """
        rows: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("*.jsonl")):
            try:
                with path.open("r", encoding="utf-8") as file:
                    for line in file:
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        ts = row.get("ts")
                        if since is not None and (ts is None or ts < since):
                            continue
                        if until is not None and (ts is not None and ts > until):
                            continue
                        if types is not None and row.get("type") not in types:
                            continue
                        rows.append(row)
            except OSError:
                continue
        rows.sort(key=lambda r: r.get("ts", 0))
        return rows[-max(0, limit):]

    def prune(self, retention_days: int = 90) -> int:
        """删除早于保留期的审计文件（整文件粒度）；返回删除数量。

        审计文件按天分片，删除即以「天」为最小单位清理，不会改写
        仍在保留期内的历史，保证可回溯窗口连续不断档。
        """
        cutoff = time.time() - max(1, int(retention_days)) * 86400
        removed = 0
        for path in self.root.glob("*.jsonl"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    continue
        return removed
