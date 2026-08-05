"""M0.3 Phase 6.3 —— Capability Resolver（最小骨架）。

只实现 ``resolve(capability_id)``：

    capability_id → CapabilityResolution(capability_id, provider, contract)

**刻意不实现**（属 RFC-0001 §6 / §6.1 后续阶段，M0.3 不提前扩张）：
- Scorer（Success/Health/Latency/Cost 加权排序）
- health ranking / 健康探测
- fallback 降级链与 retry 执行
- 自动选型

当前选择规则：候选集内取**第一个**（= 注册顺序），确定性、可预测。

Resolver 不执行工具：执行权仍在 ``ToolRegistry.call()``。
"""
from __future__ import annotations

import logging

from anima.contracts.errors import (
    CapabilityNotFoundError,
    NoProviderError,
    PermissionDeniedError,
)

from .contract import CapabilityContract
from .index import CapabilityIndex, ProviderRef

logger = logging.getLogger("anima.capability.resolver")


class CapabilityResolution:
    """一次解析结果（immutable）。"""

    __slots__ = ("capability_id", "provider", "contract")

    def __init__(self, capability_id: str, provider: ProviderRef, contract: CapabilityContract):
        object.__setattr__(self, "capability_id", capability_id)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "contract", contract)

    def __setattr__(self, name: str, value: object) -> None:  # immutable
        raise AttributeError("CapabilityResolution 不可变")

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"CapabilityResolution(capability_id={self.capability_id!r}, "
            f"provider={self.provider.id!r})"
        )


class CapabilityResolver:
    """把 capability id 解析为可用 Provider + Contract。"""

    def __init__(self, index: CapabilityIndex):
        self.index = index

    def resolve(self, capability_id: str, permission_level: int | None = None) -> CapabilityResolution:
        """解析能力。

        错误语义（RFC-0001 §8）：
        - capability id 未知 → ``CapabilityNotFoundError``
        - 能力已知但无任何可用 Provider → ``NoProviderError``
        - 有 Provider 但权限不达标（scope 非超集 / 等级不足）→ ``PermissionDeniedError``
        """
        cap_id = str(capability_id)
        contract = self.index.contract_for(cap_id)
        if contract is None:
            raise CapabilityNotFoundError(f"未知 capability: {cap_id}")

        providers = [p for p in self.index.providers_for(cap_id) if p.enabled]
        if not providers:
            raise NoProviderError(f"capability 无可用 Provider: {cap_id}")

        # Candidate 门槛（RFC-0001 §6 ①）：权限达标才进入候选；不参与排序。
        candidates: list[ProviderRef] = []
        denied: list[str] = []
        for provider in providers:
            if permission_level is not None and permission_level < provider.min_level:
                denied.append(f"{provider.id}: level<{provider.min_level}")
                continue
            missing = provider.granted_scopes.missing(contract.permission)
            if missing:
                denied.append(f"{provider.id}: missing_scopes={','.join(missing)}")
                continue
            candidates.append(provider)

        if not candidates:
            # diagnostics 只进日志，不回灌 Agent 上下文（RFC-0001 §8）
            logger.debug("capability %s 权限不达标: %s", cap_id, "; ".join(denied))
            raise PermissionDeniedError(f"capability 权限不达标: {cap_id}")

        # M0.3：不排序，取注册序首个（Scorer 见 RFC-0001 §6.1，后续阶段）
        return CapabilityResolution(cap_id, candidates[0], contract)


__all__ = ["CapabilityResolver", "CapabilityResolution"]
