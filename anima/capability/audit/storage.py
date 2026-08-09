"""F8.7 —— JSONL audit store（append-only + envelope schema + retention）。

## envelope schema（R1，用户 Q3 裁定）

``ResolutionEvent`` 是冻结结构（``capability/governance/audit.py``，8 字段，不可改）。
schema 演进是**存储侧**责任，因此每一行写为信封：

    {"schema_version": 1, "recorded_at": <float>, "event": {<ResolutionEvent 逐字段>}}

读取器对**未知版本**行：尽力解析（字段结构相同则重建成功），并递增
``unknown_versions`` 计数标注——不抛、不丢（G2）。

## retention（R2，用户 Q4 裁定）

- ``max_bytes_per_file``（默认 100MB）：写前 O(1) 大小检查，超限即轮转
  （主文件 rename 成 ``<name>.<seq>``），**不阻塞热路径**；
- ``retention_days``（默认 30）：**显式** ``cleanup()`` 方法删除超期分片——
  绝不在 emit 路径里同步重扫描；``retention_days=0`` = 关闭清理；
- cleanup **只删本 store 自己的分片**（``<primary-name>.*``），主文件永不删，
  绝不碰目录里其它文件。

## fail-open 分层

本模块忠实执行并**上抛真实 IO 错误**；fail-open 由调用方
（``persistent_sink.PersistentAuditSink``）负责计数吞掉。职责单一：
store 说真话，sink 做缓冲。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from anima.capability.governance.audit import (
    CandidateRecord,
    ResolutionEvent,
    ResolutionOutcome,
)

#: 当前信封 schema 版本。未来字段演进只 bump 此值并在读取器做迁移。
CURRENT_SCHEMA_VERSION = 1

#: 默认轮转阈值（100MB）与默认保留天数（30）——用户 Q4 裁定。
DEFAULT_MAX_BYTES_PER_FILE = 100 * 1024 * 1024
DEFAULT_RETENTION_DAYS = 30


# ---------------------------------------------------------------------------
# 序列化（信封）
# ---------------------------------------------------------------------------
def _serialize_event(event: ResolutionEvent) -> dict[str, Any]:
    """把 frozen ResolutionEvent 拍平成 JSON 可序列化 dict。"""
    return {
        "timestamp": event.timestamp,
        "capability_id": event.capability_id,
        "execution_key": list(event.execution_key),
        "requested_version": event.requested_version,
        "candidates": [
            {
                "provider": c.provider,
                "score": c.score,
                "rejected_reason": c.rejected_reason,
            }
            for c in event.candidates
        ],
        "selected_provider": event.selected_provider,
        "metadata_snapshot": dict(event.metadata_snapshot),
        "outcome": event.outcome.value,
        "error": event.error,
    }


def _deserialize_event(row: dict[str, Any]) -> ResolutionEvent:
    """从信封行重建 ResolutionEvent（尽力解析：缺失字段用默认值兜底）。"""
    ev = row["event"]
    return ResolutionEvent(
        timestamp=ev["timestamp"],
        capability_id=ev["capability_id"],
        execution_key=tuple(ev["execution_key"]),
        requested_version=ev.get("requested_version"),
        candidates=tuple(
            CandidateRecord(c["provider"], c["score"], c["rejected_reason"])
            for c in ev["candidates"]
        ),
        selected_provider=ev.get("selected_provider"),
        metadata_snapshot=dict(ev.get("metadata_snapshot") or {}),
        outcome=ResolutionOutcome(ev["outcome"]),
        error=ev.get("error"),
    )


class JsonlAuditStore:
    """append-only JSONL 审计存储。

    - ``primary_path``：主文件（活跃写入，永不删除）；
    - ``<primary>.<seq>``：轮转分片（按大小阈值产生，可被 ``cleanup()`` 清理）。
    """

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
        #: JSON 层 / 结构层无法解析的行数（坏行计数，跳过不抛）。
        self.parse_failures = 0

    @property
    def primary_path(self) -> Path:
        return self._path

    # -- 写入 ---------------------------------------------------------------
    def append(self, event: ResolutionEvent) -> None:
        """追加一行信封。超大小阈值先轮转（O(1) rename），再写。

        本方法**不吞异常**——IO 错误如实上抛，由 sink 层做 fail-open。
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise
        if self._should_rotate():
            self._rotate()
        row = {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "recorded_at": time.time(),
            "event": _serialize_event(event),
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
        """把主文件 rename 成第一个空闲的 ``<name>.<seq>``（O(1)，不丢数据）。"""
        seq = 1
        while True:
            target = self._path.with_name(f"{self._path.name}.{seq}")
            if not target.exists():
                self._path.rename(target)
                return
            seq += 1

    # -- 读取 ---------------------------------------------------------------
    def _all_files(self) -> list[Path]:
        """主文件 + 分片，按写入顺序排序（主文件在前，分片按数字序）。"""
        primary = [self._path] if self._path.exists() else []

        def _seq(p: Path) -> int:
            suffix = p.name[len(self._path.name) + 1:]  # 去掉 "<name>." 前缀
            return int(suffix) if suffix.isdigit() else -1

        rotated = sorted(
            self._path.parent.glob(f"{self._path.name}.*"),
            key=_seq,
        )
        return primary + rotated

    def read_rows(self) -> list[dict[str, Any]]:
        """读全部原始信封行。坏 JSON 计数跳过；未知版本计数但保留返回。"""
        rows: list[dict[str, Any]] = []
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
                rows.append(row)
        return rows

    def read_events(self) -> list[ResolutionEvent]:
        """读全部事件（信封反序列化）。结构无法解析的行计数跳过，不抛。"""
        events: list[ResolutionEvent] = []
        for row in self.read_rows():
            try:
                events.append(_deserialize_event(row))
            except Exception:  # noqa: BLE001 - 坏行不能拖垮整次读取
                self.parse_failures += 1
        return events

    # -- 保留期 -------------------------------------------------------------
    def cleanup(self) -> int:
        """删除超过 ``retention_days`` 的分片文件（显式调用，绝不进热路径）。

        - ``retention_days <= 0`` → 不清理（返回 0）；
        - 只删 ``<primary>.<seq>`` 分片，主文件永不删；
        - 只删本 store 命名前缀的文件，不碰目录里其它文件。
        """
        if not self.retention_days or self.retention_days <= 0:
            return 0
        cutoff = time.time() - self.retention_days * 86400
        removed = 0
        for path in self._all_files():
            if path == self._path:
                continue  # 活跃主文件永不删
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed
