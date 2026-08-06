"""M0.4.6 Step 5 —— T2–T8 Verification（挂载后安全性质回归，不激活 capability_mode）。

本文件在 D1/D2/D3 已落地（单 provider `code_exec` 已挂载）的前提下，验证「挂载后
安全性质仍成立」。重点是**性质回归**而非功能测试：

- T2  Provider Resolution    : resolve("code.execute") -> code_exec（≠ run_python）
- T3  No Retro Tag           : run_python/run_shell 不被倒灌 code.execute
- T4  Scope Boundary         : code_exec 仅 {exec:True}，exec ≠ 身份提权
- T5  Schema Boundary        : 拒绝 privilege/io 注入字段 -> execution_denied{schema_violation}
- T6  Audit Single Source    : 一次执行只产生 run_python 的 execution_*，无双审计
- T7  Deny Path Audit Complete: 所有拒绝/失败入口均产生审计（无静默拒绝）
- T8  Dual Mode Regression   : legacy 模式行为不变；pilot 模式仅暴露 code.execute 路由

纪律（与 Review Package 一致）：
- 本文件不翻转 capability_mode、不扩展 scope、不碰 legacy 执行工具 ABI；
- 执行类测试（T5/T6/T7）使用聚焦 harness（声明契约 + 挂载 D3 provider + fake run_python），
  不依赖真实沙箱，确定性可重复；
- 内省类测试（T2/T3/T4/T8）使用 `register_all_builtin` 真实接线，忠实于生产 D3 状态。
"""

import asyncio
import pytest

from anima.tools import LEVEL_USER, Tool, ToolRegistry
from anima.tools.builtin.code_exec import make_code_exec_tool
from anima.tools.builtin import register_all_builtin
from anima.capability import CapabilityContract, PermissionScopes
from anima.contracts.capability import Stability
from anima.contracts.errors import PermissionDeniedError


# --------------------------------------------------------------------------
# 测试脚手架
# --------------------------------------------------------------------------

class _CaptureAudit:
    """捕获 emit 的事件类型与字段，供断言使用。"""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, **fields):
        self.events.append((event_type, fields))

    def types(self):
        return [e for e, _ in self.events]

    def by_type(self, event_type: str):
        return [f for e, f in self.events if e == event_type]


class _FakeCore:
    def __init__(self):
        self.audit = _CaptureAudit()


def _real_wired_registry(capability_mode: bool = False) -> ToolRegistry:
    """真实接线（与 D3 后生产状态一致）：register_all_builtin 声明契约并挂载
    单 provider code_exec。core=None 跳过 owner-only 工具（不影响本文件断言）。"""
    reg = ToolRegistry(capability_mode=capability_mode)
    register_all_builtin(reg, None)
    return reg


def _mounted(core=None, run_python_grants_exec: bool = True) -> tuple[ToolRegistry, _FakeCore, dict]:
    """聚焦 harness：声明 code.execute 契约 + 挂载 D3 provider（与 register_all_builtin
    完全一致的绑定）+ 安装 fake run_python。用于执行类验证，不依赖真实沙箱。"""
    core = core or _FakeCore()
    reg = ToolRegistry(core)

    # 声明契约（幂等；与 reservation/D3 同构）
    reg.capability_index.declare_contract(CapabilityContract(
        id="code.execute", version="1.0.0", domain="code",
        description="test", stability=Stability.EXPERIMENTAL,
        permission=PermissionScopes(exec=True),
    ))
    # 挂载 D3 provider（绑定与 register_all_builtin 一字不差）
    reg.register(make_code_exec_tool(
        reg, capability_ids=["code.execute"], granted_scopes={"exec": True}))

    # fake run_python（执行基底）。默认授予 exec scope 使委托路径可达下层审计；
    # run_python_grants_exec=False 用于模拟「缺失 exec scope」的拒绝入口。
    captured = {"calls": []}

    async def _fake_run_python(code, permission_level=LEVEL_USER, **_kw):
        captured["calls"].append({"code": code, "permission_level": permission_level})
        return f"ran_python:{len(code)}"

    reg.tools["run_python"] = Tool(
        name="run_python", description="exec",
        parameters={"code": {"type": "string"}},
        handler=_fake_run_python, min_level=LEVEL_USER,
        granted_scopes={"exec": True} if run_python_grants_exec else None,
    )
    return reg, core, captured


# ==========================================================================
# T2 —— Provider Resolution
# ==========================================================================

def test_t2_resolve_routes_to_code_exec_not_run_python():
    """resolve("code.execute") 必须路由到 code_exec provider，而非 run_python 基底。"""
    reg = _real_wired_registry()
    res = reg.capability_resolver.resolve("code.execute", permission_level=LEVEL_USER)
    assert res.provider.tool_name == "code_exec"
    assert res.provider.tool_name != "run_python"
    # LLM 侧 schema name 反查仍映射到 code.execute（call_capability 入口使用此映射）
    assert reg.capability_index.capability_for_schema_name("code_execute") == "code.execute"


def test_t2_single_provider_isolated():
    """code.execute 恰好有 1 个 provider（code_exec），且不影响既有能力 provider。"""
    reg = _real_wired_registry()
    providers = [p.id for p in reg.capability_index.providers_for("code.execute")]
    assert providers == ["code_exec"]
    # 既有能力 provider 不受影响（D3 只增 code.execute 单 provider）
    assert [p.id for p in reg.capability_index.providers_for("filesystem.read")] == ["read_file"]
    assert [p.id for p in reg.capability_index.providers_for("search.web")] == ["web_search"]


# ==========================================================================
# T3 —— No Retro Tag
# ==========================================================================

def test_t3_no_retro_tag_on_legacy_execution_tools():
    """capability 不得倒灌 legacy 执行路径：run_python/run_shell 不被标注 code.execute。"""
    reg = _real_wired_registry()
    # legacy 执行工具保持零标注
    assert reg.tools["run_python"].capability_ids == []
    assert reg.tools["run_shell"].capability_ids == []
    # code.execute 的唯一 provider 是 code_exec，不把 legacy 工具变作其 provider
    tagged = [t.name for t in reg.tools.values() if "code.execute" in t.capability_ids]
    assert tagged == ["code_exec"], f"code.execute 倒灌到了非预期工具: {tagged}"
    # 强化：所有 EXECUTION_TOOLS 内的 legacy 工具均不得被标注
    for name in ("run_python", "run_shell"):
        assert name not in tagged, f"{name} 被 retro-tag 为 code.execute"


# ==========================================================================
# T4 —— Scope Boundary
# ==========================================================================

def test_t4_code_exec_scope_is_exec_only():
    """code_exec 的 granted_scopes 仅 {exec:True}；exec scope 不构成身份提权。"""
    reg = _real_wired_registry()
    gs = reg.tools["code_exec"].granted_scopes
    assert gs.exec is True
    assert gs.filesystem is False
    assert gs.network is False
    # exec scope ≠ 身份提权：provider 的 min_level 仍为默认 user 层，未借 capability 抬高
    assert reg.tools["code_exec"].min_level == LEVEL_USER
    # 契约层 scope 也仅 exec（与 RFC-0001 §7 一致，不引入 filesystem/network）
    contract = reg.capability_index.contract_for("code.execute")
    assert contract.permission.exec is True
    assert contract.permission.filesystem is False
    assert contract.permission.network is False


# ==========================================================================
# T5 —— Schema Boundary
# ==========================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [
    {"permission_level": 4},          # 特权参数注入
    {"network": True},                 # 网络注入
    {"filesystem": "rw"},              # 文件系统注入
    {"permission_level": 4, "network": True, "filesystem": "rw"},  # 混合注入
])
async def test_t5_schema_rejects_privilege_and_io_injection(extra):
    """code.execute 只允许 {code} 字段；任何额外字段被拒绝并产生
    execution_denied{reason:schema_violation}，绝不委托到 run_python。

    注：经 registry.call 的集成路径中，CodeExecSchemaViolation 由 ToolRegistry.call
    的通用 except 捕获并转为安全失败字符串（与 D2 直接调 handler 不同）——本测试验证
    的是「拒绝到达调用方 + 审计产生 + 不委托」这一完整安全性质。
    """
    reg, core, captured = _mounted()
    token = reg.set_call_context(user_id=1, group_id=10, permission_level=LEVEL_USER)
    try:
        out = await reg.call("code_exec", {"code": "print(1)", **extra}, permission_level=LEVEL_USER)
    finally:
        reg.reset_call_context(token)

    # 拒绝：调用返回安全失败，绝不委托到 run_python（注入字段无法穿透到基底）
    assert "ran_python" not in out
    denied = core.audit.by_type("execution_denied")
    assert denied, "schema 违反必须产生 execution_denied"
    assert any(f.get("reason") == "schema_violation" for f in denied)
    assert captured["calls"] == [], "注入字段不得穿透到 run_python"


# ==========================================================================
# T6 —— Audit Single Source
# ==========================================================================

@pytest.mark.asyncio
async def test_t6_no_double_audit_single_source():
    """一次 code.execute 执行只产生 run_python 的执行审计；code_exec 外层不产生
    第二份 execution_* 轨迹（无双审计）。"""
    reg, core, captured = _mounted()
    token = reg.set_call_context(user_id=123, group_id=55, permission_level=LEVEL_USER)
    try:
        out = await reg.call("code_exec", {"code": "print('hi')"}, permission_level=LEVEL_USER)
    finally:
        reg.reset_call_context(token)

    assert "ran_python" in out
    types = core.audit.types()
    # 恰好一次 started + 一次 finished（来自被委托的 run_python 基底）
    assert types.count("execution_started") == 1, types
    assert types.count("execution_finished") == 1, types
    started = core.audit.by_type("execution_started")[0]
    finished = core.audit.by_type("execution_finished")[0]
    assert started["tool"] == "run_python", "外层 code_exec 不应产生 execution 审计"
    assert finished["tool"] == "run_python"
    # 无 code_exec 自身的执行轨迹（无双审计）
    assert all(f.get("tool") != "code_exec"
               for e, f in core.audit.events if e.startswith("execution_"))


# ==========================================================================
# T7 —— Deny Path Audit Completeness
# ==========================================================================

@pytest.mark.asyncio
async def test_t7_missing_exec_scope_denied_with_audit():
    """入口：missing exec scope —— 基底 run_python 未获 exec scope 时，委托执行被门控，
    产生 execution_denied{reason:missing_exec_scope}（闭合契约 v0.2 F1）。"""
    reg, core, captured = _mounted(run_python_grants_exec=False)
    token = reg.set_call_context(user_id=1, group_id=10, permission_level=LEVEL_USER)
    try:
        await reg.call("code_exec", {"code": "print(1)"}, permission_level=LEVEL_USER)
    finally:
        reg.reset_call_context(token)

    denied = core.audit.by_type("execution_denied")
    assert denied and any(f.get("reason") == "missing_exec_scope" for f in denied)
    assert captured["calls"] == []


@pytest.mark.asyncio
async def test_t7_schema_violation_denied_with_audit():
    """入口：schema violation —— 已在 T5 覆盖，此处作为 deny 完整性的一环再次确认。"""
    reg, core, captured = _mounted()
    token = reg.set_call_context(user_id=1, group_id=10, permission_level=LEVEL_USER)
    try:
        out = await reg.call("code_exec", {"code": "x", "network": True}, permission_level=LEVEL_USER)
    finally:
        reg.reset_call_context(token)
    assert "ran_python" not in out
    assert any(f.get("reason") == "schema_violation"
               for f in core.audit.by_type("execution_denied"))


@pytest.mark.asyncio
async def test_t7_permission_insufficient_denied_with_audit():
    """入口：permission insufficient —— 调用方权限低于 code_exec min_level 时拒绝。

    注：真实实现中此门由 ToolRegistry._invoke 的 can_call 守卫，审计事件为
    ``tool_denied``（与 exec-scope 门区分），核心不变量「无静默拒绝」成立。
    这与 Review Package T7 表所列的 execution_denied 命名存在差异——属既有门控语义，
    非 D3 引入；如要求统一为 execution_denied 需单独 gate（不在 T2–T8 范围内）。
    """
    reg, core, captured = _mounted()
    # readonly(1) < LEVEL_USER(2) -> can_call False
    token = reg.set_call_context(user_id=1, group_id=10, permission_level=1)
    try:
        out = await reg.call("code_exec", {"code": "print(1)"}, permission_level=1)
    finally:
        reg.reset_call_context(token)
    assert "权限不足" in out
    assert core.audit.by_type("tool_denied"), "权限不足必须产生审计（无静默拒绝）"
    assert "execution_started" not in core.audit.types()


@pytest.mark.asyncio
async def test_t7_resolver_permission_gate_raises_no_execution():
    """入口：capability resolver 权限门槛 —— 低于 provider min_level 时 resolve 直接拒绝，
    绝不进入执行（无 execution 审计）。"""
    reg, core, _ = _mounted()
    token = reg.set_call_context(user_id=1, group_id=10, permission_level=0)
    try:
        with pytest.raises(PermissionDeniedError):
            # 注意：call_capability 本身不依赖 capability_mode flag，仅为验证 resolver 门槛
            await reg.call_capability("code.execute", {"code": "print(1)"}, permission_level=0)
    finally:
        reg.reset_call_context(token)
    assert "execution_started" not in core.audit.types()


@pytest.mark.asyncio
async def test_t7_delegate_failure_emits_execution_failed():
    """入口：sandbox / 执行失败 —— 委托的基底执行抛错时，唯一执行轨迹
    execution_failed 必须产生（失败审计完整性，对应 T7 表的 sandbox deny 行）。"""
    reg, core, captured = _mounted()

    async def _boom(code, permission_level=LEVEL_USER, **_kw):
        captured["calls"].append({"code": code})
        raise RuntimeError("sandbox denied")

    reg.tools["run_python"] = Tool(
        name="run_python", description="exec",
        parameters={"code": {"type": "string"}},
        handler=_boom, min_level=LEVEL_USER, granted_scopes={"exec": True},
    )
    token = reg.set_call_context(user_id=1, group_id=10, permission_level=LEVEL_USER)
    try:
        await reg.call("code_exec", {"code": "print(1)"}, permission_level=LEVEL_USER)
    finally:
        reg.reset_call_context(token)

    assert captured["calls"], "委托确实发生"
    assert core.audit.by_type("execution_started"), "执行开始必须审计"
    assert core.audit.by_type("execution_failed"), "执行失败必须审计（sandbox deny 不静默）"


# ==========================================================================
# T8 —— Dual Mode Regression
# ==========================================================================

def test_t8_legacy_mode_unchanged():
    """legacy 模式（默认，capability_mode=False）：行为逐字节不变。
    - code_exec 作为普通 Tool 暴露（与 web_search/read_file 混合模式同构）；
    - capability 路由名 code_execute 不出现；
    - run_python 等 legacy 工具仍是普通 Tool。"""
    reg = _real_wired_registry(capability_mode=False)
    assert reg.capability_mode is False
    names = [s["function"]["name"] for s in reg.get_schemas(permission_level=LEVEL_USER)]
    assert "code_exec" in names, "legacy 模式应把 code_exec 作为普通 Tool 暴露"
    assert "run_python" in names, "legacy 工具 run_python 应保持暴露"
    assert "code_execute" not in names, "legacy 模式不得暴露 capability 路由名"


def test_t8_pilot_mode_exposes_capability_only_for_code_execute():
    """pilot 模式（capability_mode=True）：capability 路由仅暴露已挂载能力的 tools，
    且 legacy-only 工具仍走 Tool schema（混合模式，不残废）。
    核心不变量：capability 提供身份+路由，不是新的执行权限来源——legacy 工具不获得
    新的 code.execute 旁路。"""
    reg = _real_wired_registry(capability_mode=True)
    assert reg.capability_mode is True
    names = [s["function"]["name"] for s in reg.get_schemas(permission_level=LEVEL_USER)]
    # capability 路由暴露（名 code_execute，不含 Provider 名）
    assert "code_execute" in names
    # 但作为 capability 暴露，原始 Tool 名 code_exec 不应再以 Tool schema 出现
    assert "code_exec" not in names
    # legacy-only 工具仍以 Tool schema 暴露（混合模式）
    assert "run_python" in names
    # legacy 工具不获得 code.execute 旁路：run_python 仍零标注
    assert reg.tools["run_python"].capability_ids == []
    # capability 路由只指向 code_exec，不把 run_python 变作 code.execute provider
    assert [p.id for p in reg.capability_index.providers_for("code.execute")] == ["code_exec"]


# 便于直接 `python -m pytest` 运行
if __name__ == "__main__":  # pragma: no cover
    asyncio.run(pytest.main([__file__, "-q"]))
