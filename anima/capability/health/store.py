"""F8.10 —— ``HealthEvidence`` 的 JSONL 持久化 store（Q5 裁定：独立 store）。

复用 F8.7 ``capability/audit`` / F8.8 ``capability/ops`` 的 envelope / retention
模式，但独立实现、零触碰既有包——三种 store 语义各司其职，**不合并**：

| Store | 记录 |
|-|-|
| AuditStore（F8.7） | capability resolution |
| TransitionStore（F8.8） | lifecycle decision |
| **HealthEvidenceStore（本文件）** | **execution fact** |

## envelope schema

    {"schema_version": 1, "evidence": { ...HealthEvidence 字段... }}

``observed_at`` 是 query 时间窗的基准（同 TransitionStore 以 transition.at 为基准）。

## retention

- ``max_bytes_per_file``（默认 100MB）：写前 O(1) 大小检查，超限轮转
  （主文件 rename 成 ``<name>.<seq>``）；
- ``retention_days``（默认 30）：**显式** ``cleanup()`` 删除超期分片，绝不进
  写路径；``<=0`` 关闭清理；只删本 store 自己的分片，主文件永不删。

## fail-open

append 忠实执行并上抛真实 IO 错误；fail-open 由调用方（``EvidenceAdapter``）
负责计数吞掉——store 说真话，adapter 做缓冲。

本 store 实现 :class:`~anima.capability.health.adapter.HealthEvidenceSink`
（``append``），可直接作为 adapter 的 sink 注入（sink injection seam，Q2）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from anima.capability.health.model import HealthEvidence

#: 当前信封 schema 版本。
CURRENT_SCHEMA_VERSION = 1

#: 默认轮转阈值（100MB）与默认保留天数（30）。
DEFAULT_MAX_BYTES_PER_FILE = 100 * 1024 * 1024
DEFAULT_RETENTION_DAYS = 30


def _serialize(evidence: HealthEvidence) -> dict[str, Any]:
    return {
        "evidence_id": evidence.evidence_id,
        "capability_id": evidence.capability_id,
        "provider_id": evidence.provider_id,
        "execution_id": evidence.execution_id,
        "observed_at": evidence.observed_at,
        "selected": evidence.selected,
        "executed": evidence.executed,
        "success": evidence.success,
        "error_type": evidence.error_type,
        "duration_ms": evidence.duration_ms,
        "metadata": evidence.metadata,
    }


def _deserialize(row: dict[str, Any]) -> HealthEvidence:
    ev = row["evidence"]
    return HealthEvidence(
        evidence_id=ev["evidence_id"],
        capability_id=ev["capability_id"],
        provider_id=ev["provider_id"],
        execution_id=ev["execution_id"],
        observed_at=float(ev["observed_at"]),
        selected=bool(ev["selected"]),
        executed=bool(ev["executed"]),
        success=bool(ev["success"]),
        error_type=str(ev.get("error_type", "")),
        duration_ms=float(ev.get("duration_ms", 0.0)),
        metadata=dict(ev.get("metadata") or {}),
    )


class HealthEvidenceStore:
    """``HealthEvidence`` 的 append-only JSONL 存储（实现 HealthEvidenceSink）。"""

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

    # -- 写入（HealthEvidenceSink）-----------------------------------------
    def append(self, evidence: HealthEvidence) -> None:
        """追加一行信封。超大小阈值先轮转（O(1) rename）。**不吞异常**。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._should_rotate():
            self._rotate()
        row = {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "evidence": _serialize(evidence),
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
        provider_id: str | None = None,
        success: bool | None = None,
        selected: bool | None = None,
        start: float | None = None,
        end: float | None = None,
    ) -> list[HealthEvidence]:
        """按过滤键筛选 evidence 记录（``start/end`` 基于 ``observed_at``）。"""
        records: list[HealthEvidence] = []
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
                    ev = _deserialize(row)
                except Exception:  # noqa: BLE001 - 坏行不能拖垮整次读取
                    self.parse_failures += 1
                    continue
                if capability_id is not None and ev.capability_id != capability_id:
                    continue
                if provider_id is not None and ev.provider_id != provider_id:
                    continue
                if success is not None and ev.success is not success:
                    continue
                if selected is not None and ev.selected is not selected:
                    continue
                if start is not None and ev.observed_at < start:
                    continue
                if end is not None and ev.observed_at > end:
                    continue
                records.append(ev)
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
