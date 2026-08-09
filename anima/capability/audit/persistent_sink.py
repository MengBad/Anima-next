"""F8.7 —— PersistentAuditSink（写侧，实现 ``AuditSink`` Protocol）。

## 定位

``capability/governance/audit.py`` 的 ``AuditSink`` 是 ``(event) -> None`` Protocol
（F8.3，冻结）。本 sink 是它的持久化实现：事件落到 ``JsonlAuditStore``（JSONL
append-only + envelope schema + retention），同时是**纯注入**——外部构造实例挂到
``core.governance_audit_sink``（或测试里直接传给 registry），生产代码零改动
（用户 Q2 裁定）。

## fail-open（用户红线：持久化失败 ≠ capability execution failure）

``resolve() → emit(event) → sink best effort``：

- 每次写包 ``try/except``，失败 → ``write_failures += 1``，**绝不 raise**；
- resolver 侧 ``_emit_audit``（``runtime/capability_resolver.py:515-529``）本就
  fail-open（sink 抛异常 → 吞掉 + ``audit_failures += 1``），本 sink 内部再兜一层，
  构成**双通道**：``resolver.audit_failures`` = 调用侧吞掉的，
  ``sink.write_failures`` = 存储侧吞掉的——两者都可观测，业务永不阻断；
- 热路径无同步重扫描：rotation 是写前 O(1) 大小检查；``cleanup()`` 是显式方法，
  由运维定时调用，绝不在 ``__call__`` 里触发。
"""
from __future__ import annotations

from typing import Any

from anima.capability.governance.audit import ResolutionEvent
from anima.capability.audit.storage import (
    DEFAULT_MAX_BYTES_PER_FILE,
    DEFAULT_RETENTION_DAYS,
    JsonlAuditStore,
)


class PersistentAuditSink:
    """满足 ``AuditSink`` Protocol 的持久化审计出口。

    :param path: 主 JSONL 文件路径（父目录自动创建）。
    :param max_bytes_per_file: 轮转阈值（默认 100MB）；0 关闭轮转。
    :param retention_days: 保留天数（默认 30）；``<=0`` 关闭自动清理
        （``cleanup()`` 变为无操作）。
    """

    def __init__(
        self,
        path: str,
        *,
        max_bytes_per_file: int = DEFAULT_MAX_BYTES_PER_FILE,
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> None:
        self._store = JsonlAuditStore(
            path,
            max_bytes_per_file=max_bytes_per_file,
            retention_days=retention_days,
        )
        #: 存储侧吞掉的写失败次数（fail-open 双通道之一，供监控打点）。
        self.write_failures = 0

    @property
    def store(self) -> JsonlAuditStore:
        """底层存储（只读查询面经 ``AuditQuery`` 消费）。"""
        return self._store

    def __call__(self, event: ResolutionEvent) -> None:
        """写一条事件。**任何失败都不 raise**，只递增 ``write_failures``。"""
        try:
            self._store.append(event)
        except Exception:  # noqa: BLE001 - 观测失灵不得让能力不可用（fail-open）
            self.write_failures += 1

    def cleanup(self) -> int:
        """显式保留期清理（删除超期分片）。不进热路径，由运维定时调用。"""
        return self._store.cleanup()
