"""M0.3 Phase 6.2 —— Capability Index（``capability_id → providers[]``）。

索引只是 **Registry 之上的一层查询视图**：

- ``ToolRegistry`` 仍然是唯一的 execution owner（``call(name, args)`` 不变）；
- ``CapabilityResolver`` 只消费本索引，不持有工具、不执行工具；
- Provider 在 M0.3 中就是**已注册的 Tool**（RFC-0001 §4「Provider = 具体实现」），
  索引不复制 Tool 状态，而是持引用实时读取 ``enabled`` / ``min_level``，避免快照走样。

明确不做：不做 health 探测、不做 scoring、不做降级链（RFC-0001 §6/§6.1 后续阶段）。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Iterable

from .contract import CapabilityContract, normalize_contract
from .scopes import NO_SCOPES, PermissionScopes

if TYPE_CHECKING:  # 仅类型提示，运行时不 import anima.tools（避免循环依赖）
    from anima.tools import Tool

logger = logging.getLogger("anima.capability.index")


def schema_name_for(capability_id: str) -> str:
    """capability id → LLM function name。

    OpenAI/Gemini function name 不接受 ``.``，故 ``search.web`` → ``search_web``。
    该编码**不包含任何 Provider 信息**，Agent 侧词汇仍是能力而非实现。
    """
    return str(capability_id).replace(".", "_")


class ProviderRef:
    """能力的一个实现引用（M0.3 中即一个已注册 Tool）。

    持 Tool 引用而非快照：插件运行时 enable/disable 立即对索引可见。
    """

    __slots__ = ("id", "tool")

    def __init__(self, tool: "Tool"):
        self.id = tool.name
        self.tool = tool

    @property
    def tool_name(self) -> str:
        return self.tool.name

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.tool, "enabled", True))

    @property
    def min_level(self) -> int:
        return int(getattr(self.tool, "min_level", 2))

    @property
    def description(self) -> str:
        return str(getattr(self.tool, "description", "") or "")

    @property
    def granted_scopes(self) -> PermissionScopes:
        return PermissionScopes.from_mapping(getattr(self.tool, "granted_scopes", None))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ProviderRef) and other.id == self.id

    def __hash__(self) -> int:
        return hash(self.id)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"ProviderRef(id={self.id!r})"


class CapabilityIndex:
    """``capability_id → [ProviderRef]`` 路由表（RFC-0001 §5.3 的运行时形态）。"""

    def __init__(self) -> None:
        self._providers: dict[str, list[ProviderRef]] = {}
        self._contracts: dict[str, CapabilityContract] = {}
        self._declared: set[str] = set()          # 显式声明的 contract（不随 provider 清空而丢弃）
        self._schema_names: dict[str, str] = {}   # schema_name -> capability_id

    # ---------------- 写入 ----------------

    def declare_contract(self, contract: CapabilityContract) -> None:
        """显式声明能力契约；优先级高于由 Tool 派生的契约。"""
        contract = normalize_contract(contract)
        self._contracts[contract.id] = contract
        self._declared.add(contract.id)
        self._bind_schema_name(contract.id)

    def index_tool(self, tool: "Tool") -> None:
        """把一个 Tool 的 capability 声明并入索引（幂等）。

        Tool 未声明 ``capability_ids`` 时是 no-op —— legacy Tool 完全不受影响。
        """
        cap_ids = getattr(tool, "capability_ids", None) or ()
        for cap_id in cap_ids:
            cap_id = str(cap_id)
            if not cap_id:
                continue
            bucket = self._providers.setdefault(cap_id, [])
            ref = ProviderRef(tool)
            # 同名 Tool 重注册 → 覆盖旧引用，保持注册顺序不变
            for i, existing in enumerate(bucket):
                if existing.id == ref.id:
                    bucket[i] = ref
                    break
            else:
                bucket.append(ref)
            if cap_id not in self._contracts:
                self._contracts[cap_id] = CapabilityContract.derive(
                    cap_id, description=str(getattr(tool, "description", "") or "")
                )
            elif cap_id not in self._declared:
                self._contracts[cap_id] = self._contracts[cap_id].with_description(
                    str(getattr(tool, "description", "") or "")
                )
            self._bind_schema_name(cap_id)

    def remove_tool(self, tool_name: str) -> None:
        """工具下线时同步剔除其 Provider 引用。"""
        for cap_id in list(self._providers.keys()):
            bucket = [p for p in self._providers[cap_id] if p.id != tool_name]
            if bucket:
                self._providers[cap_id] = bucket
                continue
            self._providers.pop(cap_id, None)
            if cap_id not in self._declared:
                # 派生契约随最后一个 Provider 一起消失，避免留下不可用的空能力
                self._contracts.pop(cap_id, None)
                self._schema_names.pop(schema_name_for(cap_id), None)

    def rebuild(self, tools: "Iterable[Tool]") -> None:
        """按当前工具集合整体重建 Provider 索引（保留已声明的 Contract）。"""
        self._providers.clear()
        for cap_id in [c for c in self._contracts if c not in self._declared]:
            self._contracts.pop(cap_id, None)
        self._schema_names = {
            schema_name_for(c): c for c in self._contracts
        }
        for tool in tools:
            self.index_tool(tool)

    # ---------------- 查询 ----------------

    def has(self, capability_id: str) -> bool:
        return str(capability_id) in self._contracts

    def capabilities(self) -> list[str]:
        """已知能力 id 列表（注册顺序）。"""
        return list(self._contracts.keys())

    def providers_for(self, capability_id: str) -> list[ProviderRef]:
        """该能力的 Provider 列表（注册顺序 = M0.3 的选择顺序）。"""
        return list(self._providers.get(str(capability_id), ()))

    def contract_for(self, capability_id: str) -> CapabilityContract | None:
        return self._contracts.get(str(capability_id))

    def schema_name_for(self, capability_id: str) -> str:
        return schema_name_for(capability_id)

    def capability_for_schema_name(self, schema_name: str) -> str | None:
        """LLM 侧 function name → capability id（capability 模式下的反查）。"""
        return self._schema_names.get(str(schema_name))

    # ---------------- 内部 ----------------

    def _bind_schema_name(self, capability_id: str) -> None:
        name = schema_name_for(capability_id)
        existing = self._schema_names.get(name)
        if existing and existing != capability_id:
            logger.warning(
                "capability schema name 冲突: %s 与 %s 均映射为 %s，保留先注册者",
                existing, capability_id, name,
            )
            return
        self._schema_names[name] = capability_id


__all__ = ["CapabilityIndex", "ProviderRef", "schema_name_for", "NO_SCOPES"]
