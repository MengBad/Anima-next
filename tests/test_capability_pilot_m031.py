"""M0.3.1 + M0.4 Capability Pilot —— 真实 Tool 上的 Capability 边界验证。

Pilot 范围（严格，随治理扩展）：M0.3.1 标注了 ``web_search → search.web``；
M0.4 Phase 1.1 新增 ``read_file → filesystem.read``。本测试文件把 pilot 的三条
验收线钉死：

1. Provider 是否完全隐藏（capability 模式下 LLM 看不到 ``web_search``）；
2. Resolver 是否正确转发（``search.web`` → 真实 Tool handler）；
3. Legacy path 是否零影响（默认模式逐字节不变）。

同时把「已裁定的迁移期约束」（混合模式仍暴露未迁移 Tool）写成显式断言，
使其成为**可见的既定行为**而非隐性意外。
"""
from __future__ import annotations

import json

import pytest

from anima.capability import CapabilityContract, PermissionScopes
from anima.contracts.capability import Stability
from anima.contracts.errors import PermissionDeniedError
from anima.tools import LEVEL_OWNER, LEVEL_READONLY, LEVEL_USER, Tool, ToolRegistry, create_default_registry
from anima.tools.builtin import (
    PILOT_CAPABILITY_ID,
    PILOT_CAPABILITY_ID_4,
    declare_pilot_capability_contracts,
    declare_code_execute_contract,
)

#: 不允许出现在 capability schema 中的实现层词汇
PROVIDER_TOKENS = ["web_search", "bing", "wttr", "tavily", "searxng"]


@pytest.fixture()
def registry() -> ToolRegistry:
    return create_default_registry()


# --------------------------------------------------------------------------
# 1. 索引与契约
# --------------------------------------------------------------------------

def test_pilot_capability_is_indexed(registry):
    """默认注册表中，pilot 能力已进入索引且只挂 1 个 Provider。"""
    index = registry.capability_index
    assert index.has(PILOT_CAPABILITY_ID)
    providers = index.providers_for(PILOT_CAPABILITY_ID)
    assert [p.tool_name for p in providers] == ["web_search"]


def test_pilot_is_the_only_declared_capability(registry):
    """Pilot 纪律护栏：内置工具集中标注的能力集合必须与治理裁定完全一致。

    最初 M0.3.1 时集合为 ``["search.web"]``（恰好 1 个）。M0.4 Phase 1.1 新增了
    ``filesystem.read``，M0.4.2 复用 write_file 新增 ``filesystem.write``，集合变为三个。
    M0.4.4 Capability ID Approval（APPROVED FOR ID RESERVATION）新增 ``code.execute``
    作为**仅预留身份**的能力（存在但无 Provider / 不可执行），集合变为四个。
    这条断言**故意写死当前已裁定集合** —— 后续任何一次「顺手多标一个」或「漏标」都会
    在这里失败，强制走治理裁定而不是悄悄扩散。
    """
    from anima.tools.builtin import PILOT_CAPABILITY_ID_2, PILOT_CAPABILITY_ID_3
    assert registry.capability_index.capabilities() == [
        PILOT_CAPABILITY_ID, PILOT_CAPABILITY_ID_2, PILOT_CAPABILITY_ID_3, PILOT_CAPABILITY_ID_4
    ]


def test_pilot_contract_is_explicit_not_derived(registry):
    """显式 Contract 生效：domain / permission / stability 均来自声明。"""
    contract = registry.capability_index.contract_for(PILOT_CAPABILITY_ID)
    assert contract is not None
    assert contract.id == "search.web"
    assert contract.domain == "search"
    assert contract.version == "1.0.0"
    assert contract.major == 1
    assert contract.stability is Stability.EXPERIMENTAL
    # permission 非空 —— 这正是显式契约相对派生契约的关键差别
    assert contract.permission.network is True
    assert tuple(contract.permission.granted()) == ("network",)


def test_pilot_contract_description_is_capability_level(registry):
    """能力层描述不透传 Tool 描述，且不含任何实现层词汇。"""
    contract = registry.capability_index.contract_for(PILOT_CAPABILITY_ID)
    tool_description = registry.tools["web_search"].description
    assert contract.description != tool_description
    lowered = contract.description.lower()
    for token in PROVIDER_TOKENS:
        assert token not in lowered, f"能力描述泄漏实现词汇: {token}"


# --------------------------------------------------------------------------
# 2. Legacy path 零影响
# --------------------------------------------------------------------------

def test_legacy_schema_is_byte_identical(registry):
    """默认模式（capability_mode=False）输出与未标注前完全一致。"""
    for level in (LEVEL_READONLY, LEVEL_USER, LEVEL_OWNER):
        expected = [
            t.to_openai_schema()
            for t in registry.tools.values()
            if t.enabled and level >= t.min_level
        ]
        assert registry.get_schemas(level) == expected


def test_legacy_schema_still_exposes_pilot_tool_name(registry):
    """默认模式下 web_search 仍以 Tool name 暴露 —— legacy path 未被移除。"""
    names = [s["function"]["name"] for s in registry.get_schemas(LEVEL_USER)]
    assert "web_search" in names
    assert "search_web" not in names


def test_pilot_tool_abi_unchanged(registry):
    """标注是纯 additive：name / min_level / required / handler 未被改动。"""
    tool = registry.tools["web_search"]
    assert tool.name == "web_search"
    assert tool.min_level == LEVEL_USER
    assert tool.required == ["query"]
    assert callable(tool.handler)
    assert tool.capability_ids == ["search.web"]
    assert tuple(tool.granted_scopes.granted()) == ("network",)


@pytest.mark.asyncio
async def test_legacy_call_by_tool_name_still_works(registry):
    """旧路径 Agent → tool name → call() → handler 未受影响。"""
    hits = []

    async def stub(query: str = "", num_results: int = 5) -> str:
        hits.append(query)
        return f"legacy:{query}"

    registry.tools["web_search"].handler = stub
    out = await registry.call("web_search", {"query": "anima"}, LEVEL_USER)
    assert "legacy:anima" in out
    assert hits == ["anima"]


# --------------------------------------------------------------------------
# 3. Capability 模式：Provider 隐藏
# --------------------------------------------------------------------------

def test_capability_mode_hides_provider_name(registry):
    """capability 模式下 LLM 只看到能力 id，看不到 Provider（Tool）名。"""
    schemas = registry.get_schemas(LEVEL_USER, capability_mode=True)
    names = [s["function"]["name"] for s in schemas]
    assert "search_web" in names
    assert "web_search" not in names, "Provider 名不得作为 function name 暴露"


def test_capability_schema_entry_is_clean(registry):
    """pilot 能力条目自身序列化后不含任何实现层词汇。"""
    schemas = registry.get_schemas(LEVEL_USER, capability_mode=True)
    entry = next(s for s in schemas if s["function"]["name"] == "search_web")
    blob = json.dumps(entry, ensure_ascii=False).lower()
    for token in PROVIDER_TOKENS:
        assert token not in blob, f"capability schema 泄漏实现词汇: {token}"
    # 参数面从 Provider 透传，属能力签名的一部分，必须保留
    assert set(entry["function"]["parameters"]["properties"]) == {"query", "num_results"}
    assert entry["function"]["parameters"]["required"] == ["query"]


def test_capability_mode_keeps_unmigrated_tools_visible(registry):
    """已裁定的迁移期约束（Accepted Migration Constraint）——显式钉住。

    混合模式下未迁移 Tool 仍以 Tool name 暴露；这不是泄漏失败，
    是「不移除 legacy Tool path」纪律的直接后果，迁移窗口内成立。
    """
    names = [s["function"]["name"] for s in registry.get_schemas(LEVEL_USER, capability_mode=True)]
    assert "query_web_memory" in names   # 未迁移 → 仍是 Tool name
    assert "get_time" in names
    # 已迁移的那一个则完成了切换
    assert "search_web" in names and "web_search" not in names


# --------------------------------------------------------------------------
# 4. Resolver 转发
# --------------------------------------------------------------------------

def test_resolver_resolves_pilot_to_real_provider(registry):
    resolution = registry.capability_resolver.resolve(PILOT_CAPABILITY_ID, permission_level=LEVEL_USER)
    assert resolution.capability_id == "search.web"
    assert resolution.provider.tool_name == "web_search"
    assert resolution.contract.domain == "search"


def test_resolver_reverse_lookup_from_schema_name(registry):
    assert registry.capability_index.capability_for_schema_name("search_web") == "search.web"
    assert registry.capability_index.capability_for_schema_name("web_search") is None


@pytest.mark.asyncio
async def test_call_capability_routes_to_real_handler(registry):
    """capability 入口最终仍由 ToolRegistry.call() 执行真实 handler。"""
    seen = []

    async def stub(query: str = "", num_results: int = 5) -> str:
        seen.append((query, num_results))
        return f"resolved:{query}"

    registry.tools["web_search"].handler = stub
    out = await registry.call_capability("search.web", {"query": "anima"}, LEVEL_USER)
    assert "resolved:anima" in out
    # schema name 入口同样可达
    out2 = await registry.call_capability("search_web", {"query": "next"}, LEVEL_USER)
    assert "resolved:next" in out2
    assert [q for q, _ in seen] == ["anima", "next"]


# --------------------------------------------------------------------------
# 5. 显式契约让 scope 校验真实生效
# --------------------------------------------------------------------------

def test_declared_contract_makes_scope_gate_real():
    """未授予 network scope 的 Provider 无法满足 pilot 契约。

    这条证明显式 declare_contract 消除了派生契约 permission 为空时的 no-op。
    """
    reg = ToolRegistry()
    declare_pilot_capability_contracts(reg)
    reg.register(Tool(
        "offline_search", "本地检索", {"query": {"type": "string"}},
        lambda query="": query,
        capability_ids=[PILOT_CAPABILITY_ID],   # 声明能力但未授予 network
    ))
    with pytest.raises(PermissionDeniedError):
        reg.capability_resolver.resolve(PILOT_CAPABILITY_ID, permission_level=LEVEL_USER)


def test_declared_contract_accepts_provider_with_scope():
    reg = ToolRegistry()
    declare_pilot_capability_contracts(reg)
    reg.register(Tool(
        "online_search", "联网检索", {"query": {"type": "string"}},
        lambda query="": query,
        capability_ids=[PILOT_CAPABILITY_ID],
        granted_scopes={"network": True},
    ))
    resolution = reg.capability_resolver.resolve(PILOT_CAPABILITY_ID, permission_level=LEVEL_USER)
    assert resolution.provider.tool_name == "online_search"


def test_declare_pilot_contract_is_idempotent_and_tolerant():
    """重复声明幂等；对没有 capability_index 的替身 registry 静默跳过。"""
    reg = ToolRegistry()
    declare_pilot_capability_contracts(reg)
    declare_pilot_capability_contracts(reg)
    assert reg.capability_index.capabilities() == [PILOT_CAPABILITY_ID]

    class FakeRegistry:
        def register(self, tool):  # pragma: no cover - 仅作替身
            pass

    declare_pilot_capability_contracts(FakeRegistry())  # 不得抛异常


def test_pilot_contract_matches_declared_shape():
    """契约形状与 builtin 声明一致（防止两处漂移）。"""
    reg = ToolRegistry()
    declare_pilot_capability_contracts(reg)
    contract = reg.capability_index.contract_for(PILOT_CAPABILITY_ID)
    assert contract == CapabilityContract(
        id="search.web",
        version="1.0.0",
        domain="search",
        description=contract.description,
        stability=Stability.EXPERIMENTAL,
        permission=PermissionScopes(network=True),
    )


# --------------------------------------------------------------------------
# 6. M0.4.4 Capability ID Reservation —— code.execute（存在但不可执行）
# --------------------------------------------------------------------------

def test_code_execute_reservation_is_indexed_without_provider():
    """M0.4.4 Reservation：code.execute 身份已进入索引，但未挂载任何 Provider。

    契约存在（has == True）但 providers_for 为空 —— 该能力**不可执行**。
    这是 Reservation 阶段与 Implementation 阶段的硬边界。
    """
    reg = ToolRegistry()
    declare_code_execute_contract(reg)
    assert reg.capability_index.has(PILOT_CAPABILITY_ID_4)
    assert reg.capability_index.providers_for(PILOT_CAPABILITY_ID_4) == []


def test_code_execute_reservation_required_scope_is_exec_only():
    """Reservation 不扩展 scope：required_scopes 仅 [exec]，复用既有 RFC-0001 §7。"""
    reg = ToolRegistry()
    declare_code_execute_contract(reg)
    contract = reg.capability_index.contract_for(PILOT_CAPABILITY_ID_4)
    assert contract.permission == PermissionScopes(exec=True)
    assert contract.permission.network is False
    assert contract.permission.filesystem is False
    assert contract.permission.secrets is False


def test_code_execute_reservation_idempotent_and_tolerant():
    """重复预留幂等；对没有 capability_index 的替身 registry 静默跳过。"""
    reg = ToolRegistry()
    declare_code_execute_contract(reg)
    declare_code_execute_contract(reg)
    assert reg.capability_index.capabilities() == [PILOT_CAPABILITY_ID_4]

    class FakeRegistry:
        def register(self, tool):  # pragma: no cover - 仅作替身
            pass

    declare_code_execute_contract(FakeRegistry())  # 不得抛异常


def test_code_execute_reservation_does_not_activate_execution():
    """M0.4.6 D3 后：code.execute 已挂载唯一 Provider ``code_exec``，但默认注册表仍：

    - ``capability_mode`` 关闭 → capability 路由未激活（需独立 Pilot Activation gate）；
    - 不 retro-tag legacy 执行工具：run_python / run_shell 仍零 capability 标注；
    - 恰好一个 provider（``code_exec``），不引入第二个、不新建执行器。

    legacy 执行工具 ABI 不受影响仍可注册。
    （agent_exec 需 core，不在默认注册表，故只校验 run_python / run_shell。）
    """
    reg = create_default_registry()
    assert reg.capability_index.has(PILOT_CAPABILITY_ID_4)
    providers = [p.id for p in reg.capability_index.providers_for(PILOT_CAPABILITY_ID_4)]
    assert providers == ["code_exec"]
    assert reg.capability_mode is False
    for name in ("run_python", "run_shell"):
        assert reg.tools.get(name) is not None
        assert reg.tools[name].capability_ids == [], f"{name} 被 retro-tag！"
