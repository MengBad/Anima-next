"""Capability Layer —— RFC-0001 的运行时适配层（M0.3）。

定位：**给现有 Tool runtime 加一层 Capability Adapter，而不是替换它。**

- ``ToolRegistry`` 仍是唯一 execution owner；``call(name, args)`` 签名未变；
- Capability 元数据在 Tool ABI 上 additive 扩展（``capability_ids`` / ``granted_scopes``）；
- capability schema 由 feature flag ``capability_mode`` 控制，默认关闭。

本包不属于 Life 层，不持有任何 Life state，不新增 Authority。
"""
from __future__ import annotations

from .contract import CapabilityContract
from .index import CapabilityIndex, ProviderRef, schema_name_for
from .resolver import CapabilityResolution, CapabilityResolver
from .scopes import NO_SCOPES, SCOPE_NAMES, PermissionScopes, exec_permitted, map_scopes_to_sandbox

__all__ = [
    "CapabilityContract",
    "CapabilityIndex",
    "CapabilityResolution",
    "CapabilityResolver",
    "PermissionScopes",
    "ProviderRef",
    "NO_SCOPES",
    "SCOPE_NAMES",
    "exec_permitted",
    "map_scopes_to_sandbox",
    "schema_name_for",
]
