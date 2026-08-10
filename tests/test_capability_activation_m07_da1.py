"""M0.4.7 D-A1 —— Activation Config / Gating（默认 OFF，未激活）。

本文件钉死 D-A1 四项裁定：

- **R1** ``capability_layer:`` 独立 config section，与 ``capabilities:``
  （个人能力自生长系统 P1）语义隔离，不复用不扩展；
- **R2** allowlist **fail-closed**：空 allowlist 拒绝全部治理能力路由（不同于
  sticker / exploration 的 fail-open 范式），且不支持任何通配符；
- **R4** D-A1 只做 config schema + 默认 OFF + 构造参数，不碰 ``_sync_runtime``、
  不做 registry 热更（kill switch 语义 = 受控重启生效）；
- **R6** schema 暴露面 gate：``capability_mode=True`` ≠ 所有 capability schema 自动
  公开，治理能力还须 allowlist 放行::

      schema visible = capability_mode ∧ provider available ∧ allowlist permitted

纪律：本文件**不**翻转生产默认值、**不**接 LLM 分发（react / executor 保持零引用）、
**不**加遥测、**不**改 legacy execution policy / sandbox tier / scope。
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest
import yaml

from anima.capability import CapabilityContract, PermissionScopes
from anima.contracts.capability import Stability
from anima.core.config import AnimaConfig, CapabilityLayerConfig, load_config
from anima.tools import (
    LEVEL_USER,
    Tool,
    ToolRegistry,
    create_default_registry,
)
from anima.tools.builtin import register_all_builtin
from anima.tools.builtin.code_exec import make_code_exec_tool

PILOT = "code.execute"
ALICE = "user:alice"
BOB = "user:bob"


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

    reg.register(Tool(name="tavily", description="search", parameters={"q": {"type": "string"}},
                      handler=_stub, min_level=LEVEL_USER, capability_ids=["search.web"]))
    return reg, core


# ==========================================================================
# R1 —— Config section 隔离
# ==========================================================================

def test_da1_capability_layer_section_defaults_off():
    cfg = CapabilityLayerConfig()
    assert cfg.capability_mode is False
    assert cfg.pilot_capability_allowlist == []


def test_da1_capability_layer_mounted_on_root_config():
    cfg = AnimaConfig()
    assert isinstance(cfg.capability_layer, CapabilityLayerConfig)
    assert cfg.capability_layer.capability_mode is False
    assert cfg.capability_layer.pilot_capability_allowlist == []


def test_da1_capability_layer_is_isolated_from_self_growing_capabilities():
    """R1：治理层 ``capability_layer`` 与自生长库 ``capabilities`` 完全隔离。"""
    cfg = AnimaConfig()
    assert cfg.capability_layer is not cfg.capabilities
    assert type(cfg.capability_layer) is not type(cfg.capabilities)
    # 治理 section 不得混入自生长字段，反之亦然
    layer_fields = set(CapabilityLayerConfig.model_fields)
    # F9.2：新增五个观测面开关（默认全 OFF）——仍全部属于治理域，与自生长库无交集。
    assert layer_fields == {
        "capability_mode", "pilot_capability_allowlist",
        "capability_gateway_mode", "governance_audit_enabled",
        "health_evidence_enabled", "transition_audit_enabled",
        "governance_data_dir",
    }
    growth_fields = set(type(cfg.capabilities).model_fields)
    assert not (layer_fields & growth_fields), "治理层与自生长库字段不得重叠"
    # 改一边不影响另一边
    cfg.capability_layer.capability_mode = True
    assert cfg.capabilities.enabled is True


def test_da1_capability_layer_loads_from_yaml(tmp_path: Path):
    path = tmp_path / "anima.yaml"
    path.write_text(yaml.safe_dump({
        "capability_layer": {"capability_mode": True,
                             "pilot_capability_allowlist": [ALICE]},
    }), encoding="utf-8")
    cfg = load_config(path)
    assert cfg.capability_layer.capability_mode is True
    assert cfg.capability_layer.pilot_capability_allowlist == [ALICE]
    # 缺省文件 → 仍是 OFF
    assert load_config(tmp_path / "missing.yaml").capability_layer.capability_mode is False


def test_da1_config_documents_controlled_restart():
    """R4：kill switch 语义（受控重启生效）必须写进 docstring，避免误判热生效。"""
    doc = CapabilityLayerConfig.__doc__ or ""
    assert "controlled restart" in doc
    assert "Not related to self-growing capability library" in doc


# ==========================================================================
# 构造链 —— 默认值保持现状
# ==========================================================================

def test_da1_registry_defaults_unchanged():
    reg = ToolRegistry()
    assert reg.capability_mode is False
    assert reg.pilot_capability_allowlist == ()


def test_da1_create_default_registry_defaults_unchanged():
    reg = create_default_registry()
    assert reg.capability_mode is False
    assert reg.pilot_capability_allowlist == ()


def test_da1_create_default_registry_accepts_activation_params():
    sig = inspect.signature(create_default_registry)
    # F8.6：加性扩展（capability_gateway_mode / governance_audit_sink）——受治理 pilot
    # 路径的 feature gate 与 audit sink 接线。既有三参数的顺序与默认值不变；
    # 新参数一律默认 None（未显式提供 → 从 config / core 读取，缺省 OFF + 无 sink）。
    # F8.10.1 C1：再加性扩展 health_evidence_sink（默认 None = 不发射 evidence，
    # 行为零变化）。
    assert list(sig.parameters) == [
        "core", "capability_mode", "pilot_capability_allowlist",
        "capability_gateway_mode", "governance_audit_sink", "health_evidence_sink",
    ]
    # D-A2：默认 None 表示「未显式提供 → 从 core.config.capability_layer 读取」；
    # 显式传入的值优先（replay 用 False 强制 OFF fallback）。
    assert sig.parameters["capability_mode"].default is None
    assert sig.parameters["pilot_capability_allowlist"].default is None
    assert sig.parameters["capability_gateway_mode"].default is None
    assert sig.parameters["governance_audit_sink"].default is None
    assert sig.parameters["health_evidence_sink"].default is None
    reg = create_default_registry(None, capability_mode=True,
                                  pilot_capability_allowlist=[ALICE])
    assert reg.capability_mode is True
    assert reg.pilot_capability_allowlist == (ALICE,)


def test_da2_create_default_registry_reads_config_layer():
    """D-A2 接线：未显式提供时从 ``core.config.capability_layer`` 读取（D-A1 的
    「不读 config」边界已在 D-A2 被有意反转——本测试钉死新契约）。

    用最小 stub core（仅暴露 ``.config``）即可，无需装配完整 AnimaCore。
    """
    cfg_on = AnimaConfig.model_validate({
        "capability_layer": {"capability_mode": True, "pilot_capability_allowlist": [ALICE]},
    })

    class _Core:
        config = cfg_on

    reg = create_default_registry(_Core())
    assert reg.capability_mode is True
    assert reg.pilot_capability_allowlist == (ALICE,)
    # 显式覆盖仍优先：即便 config ON，传 False 也要被尊重（replay fallback 同源）。
    reg_off = create_default_registry(_Core(), capability_mode=False,
                                      pilot_capability_allowlist=[])
    assert reg_off.capability_mode is False
    assert reg_off.pilot_capability_allowlist == ()


def test_da1_allowlist_normalization_and_no_wildcard():
    """R2：归一（trim / lower / 去重 / 丢空）；通配符不是特权，只是一个普通字符串。"""
    reg = ToolRegistry(pilot_capability_allowlist=["  User:Alice ", "user:alice", "", "  ", BOB])
    assert reg.pilot_capability_allowlist == (ALICE, BOB)
    star = ToolRegistry(pilot_capability_allowlist=["*"])
    assert star.pilot_capability_permitted(ALICE) is False, "通配符不得放行任意主体"
    assert star.pilot_capability_permitted("*") is True   # 仅字面量自身匹配
    # setter 同样归一
    reg.pilot_capability_allowlist = [" USER:Carol "]
    assert reg.pilot_capability_allowlist == ("user:carol",)


# ==========================================================================
# Default OFF regression
# ==========================================================================

def test_da1_default_off_legacy_schema_unchanged():
    reg = create_default_registry()
    assert reg.get_schemas(LEVEL_USER) == [
        t.to_openai_schema() for t in reg.tools.values()
        if t.enabled and LEVEL_USER >= t.min_level and not t.capability_only
    ]


def test_da1_default_off_mixed_mode_tools_untouched():
    reg = create_default_registry()
    names = [s["function"]["name"] for s in reg.get_schemas(LEVEL_USER)]
    for name in ("web_search", "read_file", "write_file"):
        assert name in names, f"mixed-mode 工具 {name} 不得被 D-A1 波及"
        assert reg.tools[name].capability_only is False


def test_da1_default_off_code_exec_stays_capability_only():
    reg = ToolRegistry()
    register_all_builtin(reg, None)
    assert reg.tools["code_exec"].capability_only is True
    names = [s["function"]["name"] for s in reg.get_schemas(LEVEL_USER)]
    assert "code_exec" not in names and "code_execute" not in names


@pytest.mark.asyncio
async def test_da1_default_off_capability_route_denied_with_audit():
    """capability_mode=False → 整条 capability 路由拒绝，且不静默。"""
    reg, core, captured = _governed_registry(capability_mode=False, allowlist=[ALICE])
    out = await reg.call_capability(PILOT, {"code": "print(1)"},
                                    permission_level=LEVEL_USER, caller=ALICE)
    assert "未获治理放行" in out
    denied = core.audit.by_type("capability_route_denied")
    assert denied and denied[0]["reason"] == "capability_mode_off"
    assert denied[0]["capability"] == PILOT
    assert captured["calls"] == [], "未激活时绝不可委托执行"


@pytest.mark.asyncio
async def test_da1_default_off_denies_even_mixed_mode_route():
    """总开关是整条路由的闸：未激活时非治理能力同样不可经 capability 入口调用。

    （legacy 直调路径不受影响，见上方 mixed-mode 回归。）
    """
    reg, core = _mixed_mode_registry(capability_mode=False)
    out = await reg.call_capability("search.web", {"q": "x"}, LEVEL_USER, caller=ALICE)
    assert "未获治理放行" in out
    assert core.audit.by_type("capability_route_denied")[0]["reason"] == "capability_mode_off"
    # legacy 直调仍照常（mixed-mode 行为不变）
    assert "searched:x" in await reg.call("tavily", {"q": "x"}, LEVEL_USER)


# ==========================================================================
# R2 —— Allowlist（fail-closed）
# ==========================================================================

@pytest.mark.asyncio
async def test_da1_allowlist_empty_denies():
    reg, core, captured = _governed_registry(capability_mode=True, allowlist=[])
    out = await reg.call_capability(PILOT, {"code": "print(1)"}, LEVEL_USER, caller=ALICE)
    assert "未获治理放行" in out
    assert core.audit.by_type("capability_route_denied")[0]["reason"] == "allowlist_empty"
    assert captured["calls"] == []


@pytest.mark.asyncio
async def test_da1_allowlist_match_allows():
    reg, core, captured = _governed_registry(capability_mode=True, allowlist=[ALICE])
    out = await reg.call_capability(PILOT, {"code": "print(1)"}, LEVEL_USER, caller=ALICE)
    assert "ran_python" in out
    assert core.audit.by_type("capability_route_denied") == []
    assert captured["calls"] == ["print(1)"]


@pytest.mark.asyncio
async def test_da1_allowlist_mismatch_denies():
    reg, core, captured = _governed_registry(capability_mode=True, allowlist=[BOB])
    out = await reg.call_capability(PILOT, {"code": "print(1)"}, LEVEL_USER, caller=ALICE)
    assert "未获治理放行" in out
    assert core.audit.by_type("capability_route_denied")[0]["reason"] == "allowlist_mismatch"
    assert captured["calls"] == []


@pytest.mark.asyncio
async def test_da1_no_principal_denies():
    """无主体（既无显式 caller 又无 call context）→ fail-closed 拒绝。"""
    reg, core, captured = _governed_registry(capability_mode=True, allowlist=[ALICE])
    out = await reg.call_capability(PILOT, {"code": "print(1)"}, LEVEL_USER)
    assert "未获治理放行" in out
    assert core.audit.by_type("capability_route_denied")[0]["reason"] == "no_principal"
    assert captured["calls"] == []


@pytest.mark.asyncio
async def test_da1_principal_derived_from_call_context():
    """无显式 caller 时主体取自可信 call context（``user_id`` → ``user:<id>``）。"""
    reg, core, captured = _governed_registry(capability_mode=True,
                                             allowlist=["user:42"])
    token = reg.set_call_context(user_id=42, group_id=1, permission_level=LEVEL_USER)
    try:
        out = await reg.call_capability(PILOT, {"code": "print(1)"}, LEVEL_USER)
    finally:
        reg.reset_call_context(token)
    assert "ran_python" in out

    reg2, core2, captured2 = _governed_registry(capability_mode=True, allowlist=["user:42"])
    token = reg2.set_call_context(user_id=43, group_id=1, permission_level=LEVEL_USER)
    try:
        out2 = await reg2.call_capability(PILOT, {"code": "print(1)"}, LEVEL_USER)
    finally:
        reg2.reset_call_context(token)
    assert "未获治理放行" in out2
    assert core2.audit.by_type("capability_route_denied")[0]["reason"] == "allowlist_mismatch"
    assert captured2["calls"] == []


@pytest.mark.asyncio
async def test_da1_allowlist_does_not_gate_mixed_mode_capability():
    """allowlist 只治理 capability-only 能力；mixed-mode 能力激活后行为不变。"""
    reg, core = _mixed_mode_registry(capability_mode=True, allowlist=[])
    out = await reg.call_capability("search.web", {"q": "x"}, LEVEL_USER)
    assert "searched:x" in out
    assert core.audit.by_type("capability_route_denied") == []


# ==========================================================================
# R6 —— Schema 暴露面 gate
# ==========================================================================

def test_da1_schema_off_hides_all_capability_schema():
    reg, _, _ = _governed_registry(capability_mode=False, allowlist=[ALICE])
    names = [s["function"]["name"] for s in reg.get_schemas(LEVEL_USER, caller=ALICE)]
    assert "code_execute" not in names
    assert "code_exec" not in names, "capability-only 工具不得回落到 legacy 表面"


def test_da1_schema_on_empty_allowlist_hides_governed_capability():
    reg, _, _ = _governed_registry(capability_mode=True, allowlist=[])
    names = [s["function"]["name"] for s in reg.get_schemas(LEVEL_USER, caller=ALICE)]
    assert "code_execute" not in names, "R6：allowlist 未放行时不得泄露治理能力名"


def test_da1_schema_on_mismatched_caller_hides_governed_capability():
    reg, _, _ = _governed_registry(capability_mode=True, allowlist=[BOB])
    names = [s["function"]["name"] for s in reg.get_schemas(LEVEL_USER, caller=ALICE)]
    assert "code_execute" not in names


def test_da1_schema_on_allowlisted_caller_exposes_governed_capability():
    reg, _, _ = _governed_registry(capability_mode=True, allowlist=[ALICE])
    names = [s["function"]["name"] for s in reg.get_schemas(LEVEL_USER, caller=ALICE)]
    assert "code_execute" in names
    assert "code_exec" not in names, "Provider 名永不暴露"


def test_da1_schema_gate_does_not_hide_mixed_mode_capability():
    """R6 只作用于治理能力：mixed-mode capability schema 不受 allowlist 影响。"""
    reg, _ = _mixed_mode_registry(capability_mode=True, allowlist=[])
    names = [s["function"]["name"] for s in reg.get_schemas(LEVEL_USER)]
    assert "search_web" in names
    assert "tavily" not in names


def test_da1_schema_gate_on_real_wired_registry():
    """真实接线下的三态（OFF / ON+empty / ON+allow），mixed-mode 全程不受损。"""
    off = ToolRegistry()
    register_all_builtin(off, None)
    off_names = [s["function"]["name"] for s in off.get_schemas(LEVEL_USER)]
    assert "code_execute" not in off_names and "code_exec" not in off_names

    empty = ToolRegistry(capability_mode=True)
    register_all_builtin(empty, None)
    empty_names = [s["function"]["name"] for s in empty.get_schemas(LEVEL_USER, caller=ALICE)]
    assert "code_execute" not in empty_names
    assert "search_web" in empty_names and "filesystem_read" in empty_names

    allowed = ToolRegistry(capability_mode=True, pilot_capability_allowlist=[ALICE])
    register_all_builtin(allowed, None)
    allowed_names = [s["function"]["name"]
                     for s in allowed.get_schemas(LEVEL_USER, caller=ALICE)]
    assert "code_execute" in allowed_names
    assert "search_web" in allowed_names and "filesystem_read" in allowed_names


# ==========================================================================
# Scope 纪律护栏（D-A1 不越界）
# ==========================================================================

def test_da1_llm_dispatch_not_wired():
    """禁止接 LLM：react / executor 保持零 capability 引用（D-A3 单独处理）。"""
    import anima.agent.executor as executor_mod
    import anima.agent.react as react_mod

    for mod in (react_mod, executor_mod):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "call_capability" not in src, f"{mod.__name__} 不得在 D-A1 接入 capability 路由"
        assert "capability_mode" not in src


def test_da1_no_runtime_hot_reload_of_registry():
    """R4：``_sync_runtime`` 不得触碰 registry / capability 状态。"""
    from anima.core.config_service import ConfigService

    src = inspect.getsource(ConfigService._sync_runtime)
    for token in ("capability_mode", "pilot_capability_allowlist", "capability_layer", "tools"):
        assert token not in src, f"_sync_runtime 不得热更 {token}（D-A1 R4 明确不做）"


def test_da1_no_telemetry_events_added():
    """禁止遥测：D-A4 的三个事件名在 D-A1 不得出现。"""
    src = Path(inspect.getfile(ToolRegistry)).read_text(encoding="utf-8")
    for event in ("capability_attempt", "capability_executed", '"capability_denied"'):
        assert event not in src, f"{event} 属 D-A4 遥测，D-A1 不得提前引入"


def test_da1_call_signature_still_locked():
    """legacy 执行入口 ABI 不变（D-A0 锁定的形状保持）。"""
    sig = inspect.signature(ToolRegistry.call)
    params = list(sig.parameters)
    assert params[:4] == ["self", "name", "args", "permission_level"]
    assert params[4] == "_via_capability"
    assert sig.parameters["_via_capability"].kind is inspect.Parameter.KEYWORD_ONLY


def test_da1_gate_lives_in_call_capability_not_in_caller():
    """治理门必须落在唯一入口（call_capability），避免 F0 同型的调用方旁路。"""
    src = inspect.getsource(ToolRegistry.call_capability)
    assert "self.capability_mode" in src
    assert "pilot_capability_permitted" in src
