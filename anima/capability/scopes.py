"""M0.3 —— Capability 权限域（RFC-0001 §7）与沙箱适配层。

本模块**只做映射，不新建权限 Authority**：

- ``PermissionScopes`` 表达 RFC-0001 §7 的五个 scope；
- Contract 的 ``permission`` = 该能力**要求**的 scope；
- Provider 的 ``granted_scopes`` = 该实现**被授予**的 scope；
- 规则（RFC-0001 §7）：granted ⊇ required，否则该 Provider 对该能力不可用。

``map_scopes_to_sandbox()`` 把 scope 收敛到既有 ``SandboxConfig``：
**只会收窄，永不放宽** ``sandbox_tier_for(permission_level)`` 给出的能力上界。

明确不做（M0.3 纪律）：
- 不新增 Permission Authority；
- 不修改 PermissionLevel 体系（LEVEL_BANNED..LEVEL_OWNER）与 PermissionStore；
- 不进入 Life O1 —— 本层只描述 Infrastructure 侧的执行权限，不持有任何 Life state。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

# RFC-0001 §7 权限域（最小集合，可扩展）
SCOPE_NAMES: tuple[str, ...] = (
    "network",
    "filesystem",
    "exec",
    "secrets",
    "user_interaction",
)


@dataclass(frozen=True)
class PermissionScopes:
    """RFC-0001 §7 权限域集合（immutable）。

    同一结构同时用于 Contract.permission（required）与 Provider.granted_scopes
    （granted）；两者的关系由 :meth:`satisfies` 判定。
    """

    network: bool = False
    filesystem: bool = False
    exec: bool = False
    secrets: bool = False
    user_interaction: bool = False

    # ---------------- 构造 ----------------

    @classmethod
    def from_mapping(cls, data: "Mapping[str, Any] | PermissionScopes | None") -> "PermissionScopes":
        """从 dict / PermissionScopes / None 归一化；未知键忽略，缺省视为 False。"""
        if data is None:
            return NO_SCOPES
        if isinstance(data, PermissionScopes):
            return data
        return cls(**{k: bool(data.get(k, False)) for k in SCOPE_NAMES})

    # ---------------- 查询 ----------------

    def as_dict(self) -> dict[str, bool]:
        return {k: bool(getattr(self, k)) for k in SCOPE_NAMES}

    def granted(self) -> tuple[str, ...]:
        return tuple(k for k in SCOPE_NAMES if getattr(self, k))

    def satisfies(self, required: "PermissionScopes") -> bool:
        """granted ⊇ required（RFC-0001 §7 超集规则）。"""
        return not self.missing(required)

    def missing(self, required: "PermissionScopes") -> tuple[str, ...]:
        """返回 required 中未被本集合覆盖的 scope 名（用于 diagnostics）。"""
        return tuple(k for k in SCOPE_NAMES if getattr(required, k) and not getattr(self, k))


NO_SCOPES = PermissionScopes()


# ---------------------------------------------------------------------------
# Sandbox 适配（Phase 6.5：adapter only，不做权限重构）
# ---------------------------------------------------------------------------

def map_scopes_to_sandbox(scopes: PermissionScopes, permission_level: int = 2):
    """把 Capability scope 映射为既有 ``SandboxConfig``。

    语义：**与既有分级沙箱取交集**——scope 只能收窄 tier，不能提权。

    - ``network``   → ``SandboxConfig.network = tier.network AND scopes.network``
    - ``filesystem``→ ``writable_dirs = tier.writable_dirs if scopes.filesystem else []``
    - ``exec``      → 不落 SandboxConfig 字段（沙箱本身即执行边界），
      由 :func:`exec_permitted` 单独判定，交给调用方在执行前 gate。
    - ``secrets`` / ``user_interaction`` → 不属沙箱进程隔离范畴，M0.3 不映射。

    延迟 import ``anima.tools.sandbox``：避免 ``anima.tools`` 包级循环依赖。
    """
    from anima.tools.sandbox import SandboxConfig, sandbox_tier_for  # lazy

    tier = sandbox_tier_for(permission_level)
    return SandboxConfig(
        memory_mb=tier.memory_mb,
        timeout=tier.timeout,
        network=bool(tier.network and scopes.network),
        writable_dirs=list(tier.writable_dirs) if scopes.filesystem else [],
        readonly_dirs=[],
    )


def exec_permitted(scopes: PermissionScopes) -> bool:
    """该 scope 集合是否允许执行进程/代码（RFC-0001 §7 ``exec``）。"""
    return bool(scopes.exec)
