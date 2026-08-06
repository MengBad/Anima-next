"""M0.3 Phase 6.4 —— Capability schema 生成（capability 模式专用）。

Legacy 模式下本模块**完全不参与**：``ToolRegistry.get_schemas()`` 默认
``capability_mode=False``，输出与改造前逐字节一致。

capability 模式下给 LLM 的清单只含 **capability id**（RFC-0001 §5.4：
「给 LLM 的能力清单由 Registry 推导，不含任何 Provider 名」）：

    Capability schema → Resolver → Provider

迁移期为「混合模式」：未标注 capability 的 legacy Tool 仍以 Tool schema 暴露，
保证 flag 打开时运行时不残废（纪律：不移除 legacy Tool path）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .index import CapabilityIndex

if TYPE_CHECKING:  # pragma: no cover
    from anima.tools import Tool


def capability_schema(index: CapabilityIndex, capability_id: str, permission_level: int) -> dict | None:
    """单个能力的 OpenAI function schema；无可用 Provider 时返回 None。"""
    contract = index.contract_for(capability_id)
    if contract is None:
        return None
    providers = [
        p for p in index.providers_for(capability_id)
        if p.enabled and permission_level >= p.min_level
    ]
    if not providers:
        return None
    primary = providers[0]
    tool = primary.tool
    parameters = dict(getattr(tool, "parameters", {}) or {})
    required = getattr(tool, "required", None)
    if required is None:
        required = list(parameters.keys())
    return {
        "type": "function",
        "function": {
            "name": index.schema_name_for(capability_id),
            "description": contract.description or primary.description,
            "parameters": {
                "type": "object",
                "properties": parameters,
                "required": list(required),
            },
        },
    }


def capability_schemas(index: CapabilityIndex, permission_level: int, *,
                       exclude: "set[str] | None" = None) -> list[dict]:
    """全部可见能力的 schema（不含任何 Provider 名）。

    ``exclude``（M0.4.7 D-A1 R6）：治理未放行的能力 id 集合，直接不产出 schema。
    本模块只执行「排除」这一机械动作，**不持有治理策略**——allowlist 判定属
    ``ToolRegistry``（唯一持 core / audit 的层），保持 schema 层为纯投影。
    """
    blocked = set(exclude or ())
    out: list[dict] = []
    for cap_id in index.capabilities():
        if cap_id in blocked:
            continue
        schema = capability_schema(index, cap_id, permission_level)
        if schema is not None:
            out.append(schema)
    return out


def legacy_only_schemas(tools: "list[Tool]", permission_level: int) -> list[dict]:
    """尚未标注 capability 的 Tool 仍走 Tool schema（迁移期混合模式）。"""
    return [
        t.to_openai_schema()
        for t in tools
        if t.enabled
        and permission_level >= t.min_level
        and not getattr(t, "capability_ids", None)
    ]


__all__ = ["capability_schema", "capability_schemas", "legacy_only_schemas"]
