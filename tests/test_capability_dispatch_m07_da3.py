"""M0.4.7 D-A3 —— LLM Dispatch Wiring（R3 断链闭合）。

本文件钉死 D-A3 裁定：

- **R3 断链闭合**：capability 模式下 LLM 看到的 schema name（``code_execute``）现在
  经 ``ToolRegistry.call()`` 唯一入口转发到 ``call_capability``，再经 Resolver 选
  Provider、执行仍由 ``call`` 完成。不再出现「工具不存在」断链。
- **allowlist gate 在执行前**：治理路由（capability-only Provider）仍须 pilot allowlist
  放行，否则拒绝 + 审计。
- **deny 必须 audit**：所有拒绝经 ``capability_route_denied`` 审计，无静默拒绝。
- **不改变普通 tool dispatch**：legacy 工具名 ``capability_for_schema_name`` 返回 None，
  逐字节走原路径；``run_python`` / ``tavily`` 等照常。

纪律（D-A3 不允许）：不接 telemetry（D-A4）、不翻 ``capability_mode``、不加 pilot UID、
不修 ``run_python`` / ``run_shell``、不碰 mixed-mode（``web_search`` / ``read_file`` /
``write_file``）暴露面、不扩 scope、react / executor / webui 调用方零改动（路由集中
在 ``call`` 一处）。
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from anima.capability import CapabilityContract, PermissionScopes
from anima.contracts.capability import Stability
from anima.core.config import AnimaConfig
from anima.tools import (
    LEVEL_USER,
    Tool,
    ToolRegistry,
    create_default_registry,
)
from anima.tools.builtin.code_exec import make_code_exec_tool

PILOT = "code.execute"          # capability id（真实挂载值，见 code_exec.py）
SCHEMA = "code_execute"         # LLM function name = schema_name_for("code.execute")

ALICE = "user:alice"
BOB = "user:bob"

ANIMA_DIR = Path(__file__).resolve().parent.parent / "anima"


# --------------------------------------------------------------------------
# 脚手架
# --------------------------------------------------------------------------

class _CaptureAudit:
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


def _governed_registry(capability_mode: bool = False, allowlist=None):
    """挂载治理能力 code.execute（capability_only Provider）+ fake run_python。"""
    core = _FakeCore()
    reg = ToolRegistry(core, capability_mode=capability_mode,
                       pilot_capability_allowlist=allowlist)
    reg.capability_index.declare_contract(CapabilityContract(
        id=PILOT, version="1.0.0", domain="code", description="test",
        stability=Stability.EXPERIMENTAL, permission=PermissionScopes(exec=True),
    ))
    reg.register(make_code_exec_tool(
        reg, capability_ids=[PILOT], granted_scopes={"exec": True},
        capability_only=True))
    captured: dict = {"calls": []}

    async def _fake_run_python(code, permission_level=LEVEL_USER, **_kw):
        captured["calls"].append(code)
        return f"ran_python:{len(code)}"

    reg.tools["run_python"] = Tool(
        name="run_python", description="exec",
        parameters={"code": {"type": "string"}},
        handler=_fake_run_python, min_level=LEVEL_USER, granted_scopes={"exec": True},
    )
    return reg, core, captured


def _mixed_mode_registry(capability_mode: bool = False, allowlist=None):
    """挂载非治理（mixed-mode）能力：Provider 未置 capability_only。"""
    core = _FakeCore()
    reg = ToolRegistry(core, capability_mode=capability_mode,
                       pilot_capability_allowlist=allowlist)

    def _stub(q: str = "") -> str:
        return f"searched:{q}"

    reg.register(Tool(name="tavily", description="search",
                      parameters={"q": {"type": "string"}},
                      handler=_stub, min_level=LEVEL_USER,
                      capability_ids=["search.web"]))
    return reg, core


def _stub_core(**layer_kwargs) -> object:
    cfg = AnimaConfig.model_validate({"capability_layer": layer_kwargs})

    class _Core:
        config = cfg
    return _Core()


# ==========================================================================
# R3 closure —— schema name 经 call() 路由到 capability
# ==========================================================================

@pytest.mark.asyncio
async def test_da3_schema_name_routes_to_provider_when_permitted():
    """R3 核心：call("code_execute") 在 mode ON + allowlist 命中（主体取自 call context）时路由到 Provider 执行。"""
    reg, core, captured = _governed_registry(capability_mode=True, allowlist=[ALICE])
    tok = reg.set_call_context(user_id="alice", group_id=1, permission_level=LEVEL_USER)
    try:
        out = await reg.call(SCHEMA, {"code": "print(1)"}, LEVEL_USER)
    finally:
        reg.reset_call_context(tok)
    assert "ran_python" in out
    assert captured["calls"] == ["print(1)"]
    assert core.audit.by_type("capability_route_denied") == []


@pytest.mark.asyncio
async def test_da3_normal_tool_dispatch_unchanged():
    """不改变普通 tool dispatch：legacy 名 ``run_python`` 直接走原路径，不经 capability。"""
    reg, core, captured = _governed_registry(capability_mode=True, allowlist=[ALICE])
    out = await reg.call("run_python", {"code": "x"}, LEVEL_USER)
    assert "ran_python" in out
    assert captured["calls"] == ["x"]
    # 没有走 capability 路由（无 deny / 无 governance 事件）
    assert core.audit.by_type("capability_route_denied") == []


@pytest.mark.asyncio
async def test_da3_mode_off_denies_capability_schema_name():
    """总开关是整条路由的闸：mode OFF 时 schema name 也拒绝，绝不执行。"""
    reg, core, captured = _governed_registry(capability_mode=False, allowlist=[ALICE])
    tok = reg.set_call_context(user_id="alice", group_id=1, permission_level=LEVEL_USER)
    try:
        out = await reg.call(SCHEMA, {"code": "x"}, LEVEL_USER)
    finally:
        reg.reset_call_context(tok)
    assert "未获治理放行" in out
    assert core.audit.by_type("capability_route_denied")[0]["reason"] == "capability_mode_off"
    assert captured["calls"] == []


@pytest.mark.asyncio
async def test_da3_allowlist_empty_denies():
    reg, core, captured = _governed_registry(capability_mode=True, allowlist=[])
    tok = reg.set_call_context(user_id="alice", group_id=1, permission_level=LEVEL_USER)
    try:
        out = await reg.call(SCHEMA, {"code": "x"}, LEVEL_USER)
    finally:
        reg.reset_call_context(tok)
    assert "未获治理放行" in out
    assert core.audit.by_type("capability_route_denied")[0]["reason"] == "allowlist_empty"
    assert captured["calls"] == []


@pytest.mark.asyncio
async def test_da3_allowlist_mismatch_denies():
    reg, core, captured = _governed_registry(capability_mode=True, allowlist=[BOB])
    tok = reg.set_call_context(user_id="alice", group_id=1, permission_level=LEVEL_USER)
    try:
        out = await reg.call(SCHEMA, {"code": "x"}, LEVEL_USER)
    finally:
        reg.reset_call_context(tok)
    assert "未获治理放行" in out
    assert core.audit.by_type("capability_route_denied")[0]["reason"] == "allowlist_mismatch"
    assert captured["calls"] == []


@pytest.mark.asyncio
async def test_da3_no_principal_denies():
    """无显式 caller 且 call context 无 user_id → fail-closed 拒绝。"""
    reg, core, captured = _governed_registry(capability_mode=True, allowlist=[ALICE])
    out = await reg.call(SCHEMA, {"code": "x"}, LEVEL_USER)
    assert "未获治理放行" in out
    assert core.audit.by_type("capability_route_denied")[0]["reason"] == "no_principal"
    assert captured["calls"] == []


@pytest.mark.asyncio
async def test_da3_principal_derived_from_call_context():
    """无显式 caller 时主体取自可信 call context（user_id → user:<id>）。"""
    reg, core, captured = _governed_registry(capability_mode=True,
                                             allowlist=["user:42"])
    token = reg.set_call_context(user_id=42, group_id=1, permission_level=LEVEL_USER)
    try:
        out = await reg.call(SCHEMA, {"code": "x"}, LEVEL_USER)
    finally:
        reg.reset_call_context(token)
    assert "ran_python" in out
    assert captured["calls"] == ["x"]


# ==========================================================================
# mixed-mode：仍走 capability 路由，但豁免 allowlist
# ==========================================================================

@pytest.mark.asyncio
async def test_da3_mixed_mode_exempt_from_allowlist():
    """mixed-mode 能力（Provider 非 capability_only）经 schema name 路由，但不受 allowlist 门。"""
    reg, core = _mixed_mode_registry(capability_mode=True, allowlist=[])
    out = await reg.call("search_web", {"q": "x"}, LEVEL_USER)
    assert "searched:x" in out
    # 无 governance 拒绝（allowlist 空也不影响 mixed-mode）
    assert core.audit.by_type("capability_route_denied") == []


@pytest.mark.asyncio
async def test_da3_mixed_mode_mode_off_denies():
    """但总开关仍门 mixed-mode：mode OFF 时 schema name 路由拒绝。"""
    reg, core = _mixed_mode_registry(capability_mode=False)
    out = await reg.call("search_web", {"q": "x"}, LEVEL_USER)
    assert "未获治理放行" in out
    assert core.audit.by_type("capability_route_denied")[0]["reason"] == "capability_mode_off"


@pytest.mark.asyncio
async def test_da3_legacy_mixed_mode_tool_still_direct():
    """legacy 工具名（tavily）不走 capability 路由，照常直调。"""
    reg, core = _mixed_mode_registry(capability_mode=True, allowlist=[])
    out = await reg.call("tavily", {"q": "x"}, LEVEL_USER)
    assert "searched:x" in out
    assert core.audit.by_type("capability_route_denied") == []


# ==========================================================================
# 重入安全：call_capability 内部回 call(provider, _via_capability=True) 不回环
# ==========================================================================

@pytest.mark.asyncio
async def test_da3_no_reentrancy_loop():
    """call_capability 内部 call("code_exec", _via_capability=True) 不应再被当 schema 名回环。"""
    reg, core, captured = _governed_registry(capability_mode=True, allowlist=[ALICE])
    tok = reg.set_call_context(user_id="alice", group_id=1, permission_level=LEVEL_USER)
    try:
        out = await reg.call(SCHEMA, {"code": "x"}, LEVEL_USER)
    finally:
        reg.reset_call_context(tok)
    # provider 执行恰好一次，且未触发任何 capability 拒绝（证明无回环）
    assert captured["calls"] == ["x"]
    assert core.audit.by_type("capability_route_denied") == []


# ==========================================================================
# 真实 registry 集成：create_default_registry 实际挂载 code.execute
# ==========================================================================

@pytest.mark.asyncio
async def test_da3_real_registry_routes_code_execute():
    """真实 wiring：create_default_registry(mode ON) 经 call("code_execute") 命中 Provider。"""
    reg = create_default_registry(
        _stub_core(capability_mode=True, pilot_capability_allowlist=[ALICE]))
    assert "code_exec" in reg.tools, "D-A0 应已挂载 code.execute Provider"
    captured = []

    async def _stub(code, **_kw):
        captured.append(code)
        return f"EXEC:{code}"

    reg.tools["code_exec"].handler = _stub
    tok = reg.set_call_context(user_id="alice", group_id=1, permission_level=LEVEL_USER)
    try:
        out = await reg.call(SCHEMA, {"code": "y"}, LEVEL_USER)
    finally:
        reg.reset_call_context(tok)
    assert captured == ["y"]
    assert "EXEC:y" in out


# ==========================================================================
# 纪律：call 签名不变 + 调用方零改动 + 无 D-A4 遥测
# ==========================================================================

def test_da3_call_signature_unchanged():
    """D-A3 不向 call 增加任何新的必填参数（caller 仍只在 call_capability 注入）。"""
    sig = inspect.signature(ToolRegistry.call)
    params = list(sig.parameters)
    assert params == ["self", "name", "args", "permission_level", "_via_capability"]
    assert sig.parameters["_via_capability"].default is False
    # call 本身不接受 caller（caller 由 call_capability / call context 推导）
    assert "caller" not in sig.parameters


def test_da3_dispatch_sites_unchanged():
    """react / executor / webui 调用方零改动：仍用 tools.call，无 call_capability 引用。

    D-A3 把 R3 路由集中在 ``ToolRegistry.call`` 一处，调用方（含 LLM 分发）不应
    直接感知 capability 路由——否则会出现 F0 同型旁路。
    """
    for fn in ("agent/react.py", "agent/executor.py", "webui/server.py"):
        src = (ANIMA_DIR / fn).read_text(encoding="utf-8")
        assert "self.tools.call(" in src or "core.tools.call(" in src, \
            f"{fn} 应仍经 tools.call 分发"
        assert "call_capability(" not in src, \
            f"{fn} 不应直接引用 call_capability（路由已集中到 call）"


@pytest.mark.asyncio
async def test_da3_no_telemetry_events():
    """审计事件纪律：capability 分发路径只产生 ``call`` / ``call_capability`` / ``_invoke``
    既有的审计事件类型。D-A4 接入的是指标遥测（``MetricsCollector``），**不**新增审计
    事件名——审计（取责）与遥测（可观测）在此分流、互不污染。"""
    reg, core, captured = _governed_registry(capability_mode=True, allowlist=[ALICE])
    # 一次成功分发（alice 命中）+ 一次拒绝分发（bob 不匹配）
    tok_a = reg.set_call_context(user_id="alice", group_id=1, permission_level=LEVEL_USER)
    try:
        await reg.call(SCHEMA, {"code": "x"}, LEVEL_USER)
    finally:
        reg.reset_call_context(tok_a)
    tok_b = reg.set_call_context(user_id="bob", group_id=1, permission_level=LEVEL_USER)
    try:
        await reg.call(SCHEMA, {"code": "x"}, LEVEL_USER)
    finally:
        reg.reset_call_context(tok_b)
    # 这些事件均为 call / call_capability / _invoke 既有审计（D-A1 之前即存在），
    # D-A3 仅把 schema name 转发给 call_capability，不引入新事件。
    known = {
        "capability_route_denied", "execution_denied", "tool_denied",
        "tool_approval_requested", "tool_approval_execution_failed",
        "tool_approved_executed", "tool_approval_rejected",
        "tool_failed", "execution_started", "execution_failed",
        "tool_executed", "execution_finished",
    }
    for etype, _ in core.audit.events:
        assert etype in known, f"新增遥测事件 {etype!r} 违反 D-A4 纪律"
