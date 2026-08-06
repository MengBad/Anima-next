"""M0.4.7 D-A0 —— F0 closure 验收测试（capability-only legacy 绕过闭合）。

D-A0 引入逐工具开关 ``Tool.capability_only``：``code_exec`` 挂载时被置为 True，
从而退出 legacy tool 表面（get_schemas 不暴露）且禁止 legacy 直调（call 守卫），
仅经 capability 路由（call_capability 内部 _via_capability=True）可达。

既有 mixed-mode 工具（web_search / read_file / write_file）保持 capability_only=False，
legacy 暴露 + capability 路由并存的行为不变。

纪律（与 Activation Review Package 一致）：
- 本文件不翻转 capability_mode、不扩展 scope、不碰 legacy 执行工具 ABI、不接 LLM 路由；
- 仅验证 F0 closure 的两种不变量：
  (a) legacy 表面不再暴露 code_exec，但 mixed-mode 工具不受影响；
  (b) legacy 直调 code_exec 被治理守卫拒绝并产生审计；capability 路由仍可达。
"""

import pytest

from anima.tools import LEVEL_USER, Tool, ToolRegistry
from anima.tools.builtin import register_all_builtin
from anima.tools.builtin.code_exec import make_code_exec_tool
from anima.capability import CapabilityContract, PermissionScopes
from anima.contracts.capability import Stability


# --------------------------------------------------------------------------
# 脚手架
# --------------------------------------------------------------------------

class _CaptureAudit:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, **fields):
        self.events.append((event_type, fields))

    def by_type(self, event_type: str):
        return [f for e, f in self.events if e == event_type]


class _FakeCore:
    def __init__(self):
        self.audit = _CaptureAudit()


def _real_wired_registry(capability_mode: bool = False) -> ToolRegistry:
    reg = ToolRegistry(capability_mode=capability_mode)
    register_all_builtin(reg, None)
    return reg


def _mounted(capability_only: bool = True) -> tuple[ToolRegistry, _FakeCore, dict]:
    """聚焦 harness：声明契约 + 挂载 code_exec（capability_only 可控）+ fake run_python。"""
    core = _FakeCore()
    reg = ToolRegistry(core)
    reg.capability_index.declare_contract(CapabilityContract(
        id="code.execute", version="1.0.0", domain="code",
        description="test", stability=Stability.EXPERIMENTAL,
        permission=PermissionScopes(exec=True),
    ))
    reg.register(make_code_exec_tool(
        reg, capability_ids=["code.execute"], granted_scopes={"exec": True},
        capability_only=capability_only))
    captured = {"calls": []}

    async def _fake_run_python(code, permission_level=LEVEL_USER, **_kw):
        captured["calls"].append({"code": code})
        return f"ran_python:{len(code)}"

    reg.tools["run_python"] = Tool(
        name="run_python", description="exec",
        parameters={"code": {"type": "string"}},
        handler=_fake_run_python, min_level=LEVEL_USER, granted_scopes={"exec": True},
    )
    return reg, core, captured


# ==========================================================================
# F0 closure —— legacy 表面
# ==========================================================================

def test_da0_legacy_schema_excludes_code_exec():
    """legacy 模式 get_schemas 不再包含 code_exec（F0 closure）。"""
    reg = _real_wired_registry(capability_mode=False)
    names = [s["function"]["name"] for s in reg.get_schemas(permission_level=LEVEL_USER)]
    assert "code_exec" not in names, "F0 后 legacy 表面必须排除 code_exec"


def test_da0_legacy_schema_keeps_mixed_mode_tools():
    """mixed-mode 既有能力工具仍 legacy 暴露（D-A0 不误伤）。"""
    reg = _real_wired_registry(capability_mode=False)
    names = [s["function"]["name"] for s in reg.get_schemas(permission_level=LEVEL_USER)]
    for name in ("web_search", "read_file", "write_file"):
        assert name in names, f"mixed-mode 工具 {name} 不应被 F0 误伤"
    for name in ("web_search", "read_file", "write_file"):
        assert reg.tools[name].capability_only is False


def test_da0_pilot_schema_exposes_capability_route_only():
    """pilot 模式 capability 路由暴露 code_execute，但仍不暴露原始 Tool 名 code_exec。"""
    reg = _real_wired_registry(capability_mode=True)
    names = [s["function"]["name"] for s in reg.get_schemas(permission_level=LEVEL_USER)]
    assert "code_execute" in names
    assert "code_exec" not in names


def test_da0_code_exec_capability_only_flag_set():
    """挂载后 code_exec.capability_only == True（D-A0 核心不变量）。"""
    reg = _real_wired_registry(capability_mode=False)
    assert reg.tools["code_exec"].capability_only is True


# ==========================================================================
# F0 closure —— legacy 直调守卫
# ==========================================================================

@pytest.mark.asyncio
async def test_da0_direct_call_code_exec_rejected_with_audit():
    """legacy 直调 capability-only 工具 code_exec 被治理守卫拒绝：
    - 返回治理拒绝文案；
    - 产生 execution_denied{reason=capability_only_called_via_legacy}（无静默拒绝）；
    - 绝不委托到 run_python。
    """
    reg, core, captured = _mounted(capability_only=True)
    token = reg.set_call_context(user_id=1, group_id=10, permission_level=LEVEL_USER)
    try:
        out = await reg.call("code_exec", {"code": "print(1)"}, permission_level=LEVEL_USER)
    finally:
        reg.reset_call_context(token)

    assert "只能通过 capability 路由调用" in out, "capability-only 守卫必须拒绝 legacy 直调"
    denied = core.audit.by_type("execution_denied")
    assert denied and any(f.get("reason") == "capability_only_called_via_legacy" for f in denied), \
        "F0 守卫必须产生 execution_denied{reason=capability_only_called_via_legacy}"
    assert captured["calls"] == [], "legacy 直调绝不可委托到 run_python"


@pytest.mark.asyncio
async def test_da0_capability_only_false_keeps_legacy_callable():
    """未置 capability_only 的同款工具仍可 legacy 直调（验证开关语义对称，非回归）。"""
    reg, core, captured = _mounted(capability_only=False)
    token = reg.set_call_context(user_id=1, group_id=10, permission_level=LEVEL_USER)
    try:
        out = await reg.call("code_exec", {"code": "print(1)"}, permission_level=LEVEL_USER)
    finally:
        reg.reset_call_context(token)
    assert "ran_python" in out, "capability_only=False 时 legacy 直调应可达"
    assert captured["calls"], "委托发生"


@pytest.mark.asyncio
async def test_da0_capability_route_still_resolves_code_exec():
    """经 call_capability 路由仍正确解析到 code_exec（F0 不影响 capability 路径）。"""
    reg, core, captured = _mounted(capability_only=True)
    token = reg.set_call_context(user_id=1, group_id=10, permission_level=LEVEL_USER)
    try:
        out = await reg.call_capability("code.execute", {"code": "print('hi')"},
                                         permission_level=LEVEL_USER)
    finally:
        reg.reset_call_context(token)

    assert "ran_python" in out, "capability 路由必须仍可达 code_exec -> run_python"
    assert captured["calls"], "委托确实发生，F0 未阻断 capability 路径"
    assert captured["calls"][0]["code"] == "print('hi')"


# 便于直接 `python -m pytest` 运行
if __name__ == "__main__":  # pragma: no cover
    import asyncio
    import pytest as _pytest
    asyncio.run(_pytest.main([__file__, "-q"]))
