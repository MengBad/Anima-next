"""M0.4 Capability Expansion Phase 1.1 —— filesystem.read Pilot。

第二个真实 capability：新增只读 Tool ``read_file`` 作为 ``filesystem.read`` 的 Provider。
沿用 M0.3.1 的三条验收线（Provider 隐藏 / Resolver 转发 / Legacy 零影响），并额外钉死：

- 只读性由 handler 构造保证（只调读 API + data_dir 边界 + 大小上限 + 目录穿越拦截）；
- capability 集合严格等于治理裁定（search.web + filesystem.read，无第三个）；
- filesystem.read 的 scope 校验真实生效；
- 迁移期约束（混合模式仍暴露未迁移 Tool）保持可见。

已知限制（Known Limitation，不碰 RFC-0001 §7）：当前 scope 最小集合不区分 read/write，
``filesystem.read`` 的「只读」在 handler 层保证，sandbox 映射层保持既有
``filesystem → writable_dirs`` 语义。
"""
from __future__ import annotations

import json

import pytest

from anima.capability import CapabilityContract, PermissionScopes
from anima.contracts.capability import Stability
from anima.contracts.errors import PermissionDeniedError
from anima.tools import (
    LEVEL_OWNER,
    LEVEL_READONLY,
    LEVEL_USER,
    Tool,
    ToolRegistry,
    create_default_registry,
)
from anima.tools.builtin import (
    PILOT_CAPABILITY_ID,
    PILOT_CAPABILITY_ID_2,
    PILOT_CAPABILITY_ID_3,
    PILOT_CAPABILITY_ID_4,
    declare_filesystem_read_contract,
)
from anima.tools.builtin.file import read_file

#: 不应作为实现层词汇出现在 capability schema 中的 Token
PROVIDER_TOKENS = ["read_file", "write_file", "bing", "wttr"]


@pytest.fixture()
def registry() -> ToolRegistry:
    return create_default_registry()


# --------------------------------------------------------------------------
# 1. 索引与契约（含治理护栏：集合严格等于裁定）
# --------------------------------------------------------------------------

def test_filesystem_read_is_second_real_capability(registry):
    """capability 集合严格等于治理裁定：search.web + filesystem.read + filesystem.write
    + M0.4.4 预留的 code.execute（D3 已挂载唯一 Provider code_exec，故可执行但受
    capability_mode 默认关闭 + 独立 Pilot Activation gate 约束）。

    注：本测试随 Pilot / Reservation / D3 扩展同步更新集合（KL2 透明修正，非回归）。
    工具标注集合（下条 annotated）现为 4 个 Tool：web_search / read_file /
    write_file / code_exec。
    """
    assert registry.capability_index.capabilities() == [
        PILOT_CAPABILITY_ID, PILOT_CAPABILITY_ID_2, PILOT_CAPABILITY_ID_3,
        PILOT_CAPABILITY_ID_4,
    ]
    # 恰好 4 个 Tool 被标注，且身份固定
    annotated = {t.name: t.capability_ids for t in registry.tools.values() if t.capability_ids}
    assert annotated == {
        "web_search": [PILOT_CAPABILITY_ID],
        "read_file": [PILOT_CAPABILITY_ID_2],
        "write_file": [PILOT_CAPABILITY_ID_3],
        "code_exec": [PILOT_CAPABILITY_ID_4],
    }
    # 其余全部零标注（legacy ABI 不变）
    assert all(
        t.capability_ids == []
        for t in registry.tools.values()
        if t.name not in ("web_search", "read_file", "write_file", "code_exec")
    )


def test_filesystem_read_contract_explicit(registry):
    """显式 Contract 生效：domain / version / stability / permission 均来自声明。"""
    contract = registry.capability_index.contract_for(PILOT_CAPABILITY_ID_2)
    assert contract is not None
    assert contract.id == "filesystem.read"
    assert contract.domain == "filesystem"
    assert contract.version == "1.0.0"
    assert contract.major == 1
    assert contract.stability is Stability.EXPERIMENTAL
    assert contract.permission.filesystem is True
    assert tuple(contract.permission.granted()) == ("filesystem",)


def test_filesystem_read_description_is_capability_level(registry):
    """能力层描述不透传 Tool 描述，且不含任何 Provider / 实现层词汇。"""
    contract = registry.capability_index.contract_for(PILOT_CAPABILITY_ID_2)
    tool_description = registry.tools["read_file"].description
    assert contract.description != tool_description
    lowered = contract.description.lower()
    for token in PROVIDER_TOKENS:
        assert token not in lowered, f"能力描述泄漏实现词汇: {token}"


# --------------------------------------------------------------------------
# 2. Legacy path 零影响
# --------------------------------------------------------------------------

def test_legacy_schema_byte_identical_with_both_pilots(registry):
    """默认模式（capability_mode=False）输出与未标注前一致——除 F0 closure：

    capability-only 工具（code_exec）退出 legacy 表面，其余 mixed-mode 工具
    （web_search / read_file / write_file）仍 legacy 暴露。该差异是 M0.4.7 D-A0
    的既定行为，非回归。
    """
    for level in (LEVEL_READONLY, LEVEL_USER, LEVEL_OWNER):
        expected = [
            t.to_openai_schema()
            for t in registry.tools.values()
            if t.enabled and level >= t.min_level and not t.capability_only
        ]
        assert registry.get_schemas(level) == expected


def test_legacy_schema_still_exposes_read_file_name(registry):
    """默认模式下 read_file 仍以 Tool name 暴露 —— legacy path 未被移除。"""
    names = [s["function"]["name"] for s in registry.get_schemas(LEVEL_USER)]
    assert "read_file" in names
    assert "filesystem_read" not in names


def test_read_file_tool_abi_unchanged(registry):
    """标注是纯 additive：name / min_level / required / handler 未被改动。"""
    tool = registry.tools["read_file"]
    assert tool.name == "read_file"
    assert tool.min_level == LEVEL_USER
    assert tool.required == ["filename"]
    assert callable(tool.handler)
    assert tool.capability_ids == ["filesystem.read"]
    assert tuple(tool.granted_scopes.granted()) == ("filesystem",)


@pytest.mark.asyncio
async def test_legacy_call_read_file_by_tool_name_still_works(registry, tmp_path):
    """旧路径 Agent → tool name → call() → handler 未受影响。"""
    target = tmp_path / "note.txt"
    target.write_text("hello", encoding="utf-8")

    seen = []

    def stub(filename: str = "", max_bytes: int = 200_000) -> str:
        seen.append(filename)
        return f"legacy:{filename}"

    registry.tools["read_file"].handler = stub
    out = await registry.call("read_file", {"filename": str(target)}, LEVEL_USER)
    assert "legacy:" in out
    assert seen == [str(target)]


# --------------------------------------------------------------------------
# 3. Capability 模式：Provider 隐藏
# --------------------------------------------------------------------------

def test_capability_mode_hides_read_file_provider(registry):
    """capability 模式下 LLM 只看到能力 id，看不到 Provider（Tool）名。"""
    schemas = registry.get_schemas(LEVEL_USER, capability_mode=True)
    names = [s["function"]["name"] for s in schemas]
    assert "filesystem_read" in names
    assert "read_file" not in names, "Provider 名不得作为 function name 暴露"


def test_filesystem_read_schema_entry_is_clean(registry):
    """filesystem.read 能力条目自身序列化后不含任何实现层词汇。"""
    schemas = registry.get_schemas(LEVEL_USER, capability_mode=True)
    entry = next(s for s in schemas if s["function"]["name"] == "filesystem_read")
    blob = json.dumps(entry, ensure_ascii=False).lower()
    for token in PROVIDER_TOKENS:
        assert token not in blob, f"capability schema 泄漏实现词汇: {token}"
    # 参数面从 Provider 透传，属能力签名的一部分，必须保留
    assert set(entry["function"]["parameters"]["properties"]) == {"filename", "max_bytes"}
    assert entry["function"]["parameters"]["required"] == ["filename"]


def test_capability_mode_keeps_unmigrated_tools_visible(registry):
    """已裁定的迁移期约束（Accepted Migration Constraint）——显式钉住。

    混合模式下未迁移 Tool 仍以 Tool name 暴露；这不是泄漏失败，
    是「不移除 legacy Tool path」纪律的直接后果，迁移窗口内成立。
    """
    names = [s["function"]["name"] for s in registry.get_schemas(LEVEL_USER, capability_mode=True)]
    assert "query_web_memory" in names   # 未迁移 → 仍是 Tool name
    assert "get_time" in names
    # 两个已迁移的则完成了切换
    assert "search_web" in names and "web_search" not in names
    assert "filesystem_read" in names and "read_file" not in names


# --------------------------------------------------------------------------
# 4. Resolver 转发
# --------------------------------------------------------------------------

def test_resolver_resolves_filesystem_read_to_real_provider(registry):
    resolution = registry.capability_resolver.resolve(PILOT_CAPABILITY_ID_2, permission_level=LEVEL_USER)
    assert resolution.capability_id == "filesystem.read"
    assert resolution.provider.tool_name == "read_file"
    assert resolution.contract.domain == "filesystem"


def test_resolver_reverse_lookup_filesystem_read(registry):
    assert registry.capability_index.capability_for_schema_name("filesystem_read") == "filesystem.read"
    assert registry.capability_index.capability_for_schema_name("read_file") is None


@pytest.mark.asyncio
async def test_call_capability_routes_read_file_handler(registry):
    """capability 入口最终仍由 ToolRegistry.call() 执行真实 handler。"""
    seen = []

    def stub(filename: str = "", max_bytes: int = 200_000) -> str:
        seen.append(filename)
        return f"resolved:{filename}"

    registry.tools["read_file"].handler = stub
    out = await registry.call_capability("filesystem.read", {"filename": "a.txt"}, LEVEL_USER)
    assert "resolved:a.txt" in out
    # schema name 入口同样可达
    out2 = await registry.call_capability("filesystem_read", {"filename": "b.txt"}, LEVEL_USER)
    assert "resolved:b.txt" in out2
    assert seen == ["a.txt", "b.txt"]


# --------------------------------------------------------------------------
# 5. 只读性 + 路径安全（handler 层）
# --------------------------------------------------------------------------

def test_read_file_handler_reads_content(tmp_path):
    f = tmp_path / "ok.txt"
    f.write_text("payload-123", encoding="utf-8")
    assert read_file("ok.txt", data_dir=str(tmp_path)) == "payload-123"


def test_read_file_blocks_directory_traversal(tmp_path):
    """../ 穿越被拦截，无法读取 data 目录外的文件。"""
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("TOPSECRET", encoding="utf-8")
    with pytest.raises(ValueError):
        read_file("../secret.txt", data_dir=str(tmp_path))


def test_read_file_rejects_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_file("nope.txt", data_dir=str(tmp_path))


def test_read_file_rejects_oversize(tmp_path):
    big = tmp_path / "big.txt"
    big.write_text("x" * 100, encoding="utf-8")
    with pytest.raises(ValueError):
        read_file("big.txt", data_dir=str(tmp_path), max_bytes=10)


def test_read_file_never_writes_or_mutates(tmp_path):
    """只读：读取后 data 目录文件数不变，且不产生任何新文件。"""
    f = tmp_path / "data.txt"
    f.write_text("stable", encoding="utf-8")
    before = sorted(p.name for p in tmp_path.iterdir())
    read_file("data.txt", data_dir=str(tmp_path))
    after = sorted(p.name for p in tmp_path.iterdir())
    assert after == before
    # 内容未被改写
    assert f.read_text(encoding="utf-8") == "stable"


def test_read_file_respects_data_dir_boundary(tmp_path):
    """允许读取 data 目录内的子路径，但越界一律拒绝。"""
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "inner.txt").write_text("inner", encoding="utf-8")
    assert read_file("sub/inner.txt", data_dir=str(tmp_path)) == "inner"
    # 绝对路径混入也被拦截
    with pytest.raises(ValueError):
        read_file(str((tmp_path.parent / "escape.txt")), data_dir=str(tmp_path))


# --------------------------------------------------------------------------
# 6. 显式契约让 scope 校验真实生效
# --------------------------------------------------------------------------

def test_declared_filesystem_contract_scope_gate():
    """未授予 filesystem scope 的 Provider 无法满足 filesystem.read 契约。"""
    reg = ToolRegistry()
    declare_filesystem_read_contract(reg)
    reg.register(Tool(
        "offline_fs", "本地读", {"filename": {"type": "string"}},
        lambda filename="": filename,
        capability_ids=[PILOT_CAPABILITY_ID_2],   # 声明能力但未授予 filesystem
    ))
    with pytest.raises(PermissionDeniedError):
        reg.capability_resolver.resolve(PILOT_CAPABILITY_ID_2, permission_level=LEVEL_USER)


def test_declared_filesystem_contract_accepts_provider_with_scope():
    reg = ToolRegistry()
    declare_filesystem_read_contract(reg)
    reg.register(Tool(
        "real_fs", "真实读", {"filename": {"type": "string"}},
        lambda filename="": filename,
        capability_ids=[PILOT_CAPABILITY_ID_2],
        granted_scopes={"filesystem": True},
    ))
    resolution = reg.capability_resolver.resolve(PILOT_CAPABILITY_ID_2, permission_level=LEVEL_USER)
    assert resolution.provider.tool_name == "real_fs"


def test_declare_filesystem_read_contract_idempotent_and_tolerant():
    """重复声明幂等；对没有 capability_index 的替身 registry 静默跳过。"""
    reg = ToolRegistry()
    declare_filesystem_read_contract(reg)
    declare_filesystem_read_contract(reg)
    assert reg.capability_index.capabilities() == [PILOT_CAPABILITY_ID_2]

    class FakeRegistry:
        def register(self, tool):  # pragma: no cover - 仅作替身
            pass

    declare_filesystem_read_contract(FakeRegistry())  # 不得抛异常


def test_filesystem_read_contract_matches_declared_shape():
    """契约形状与 builtin 声明一致（防止两处漂移）。"""
    reg = ToolRegistry()
    declare_filesystem_read_contract(reg)
    contract = reg.capability_index.contract_for(PILOT_CAPABILITY_ID_2)
    assert contract == CapabilityContract(
        id="filesystem.read",
        version="1.0.0",
        domain="filesystem",
        description=contract.description,
        stability=Stability.EXPERIMENTAL,
        permission=PermissionScopes(filesystem=True),
    )
