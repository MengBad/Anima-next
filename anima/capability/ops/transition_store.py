"""F8.8 —— ``LifecycleTransition`` 的 JSONL 持久化 store。

复用 F8.7 ``capability/audit`` 的 envelope / retention 模式（用户 Q5 裁定：独立
transition store，**不复用** resolution AuditSink——事件语义不同：resolution vs
transition），但独立实现，零触碰 F8.7 包。

## envelope schema

    {"schema_version": 1, "capability_id": "...", "recorded_at": <float>,
     "transition": {"source": "active" | null, "target": "quarantined",
                    "reason": "...", "at": <float>}}

``source`` 为 ``None`` 表示初始记录（与 ``LifecycleTransition`` 语义一致）。

## retention

- ``max_bytes_per_file``（默认 100MB）：写前 O(1) 大小检查，超限轮转
  （主文件 rename 成 ``<name>.<seq>``）；
- ``retention_days``（默认 30）：**显式** ``cleanup()`` 删除超期分片，绝不进
  写路径；``<=0`` 关闭清理；只删本 store 自己的分片，主文件永不删。

## fail-open

append 忠实执行并上抛真实 IO 错误；fail-open 由调用方
（``HealthObserver``）负责计数吞掉——store 说真话，observer 做缓冲。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from anima.capability.governance.lifecycle import (
    LifecycleState,
    LifecycleTransition,
)

#: 当前信封 schema 版本。
CURRENT_SCHEMA_VERSION = 1

#: 默认轮转阈值（100MB）与默认保留天数（30）。
DEFAULT_MAX_BYTES_PER_FILE = 100 * 1024 * 1024
DEFAULT_RETENTION_DAYS = 30


def _serialize(capability_id: str, transition: LifecycleTransition) -> dict[str, Any]:
    return {
        "capability_id": capability_id,
        "transition": {
            "source": transition.source.value if transition.source else None,
            "target": transition.target.value,
            "reason": transition.reason,
            "at": transition.at,
        },
    }


def _deserialize(row: dict[str, Any]) -> tuple[str, LifecycleTransition]:
    tr = row["transition"]
    return (
        row["capability_id"],
        LifecycleTransition(
            source=LifecycleState(tr["source"]) if tr["source"] else None,
            target=LifecycleState(tr["target"]),
            reason=tr["reason"],
            at=tr["at"],
        ),
    )


class TransitionStore:
    """``LifecycleTransition`` 的 append-only JSONL 存储。"""

    def __init__(
        self,
        path: str,
        *,
        max_bytes_per_file: int = DEFAULT_MAX_BYTES_PER_FILE,
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> None:
        self._path = Path(path)
        self.max_bytes_per_file = max_bytes_per_file
        self.retention_days = retention_days
        #: 读到未知 schema_version 的行数（标注，不丢行）。
        self.unknown_versions = 0
        #: JSON / 结构层无法解析的行数（坏行计数，跳过不抛）。
        self.parse_failures = 0

    @property
    def primary_path(self) -> Path:
        return self._path

    # -- 写入 ---------------------------------------------------------------
    def append(self, capability_id: str, transition: LifecycleTransition) -> None:
        """追加一行信封。超大小阈值先轮转（O(1) rename）。**不吞异常**。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._should_rotate():
            self._rotate()
        row = {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "recorded_at": time.time(),
            **_serialize(capability_id, transition),
        }
        with self._path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _should_rotate(self) -> bool:
        if not self.max_bytes_per_file:
            return False
        try:
            return self._path.exists() and self._path.stat().st_size >= self.max_bytes_per_file
        except OSError:
            return False

    def _rotate(self) -> None:
        seq = 1
        while True:
            target = self._path.with_name(f"{self._path.name}.{seq}")
            if not target.exists():
                self._path.rename(target)
                return
            seq += 1

    # -- 读取 ---------------------------------------------------------------
    def _all_files(self) -> list[Path]:
        primary = [self._path] if self._path.exists() else []

        def _seq(p: Path) -> int:
            suffix = p.name[len(self._path.name) + 1:]
            return int(suffix) if suffix.isdigit() else -1

        rotated = sorted(self._path.parent.glob(f"{self._path.name}.*"), key=_seq)
        return primary + rotated

    def query(
        self,
        *,
        capability_id: str | None = None,
        target: LifecycleState | None = None,
        source: LifecycleState | None = None,
        start: float | None = None,
        end: float | None = None,
    ) -> list[LifecycleTransition]:
        """按过滤键筛选 transition 记录（``start/end`` 基于 ``transition.at``）。"""
        records: list[LifecycleTransition] = []
        for path in self._all_files():
            try:
                with path.open("r", encoding="utf-8") as file:
                    lines = file.readlines()
            except OSError:
                self.parse_failures += 1
                continue
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    self.parse_failures += 1
                    continue
                if row.get("schema_version") != CURRENT_SCHEMA_VERSION:
                    self.unknown_versions += 1
                try:
                    cid, tr = _deserialize(row)
                except Exception:  # noqa: BLE001 - 坏行不能拖垮整次读取
                    self.parse_failures += 1
                    continue
                if capability_id is not None and cid != capability_id:
                    continue
                if target is not None and tr.target is not target:
                    continue
                if source is not None and tr.source is not source:
                    continue
                if start is not None and tr.at < start:
                    continue
                if end is not None and tr.at > end:
                    continue
                records.append(tr)
        return records

    # -- 保留期 -------------------------------------------------------------
    def cleanup(self) -> int:
        """删除超过 ``retention_days`` 的分片（显式调用；只删自有分片）。"""
        if not self.retention_days or self.retention_days <= 0:
            return 0
        cutoff = time.time() - self.retention_days * 86400
        removed = 0
        for path in self._all_files():
            if path == self._path:
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed
