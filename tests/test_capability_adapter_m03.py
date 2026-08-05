"""M0.3 Capability Adapter 测试（RFC-0001 Phase 6）。

覆盖五个验收点：
- Tool 无 capability → legacy 路径完全不变
- capability → provider 正常 resolve
- unknown capability → CapabilityNotFoundError
- capability 模式下 Provider 名对 LLM 不可见
- scope → sandbox 映射（只收窄，不提权）

纪律回归：默认行为 100% 不变（capability_mode 默认关闭）。
"""

import pytest

from anima.capability import (
    CapabilityContract,
    PermissionScopes,
    exec_permitted,
    map_scopes_to_sandbox,
)
from anima.contracts.errors import (
    CapabilityNotFoundError,
    NoProviderError,
    PermissionDeniedError,
)
from anima.tools import LEVEL_ADMIN, LEVEL_OWNER, LEVEL_USER, Tool, ToolRegistry


def _tool(name, **kw):
    return Tool(
        name=name,
        description=kw.pop("description", f"{name} 描述"),
        parameters=kw.pop("parameters", {"q": {"type": "string", "description": "输入"}}),
        handler=kw.pop("handler", lambda q="": f"{name}:{q}"),
        **kw,
    )


# ---------------------------------------------------------------------------
# A. Capability Metadata（Phase 6.1）
# ---------------------------------------------------------------------------

def test_tool_without_capability_keeps_legacy():
    """未声明 capability 的 Tool：ABI、schema、索引三处都保持 legacy 语义。"""
    reg = ToolRegistry()
    legacy = _tool("legacy_only")
    reg.register(legacy)

    # 1) ABI additive：旧构造签名不变，新字段为空
    assert legacy.capability_ids == []
    assert legacy.granted_scopes == PermissionScopes()

    # 2) schema 与改造前一致（函数名仍是 tool name）
    schemas = reg.get_schemas(LEVEL_USER)
    assert [s["function"]["name"] for s in schemas] == ["legacy_only"]
    assert schemas[0] == legacy.to_openai_schema()

    # 3) 不进入 capability 索引
    assert reg.capability_index.capabilities() == []


def test_capability_metadata_is_additive():
    """声明 capability 不改变 name / handler / schema 契约。"""
    reg = ToolRegistry()
    tool = _tool("web_search", capability_ids=["search.web"], granted_scopes={"network": True})
    reg.register(tool)

    assert tool.name == "web_search"                       # name 未被 capability 替代
    assert tool.capability_ids == ["search.web"]
    assert tool.granted_scopes.network is True
    # legacy schema 仍以 tool name 暴露，handler contract 不变
    assert reg.get_schemas(LEVEL_USER)[0]["function"]["name"] == "web_search"


def test_existing_tool_registration_unaffected():
    """既有内置工具注册全部通过；M0.3.1 Pilot 后仅 1 个工具带 capability，其余 legacy 完全不变。

    注意：本条随 M0.3.1 Capability Pilot 调整。M0.3 时 Adapter 是空索引，
    故断言「全部不带 capability」。Pilot 在 web_search 上标注了 search.web 这一个能力，
    系有意、受限（1 个工具）的变更，不是回归。其余工具的 ABI / schema / 索引全部保持 legacy。
    """
    from anima.tools import create_default_registry

    reg = create_default_registry(core=None)
    assert len(reg.tools) > 0

    # M0.3.1：恰好 1 个工具标注了 capability，且只能是 pilot 的 search.web。
    annotated = {t.name: t.capability_ids for t in reg.tools.values() if t.capability_ids}
    assert annotated == {"web_search": ["search.web"]}
    # 所有「其他」工具保持零标注（legacy ABI 不变）
    assert all(t.capability_ids == [] for t in reg.tools.values() if t.name != "web_search")
    # 索引只有 pilot 这一个能力，不含任何 Provider / Tool 名
    assert reg.capability_index.capabilities() == ["search.web"]

    # 默认 flag 关闭 → schema 输出与 legacy 完全一致
    assert reg.capability_mode is False
    assert reg.get_schemas(LEVEL_USER) == [
        t.to_openai_schema() for t in reg.tools.values()
        if t.enabled and LEVEL_USER >= t.min_level
    ]


# ---------------------------------------------------------------------------
# B. Capability Index（Phase 6.2）
# ---------------------------------------------------------------------------

def test_capability_index_maps_capability_to_providers():
    reg = ToolRegistry()
    reg.register(_tool("tavily", capability_ids=["search.web"]))
    reg.register(_tool("searxng", capability_ids=["search.web"]))
    reg.register(_tool("legacy_only"))

    providers = reg.capability_index.providers_for("search.web")
    assert [p.id for p in providers] == ["tavily", "searxng"]   # 注册顺序即优先级
    assert reg.capability_index.capabilities() == ["search.web"]


def test_unregister_removes_provider_from_index():
    reg = ToolRegistry()
    reg.register(_tool("tavily", capability_ids=["search.web"]))
    reg.register(_tool("searxng", capability_ids=["search.web"]))

    reg.unregister("tavily")
    assert [p.id for p in reg.capability_index.providers_for("search.web")] == ["searxng"]

    reg.unregister("searxng")
    assert reg.capability_index.capabilities() == []            # 派生契约随最后 Provider 消失


def test_registry_call_signature_unchanged():
    """索引不介入执行：ToolRegistry.call(name, args) 仍是唯一执行入口。"""
    import inspect

    sig = inspect.signature(ToolRegistry.call)
    assert list(sig.parameters) == ["self", "name", "args", "permission_level"]


# ---------------------------------------------------------------------------
# C. Resolver Skeleton（Phase 6.3）
# ---------------------------------------------------------------------------

def test_capability_resolves_provider():
    reg = ToolRegistry()
    reg.register(_tool("tavily", capability_ids=["search.web"]))
    reg.register(_tool("searxng", capability_ids=["search.web"]))

    res = reg.capability_resolver.resolve("search.web")
    assert res.capability_id == "search.web"
    assert res.provider.id == "tavily"                # 无 scoring：取注册序首个
    assert res.provider.tool_name == "tavily"
    assert res.contract.id == "search.web"
    assert res.contract.domain == "search"
    with pytest.raises(AttributeError):               # resolution 不可变
        res.provider = None


def test_unknown_capability_error():
    reg = ToolRegistry()
    reg.register(_tool("legacy_only"))
    with pytest.raises(CapabilityNotFoundError):
        reg.capability_resolver.resolve("browser.page.extract")


def test_disabled_provider_yields_no_provider():
    reg = ToolRegistry()
    tool = _tool("tavily", capability_ids=["search.web"])
    reg.register(tool)
    tool.enabled = False                              # 插件运行时停用：索引实时可见
    with pytest.raises(NoProviderError):
        reg.capability_resolver.resolve("search.web")


def test_permission_denied_when_scopes_insufficient():
    """Contract 要求 network，Provider 未被授予 → PermissionDenied（RFC-0001 §7 超集规则）。"""
    reg = ToolRegistry()
    reg.register(_tool("offline_search", capability_ids=["search.web"]))
    reg.capability_index.declare_contract(
        CapabilityContract(id="search.web", permission=PermissionScopes(network=True))
    )
    with pytest.raises(PermissionDeniedError):
        reg.capability_resolver.resolve("search.web")

    reg.register(_tool("tavily", capability_ids=["search.web"], granted_scopes={"network": True}))
    assert reg.capability_resolver.resolve("search.web").provider.id == "tavily"


async def test_call_capability_delegates_to_registry():
    """Resolver 只选型；执行仍走既有 call()。"""
    reg = ToolRegistry()
    reg.register(_tool("tavily", capability_ids=["search.web"]))
    assert await reg.call_capability("search.web", {"q": "hi"}) == "tavily:hi"
    assert await reg.call_capability("search_web", {"q": "hi"}) == "tavily:hi"   # schema name 反查


# ---------------------------------------------------------------------------
# D. Schema Migration（Phase 6.4）
# ---------------------------------------------------------------------------

def test_capability_mode_off_by_default():
    reg = ToolRegistry()
    reg.register(_tool("tavily", capability_ids=["search.web"]))
    assert reg.capability_mode is False
    assert reg.get_schemas(LEVEL_USER) == reg.get_schemas(LEVEL_USER, capability_mode=False)
    assert [s["function"]["name"] for s in reg.get_schemas(LEVEL_USER)] == ["tavily"]


def test_capability_schema_hidden_provider():
    """capability 模式：LLM 只看到 capability id，看不到任何 Provider 名。"""
    import json

    reg = ToolRegistry()
    reg.register(_tool("tavily", description="联网检索", capability_ids=["search.web"]))
    reg.register(_tool("searxng", description="联网检索", capability_ids=["search.web"]))
    reg.register(_tool("legacy_only", description="遗留工具"))

    schemas = reg.get_schemas(LEVEL_USER, capability_mode=True)
    names = [s["function"]["name"] for s in schemas]
    assert "search_web" in names                       # capability id（. → _）
    assert "tavily" not in names and "searxng" not in names
    assert "legacy_only" in names                      # 未标注能力的工具仍走 legacy（不移除）

    blob = json.dumps(schemas, ensure_ascii=False)
    assert "tavily" not in blob and "searxng" not in blob

    # 参数契约取自 Provider，但 Provider 身份不外泄
    cap_schema = next(s for s in schemas if s["function"]["name"] == "search_web")
    assert cap_schema["function"]["parameters"]["properties"]["q"]["type"] == "string"


def test_capability_schema_respects_permission_level():
    reg = ToolRegistry()
    reg.register(_tool("owner_only", capability_ids=["admin.op"], min_level=LEVEL_OWNER))
    assert reg.get_schemas(LEVEL_USER, capability_mode=True) == []
    assert [s["function"]["name"] for s in reg.get_schemas(LEVEL_OWNER, capability_mode=True)] == ["admin_op"]


def test_capability_mode_flag_on_instance():
    reg = ToolRegistry(capability_mode=True)
    reg.register(_tool("tavily", capability_ids=["search.web"]))
    assert [s["function"]["name"] for s in reg.get_schemas(LEVEL_USER)] == ["search_web"]
    assert [s["function"]["name"] for s in reg.get_schemas(LEVEL_USER, capability_mode=False)] == ["tavily"]


# ---------------------------------------------------------------------------
# E. Permission Adapter（Phase 6.5）
# ---------------------------------------------------------------------------

def test_scope_mapping():
    """scope → sandbox：与既有 tier 取交集，只收窄不提权。"""
    from anima.tools.sandbox import sandbox_tier_for

    scopes = PermissionScopes(network=True, filesystem=True, exec=True)

    # owner tier 本身放开网络 → scope 允许后生效
    owner = map_scopes_to_sandbox(scopes, LEVEL_OWNER)
    owner_tier = sandbox_tier_for(LEVEL_OWNER)
    assert owner.network is True
    assert owner.writable_dirs == list(owner_tier.writable_dirs)
    assert owner.timeout == owner_tier.timeout and owner.memory_mb == owner_tier.memory_mb

    # 普通用户 tier 无网络 → 即使 scope 要求 network 也不得提权
    user = map_scopes_to_sandbox(scopes, LEVEL_USER)
    assert sandbox_tier_for(LEVEL_USER).network is False
    assert user.network is False

    # scope 收窄：不要 filesystem → 不给可写目录
    narrowed = map_scopes_to_sandbox(PermissionScopes(), LEVEL_OWNER)
    assert narrowed.network is False and narrowed.writable_dirs == []

    # exec 单独判定，不落 SandboxConfig 字段
    assert exec_permitted(scopes) is True
    assert exec_permitted(PermissionScopes()) is False


def test_scope_superset_rule():
    """RFC-0001 §7：granted ⊇ required。"""
    granted = PermissionScopes(network=True, filesystem=True)
    assert granted.satisfies(PermissionScopes(network=True))
    assert granted.satisfies(PermissionScopes())
    assert not granted.satisfies(PermissionScopes(network=True, secrets=True))
    assert granted.missing(PermissionScopes(exec=True, secrets=True)) == ("exec", "secrets")


def test_permission_level_system_untouched():
    """不新建 Permission Authority：等级体系与 can_call 语义不变。"""
    reg = ToolRegistry()
    reg.register(_tool("admin_tool", capability_ids=["admin.op"], min_level=LEVEL_ADMIN))
    assert reg.can_call("admin_tool", LEVEL_USER) is False
    assert reg.can_call("admin_tool", LEVEL_ADMIN) is True
    with pytest.raises(PermissionDeniedError):
        reg.capability_resolver.resolve("admin.op", permission_level=LEVEL_USER)
    assert reg.capability_resolver.resolve("admin.op", permission_level=LEVEL_ADMIN).provider.id == "admin_tool"
