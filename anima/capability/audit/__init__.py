"""F8.7 —— Governance Audit Persistence（ResolutionEvent → Persistent Store）。

给 F8.3 的治理审计事件增加**跨进程生命周期**：事件不再只进 ``InMemoryAuditSink``
（重启即丢、无法追责、无法趋势分析），而是落成 append-only JSONL，可查询、可重放、
可保留。

## 边界（用户 F8.7 Implementation Authorization 裁定）

- 本包是**新子包**，加性新增；不修改 ``capability/governance/*`` 现存文件、
  ``runtime/capability_resolver.py``、execution ledger、Gateway routing、
  ``tools/__init__.py``。
- 对 governance 的依赖是**单向**的：``anima.capability.audit → governance.audit``
  （governance 不反向 import 本包）。
- **红线**：``resolve() → emit(event) → sink best effort``。持久化失败 ≠ capability
  execution failure——sink 写失败只计数、绝不 raise，也不阻塞 resolve 热路径
  （rotation 是写前 O(1) 大小检查；cleanup 是显式方法，绝不在 emit 里同步重扫描）。
"""
from __future__ import annotations

from .persistent_sink import PersistentAuditSink
from .query import AuditQuery
from .storage import CURRENT_SCHEMA_VERSION, JsonlAuditStore

__all__ = [
    "AuditQuery",
    "CURRENT_SCHEMA_VERSION",
    "JsonlAuditStore",
    "PersistentAuditSink",
]
