"""M0.4 Capability Expansion Phase 1.2 (M0.4.2) —— filesystem.write Pilot。

复用既有 ``write_file`` 作为 ``filesystem.write`` 的 Provider（不新增 Provider）。
把 Pilot 的三条验收线 + write 专属安全项钉死：

- capability resolve → write_file（Provider 复用，无新建）
- Provider 隐藏（filesystem_write 暴露，write_file 不暴露）
- Legacy path 零影响（默认模式 write_file 仍以 Tool name 暴露）
- write scope 校验（无 filesystem scope → 拒绝；有 → 允许）
- 路径 containment（``../`` 与绝对路径均落在 output/ 内，不逃逸）
- overwrite 语义保持（同文件名覆盖，KL-write-2）
- 显式契约生效（domain / version / stability / permission）
- 能力描述能力层（无 Provider / Token 泄漏）
- 混合模式未迁移 Tool 仍可见
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from anima.capability import CapabilityContract, PermissionScopes
from anima.contracts.capability import Stability
from anima.contracts.errors import PermissionDeniedError
from anima.tools import (
    LEVEL_USER,
    Tool,
    ToolRegistry,
    create_default_registry,
)
from anima.tools.builtin import (
    PILOT_CAPABILITY_ID,
    PILOT_CAPABILITY_ID_2,
    PILOT_CAPABILITY_ID_3,
    declare_filesystem_write_contract,
)
from anima.tools.builtin.file import write_file

#: 不应作为实现层词汇出现在 capability schema / 描述中的 Token
PROVIDER_TOKENS = ["write_file", "read_file", "bing", "wttr"]


@pytest.fixture()
def registry() -> ToolRegistry:
    return create_default_registry()


# --------------------------------------------------------------------------
# 1. 索引与 Provider 复用（不新建 Provider）
# --------------------------------------------------------------------------

def test_filesystem_write_resolves_to_write_file_provider(registry):
    """filesystem.write 必须复用既有 write_file，不得新建 Provider。"""
    providers = registry.capability_index.providers_for(PILOT_CAPABILITY_ID_3)
    assert [p.tool_name for p in providers] == ["write_file"]
    assert registry.tools["write_file"].capability_ids == [PILOT_CAPABILITY_ID_3]


# --------------------------------------------------------------------------
# 2. Provider 隐藏（验收线 1）
# --------------------------------------------------------------------------

def test_capability_mode_hides_write_file_provider(registry):
    names = [s["function"]["name"] for s in registry.get_schemas(LEVEL_USER, capability_mode=True)]
    assert "filesystem_write" in names
    assert "write_file" not in names


def test_write_capability_schema_entry_is_clean(registry):
    """filesystem.write 条目序列化后不含任何实现层词汇；参数面保留 filename/content。"""
    schemas = registry.get_schemas(LEVEL_USER, capability_mode=True)
    entry = next(s for s in schemas if s["function"]["name"] == "filesystem_write")
    blob = json.dumps(entry, ensure_ascii=False)
    for tok in PROVIDER_TOKENS:
        assert tok not in blob
    props = entry["function"]["parameters"]["properties"]
    assert "filename" in props and "content" in props


# --------------------------------------------------------------------------
# 3. Schema name + 反查
# --------------------------------------------------------------------------

def test_write_schema_name_and_reverse_lookup(registry):
    assert registry.capability_index.capability_for_schema_name("filesystem_write") == "filesystem.write"
    assert registry.capability_index.capability_for_schema_name("write_file") is None


# --------------------------------------------------------------------------
# 4. Resolver 转发
# --------------------------------------------------------------------------

def test_resolver_resolves_filesystem_write_to_real_provider(registry):
    resolution = registry.capability_resolver.resolve(PILOT_CAPABILITY_ID_3, permission_level=LEVEL_USER)
    assert resolution.capability_id == "filesystem.write"
    assert resolution.provider.tool_name == "write_file"
    assert resolution.contract.domain == "filesystem"


@pytest.mark.asyncio
async def test_call_capability_routes_write_file_handler(registry):
    """capability 入口最终仍由 ToolRegistry.call() 执行真实 handler（用 stub 验证路由）。"""
    seen = []

    def stub(filename: str = "", content: str = ""):
        seen.append((filename, content))
        return f"written:{filename}"

    registry.tools["write_file"].handler = stub
    # M0.4.7 D-A1：capability 路由需总开关激活；filesystem.write 非治理能力，无需 allowlist。
    registry.capability_mode = True
    out = await registry.call_capability("filesystem.write", {"filename": "a.txt", "content": "x"}, LEVEL_USER)
    assert "written:a.txt" in out
    # schema name 入口同样可达
    out2 = await registry.call_capability("filesystem_write", {"filename": "b.txt", "content": "y"}, LEVEL_USER)
    assert "written:b.txt" in out2
    assert seen == [("a.txt", "x"), ("b.txt", "y")]


# --------------------------------------------------------------------------
# 5. Legacy 零影响（验收线 3）
# --------------------------------------------------------------------------

def test_legacy_schema_keeps_write_file_by_tool_name(registry):
    """默认 flag 关：write_file 仍以 Tool name 暴露，filesystem_write 不出现。"""
    names = [s["function"]["name"] for s in registry.get_schemas(LEVEL_USER)]
    assert "write_file" in names
    assert "filesystem_write" not in names


def test_mixed_mode_keeps_unmigrated_tools_visible(registry):
    names = [s["function"]["name"] for s in registry.get_schemas(LEVEL_USER, capability_mode=True)]
    assert "query_web_memory" in names   # 未迁移 → 仍是 Tool name
    assert "get_time" in names


# --------------------------------------------------------------------------
# 6. write scope 校验（显式契约让 scope 超集校验真实生效）
# --------------------------------------------------------------------------

def test_declared_write_contract_scope_gate():
    """未授予 filesystem scope 的 Provider 无法满足 filesystem.write 契约。"""
    reg = ToolRegistry()
    declare_filesystem_write_contract(reg)
    reg.register(Tool(
        "offline_write", "本地写",
        {"filename": {"type": "string"}, "content": {"type": "string"}},
        lambda filename="", content="": content,
        capability_ids=[PILOT_CAPABILITY_ID_3],   # 声明能力但未授予 filesystem
    ))
    with pytest.raises(PermissionDeniedError):
        reg.capability_resolver.resolve(PILOT_CAPABILITY_ID_3, permission_level=LEVEL_USER)


def test_declared_write_contract_accepts_provider_with_scope():
    reg = ToolRegistry()
    declare_filesystem_write_contract(reg)
    reg.register(Tool(
        "real_write", "真实写",
        {"filename": {"type": "string"}, "content": {"type": "string"}},
        lambda filename="", content="": content,
        capability_ids=[PILOT_CAPABILITY_ID_3],
        granted_scopes={"filesystem": True},
    ))
    resolution = reg.capability_resolver.resolve(PILOT_CAPABILITY_ID_3, permission_level=LEVEL_USER)
    assert resolution.provider.tool_name == "real_write"


# --------------------------------------------------------------------------
# 7. 显式契约 + 能力层描述
# --------------------------------------------------------------------------

def test_filesystem_write_contract_explicit(registry):
    contract = registry.capability_index.contract_for(PILOT_CAPABILITY_ID_3)
    assert contract is not None
    assert contract.id == "filesystem.write"
    assert contract.domain == "filesystem"
    assert contract.version == "1.0.0"
    assert contract.major == 1
    assert contract.stability is Stability.EXPERIMENTAL
    assert contract.permission.filesystem is True
    assert tuple(contract.permission.granted()) == ("filesystem",)


def test_filesystem_write_description_is_capability_level(registry):
    """能力描述由能力层给出，不透传 Provider / Tool 名。"""
    contract = registry.capability_index.contract_for(PILOT_CAPABILITY_ID_3)
    assert contract.description
    for tok in PROVIDER_TOKENS:
        assert tok not in contract.description


# --------------------------------------------------------------------------
# 8. handler 层：路径 containment + overwrite（KL-write-2）
# --------------------------------------------------------------------------

def test_write_file_confines_to_output_dir(tmp_path):
    """``../`` 穿越被规范化到 output/ 内，绝不逃逸 data 目录。"""
    out = write_file("../escape.txt", "x", data_dir=str(tmp_path))
    p = Path(out)
    assert p.parent.name == "output"
    assert p.parent.parent == tmp_path.resolve()
    # 父目录（data 根）未被写入 escape.txt
    assert not (tmp_path / "escape.txt").exists()
    assert p.read_text(encoding="utf-8") == "x"


def test_write_file_normalizes_absolute_path(tmp_path):
    """绝对路径同样被规范化到 output/ 内。"""
    out = write_file("/etc/passwd", "x", data_dir=str(tmp_path))
    assert Path(out).parent == (tmp_path / "output").resolve()


def test_write_file_overwrites_existing(tmp_path):
    """同文件名覆盖既有内容（KL-write-2：保持 legacy overwrite 语义）。"""
    write_file("a.txt", "v1", data_dir=str(tmp_path))
    write_file("a.txt", "v2", data_dir=str(tmp_path))
    assert (tmp_path / "output" / "a.txt").read_text(encoding="utf-8") == "v2"
