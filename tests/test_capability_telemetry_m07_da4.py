"""M0.4.7 D-A4 —— Capability Routing Telemetry（可观测性）。

本文件钉死 D-A4 裁定：

- **区分四态**：capability 路由按 ``denied`` / ``allowed`` / ``routed`` / ``failed``
  四类产出可观测指标，进入进程级 ``MetricsCollector``（WebUI ``/api/metrics`` 可见）。
- **与审计分流**：审计（``capability_route_denied``）负责取责/取证，遥测（指标计数器）
  负责可观测；D-A4 只新增指标、不新增审计事件类型。
- **不改执行语义**：指标为旁路计数，返回值与异常行为逐字节不变。

纪律（D-A4 不允许）：不翻 ``capability_mode``、不添加 pilot cohort、不改变 allowlist、
不修改 ``run_python`` / ``run_shell``、不改变 sandbox policy、不做 rollout。
"""
from __future__ import annotations

import pytest

from anima.capability import CapabilityContract, PermissionScopes
from anima.contracts.capability import Stability
from anima.core.config import AnimaConfig
from anima.core.metrics import get_metrics
from anima.tools import LEVEL_USER, Tool, ToolRegistry, create_default_registry
from anima.tools.builtin.code_exec import make_code_exec_tool
from anima.tools import _SAFE_TOOL_FAILURE

PILOT = "code.execute"          # capability id（真实挂载值）
SCHEMA = "code_execute"         # LLM function name = schema_name_for("code.execute")
ALICE = "user:alice"
BOB = "user:bob"


# --------------------------------------------------------------------------
# 脚手架（自包含，不跨文件依赖 D-A3）
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


@pytest.fixture(autouse=True)
def _reset_metrics():
    """MetricsCollector 是进程级单例，测试间必须清空，避免计数污染。"""
    get_metrics().reset()
    yield
    get_metrics().reset()


def _governed_registry(capability_mode: bool = False, allowlist=None):
    """挂载治理能力 code.execute（capability_only Provider）。"""
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


def _stub_core(**layer_kwargs) -> object:
    cfg = AnimaConfig.model_validate({"capability_layer": layer_kwargs})

    class _Core:
        config = cfg
    return _Core()


def _boom_handler(code, **_kw):
    """注入的必然失败 handler（抛异常路径，_invoke 捕获后返回 _SAFE_TOOL_FAILURE）。"""
    raise RuntimeError("injected capability failure")


M = lambda name, **tags: get_metrics().get_counter(name, tags=tags or None)


# ==========================================================================
# denied —— 治理门拒绝即计数（reason 分流）
# ==========================================================================

@pytest.mark.asyncio
async def test_da4_mode_off_denies_and_counts():
    reg, core, _ = _governed_registry(capability_mode=False, allowlist=[ALICE])
    tok = reg.set_call_context(user_id="alice", group_id=1, permission_level=LEVEL_USER)
    try:
        out = await reg.call(SCHEMA, {"code": "x"}, LEVEL_USER)
    finally:
        reg.reset_call_context(tok)
    assert "未获治理放行" in out
    assert M("capability.route.denied", capability=PILOT, reason="capability_mode_off") == 1
    # 未通过门 → 无 allowed / routed / failed
    assert M("capability.route.allowed", capability=PILOT) == 0
    assert M("capability.route.routed", capability=PILOT) == 0
    assert M("capability.route.failed", capability=PILOT) == 0
    # 审计仍发出（与遥测分流但并存）
    assert core.audit.by_type("capability_route_denied")[0]["reason"] == "capability_mode_off"


@pytest.mark.asyncio
async def test_da4_allowlist_empty_denies_and_counts():
    reg, core, _ = _governed_registry(capability_mode=True, allowlist=[])
    tok = reg.set_call_context(user_id="alice", group_id=1, permission_level=LEVEL_USER)
    try:
        await reg.call(SCHEMA, {"code": "x"}, LEVEL_USER)
    finally:
        reg.reset_call_context(tok)
    assert M("capability.route.denied", capability=PILOT, reason="allowlist_empty") == 1


@pytest.mark.asyncio
async def test_da4_allowlist_mismatch_denies_and_counts():
    reg, core, _ = _governed_registry(capability_mode=True, allowlist=[BOB])
    tok = reg.set_call_context(user_id="alice", group_id=1, permission_level=LEVEL_USER)
    try:
        await reg.call(SCHEMA, {"code": "x"}, LEVEL_USER)
    finally:
        reg.reset_call_context(tok)
    assert M("capability.route.denied", capability=PILOT, reason="allowlist_mismatch") == 1


@pytest.mark.asyncio
async def test_da4_no_principal_denies_and_counts():
    reg, core, _ = _governed_registry(capability_mode=True, allowlist=[ALICE])
    await reg.call(SCHEMA, {"code": "x"}, LEVEL_USER)  # 无 call context → 无主体
    assert M("capability.route.denied", capability=PILOT, reason="no_principal") == 1


# ==========================================================================
# allowed / routed —— 治理门通过即计数（无论 Provider 是否随后失败）
# ==========================================================================

@pytest.mark.asyncio
async def test_da4_success_counts_allowed_and_routed():
    reg, core, captured = _governed_registry(capability_mode=True, allowlist=[ALICE])
    tok = reg.set_call_context(user_id="alice", group_id=1, permission_level=LEVEL_USER)
    try:
        out = await reg.call(SCHEMA, {"code": "x"}, LEVEL_USER)
    finally:
        reg.reset_call_context(tok)
    assert "ran_python" in out
    assert M("capability.route.allowed", capability=PILOT) == 1
    assert M("capability.route.routed", capability=PILOT) == 1
    # 成功执行 → 无 failed
    assert M("capability.route.failed", capability=PILOT) == 0
    assert captured["calls"] == ["x"]


@pytest.mark.asyncio
async def test_da4_failure_counts_failed():
    """Provider 执行抛出异常 → _invoke 捕获后返回 _SAFE_TOOL_FAILURE；
    allowed + routed + failed 三态齐全，返回值逐字节不变。"""
    reg, core, _ = _governed_registry(capability_mode=True, allowlist=[ALICE])
    reg.tools["code_exec"].handler = _boom_handler

    tok = reg.set_call_context(user_id="alice", group_id=1, permission_level=LEVEL_USER)
    try:
        out = await reg.call(SCHEMA, {"code": "x"}, LEVEL_USER)
    finally:
        reg.reset_call_context(tok)
    assert out == _SAFE_TOOL_FAILURE
    assert M("capability.route.allowed", capability=PILOT) == 1
    assert M("capability.route.routed", capability=PILOT) == 1
    assert M("capability.route.failed", capability=PILOT) == 1


@pytest.mark.asyncio
async def test_da4_pending_approval_not_counted_failed():
    """等待主人审批（非错误、非失败）route 成功、不记 failed。"""
    reg, core, _ = _governed_registry(capability_mode=True, allowlist=[ALICE])
    reg.tools["code_exec"].handler = lambda code, **_kw: "等待主人审批: id=1"
    tok = reg.set_call_context(user_id="alice", group_id=1, permission_level=LEVEL_USER)
    try:
        out = await reg.call(SCHEMA, {"code": "x"}, LEVEL_USER)
    finally:
        reg.reset_call_context(tok)
    assert "等待主人审批" in out
    assert M("capability.route.allowed", capability=PILOT) == 1
    assert M("capability.route.routed", capability=PILOT) == 1
    assert M("capability.route.failed", capability=PILOT) == 0


# ==========================================================================
# mixed-mode —— 也走 capability 路由，计 allowed/routed（非 denied）
# ==========================================================================

@pytest.mark.asyncio
async def test_da4_mixed_mode_counts_allowed_routed():
    core = _FakeCore()
    reg = ToolRegistry(core, capability_mode=True, pilot_capability_allowlist=[])

    def _stub(q: str = "") -> str:
        return f"searched:{q}"

    reg.register(Tool(name="tavily", description="search",
                      parameters={"q": {"type": "string"}},
                      handler=_stub, min_level=LEVEL_USER,
                      capability_ids=["search.web"]))
    out = await reg.call("search_web", {"q": "x"}, LEVEL_USER)
    assert "searched:x" in out
    assert M("capability.route.allowed", capability="search.web") == 1
    assert M("capability.route.routed", capability="search.web") == 1
    # mixed-mode 不受 allowlist，从不 deny
    assert M("capability.route.denied", capability="search.web") == 0


# ==========================================================================
# 真实 wiring：create_default_registry 也产出指标
# ==========================================================================

@pytest.mark.asyncio
async def test_da4_real_registry_emits_telemetry():
    reg = create_default_registry(
        _stub_core(capability_mode=True, pilot_capability_allowlist=[ALICE]))
    assert "code_exec" in reg.tools
    reg.tools["code_exec"].handler = lambda code, **_kw: f"EXEC:{code}"
    tok = reg.set_call_context(user_id="alice", group_id=1, permission_level=LEVEL_USER)
    try:
        out = await reg.call(SCHEMA, {"code": "y"}, LEVEL_USER)
    finally:
        reg.reset_call_context(tok)
    assert "EXEC:y" in out
    assert M("capability.route.allowed", capability=PILOT) == 1
    assert M("capability.route.routed", capability=PILOT) == 1
    assert M("capability.route.failed", capability=PILOT) == 0


# ==========================================================================
# 指标进入 WebUI 可观测面（/api/metrics 用的 snapshot）
# ==========================================================================

@pytest.mark.asyncio
async def test_da4_metrics_exposed_in_snapshot():
    reg, core, _ = _governed_registry(capability_mode=True, allowlist=[ALICE])
    tok = reg.set_call_context(user_id="alice", group_id=1, permission_level=LEVEL_USER)
    try:
        await reg.call(SCHEMA, {"code": "x"}, LEVEL_USER)
    finally:
        reg.reset_call_context(tok)
    snap = get_metrics().snapshot()
    counters = snap["counters"]
    assert any(k.startswith("capability.route.allowed") for k in counters)
    assert any(k.startswith("capability.route.routed") for k in counters)
    # 失败的 key 不应出现（本次成功）
    assert not any(k.startswith("capability.route.failed") for k in counters)
    # tag 拼接到 key 中，便于按 capability / reason 下钻
    assert any(f"capability={PILOT}" in k for k in counters
               if k.startswith("capability.route."))


# ==========================================================================
# 纪律：D-A4 不引入新审计事件类型、不翻 flag、不改签名
# ==========================================================================

@pytest.mark.asyncio
async def test_da4_no_new_audit_event_types():
    """D-A4 仅新增指标计数；capability 路由路径不引入任何新审计事件名。"""
    reg, core, _ = _governed_registry(capability_mode=True, allowlist=[ALICE])
    tok = reg.set_call_context(user_id="alice", group_id=1, permission_level=LEVEL_USER)
    try:
        await reg.call(SCHEMA, {"code": "x"}, LEVEL_USER)
    finally:
        reg.reset_call_context(tok)
    known = {
        "capability_route_denied", "execution_denied", "tool_denied",
        "tool_failed", "tool_approval_requested", "execution_started",
        "execution_finished", "tool_executed", "execution_failed",
    }
    for etype, _ in core.audit.events:
        assert etype in known, f"D-A4 引入新审计事件 {etype!r}（应为纯指标）"


def test_da4_call_signature_unchanged():
    """D-A4 不向 call / call_capability 增加破坏性参数。"""
    import inspect
    csig = inspect.signature(ToolRegistry.call)
    assert list(csig.parameters) == ["self", "name", "args", "permission_level", "_via_capability"]
    psig = inspect.signature(ToolRegistry.call_capability)
    assert list(psig.parameters) == ["self", "capability_ref", "args", "permission_level", "caller"]


def test_da4_default_config_off():
    """默认配置 capability_mode=False、allowlist 空（D-A4 不翻 flag）。"""
    cfg = AnimaConfig.model_validate({})
    assert cfg.capability_layer.capability_mode is False
    assert cfg.capability_layer.pilot_capability_allowlist == []
