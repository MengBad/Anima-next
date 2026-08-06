"""M0.4.6 D2 —— `code.execute` Provider Adapter 测试（spec 范围，不挂载 capability）。

验证 D2 交付的 adapter（anima/tools/builtin/code_exec.py）行为：

- schema allowlist 拒绝未知字段（契约 v0.2 §6.1：拒绝而非静默丢弃）；
- 委托执行经 ``registry.call("run_python", ...)`` 复用已硬化基座；
- 审计透明：外层 ``code_exec`` 不产生 ``execution_*`` 审计（无双审计）；
- 无特权影响：绝不读取 args 中的 permission_level，只信可信 ctx。

纪律：本文件**不**挂载 capability、**不**激活 capability_mode。测试中若向 registry
注册 code_exec Tool，其 ``capability_ids`` 显式为 ``[]`` —— 不构成 D3 provider 挂载
（``providers_for("code.execute")`` 仍为空）。
"""

import asyncio
import pytest

from anima.tools import LEVEL_USER, Tool, ToolRegistry
from anima.tools.builtin.code_exec import make_code_exec_tool, CodeExecSchemaViolation


class _CaptureAudit:
    """捕获 emit 的事件类型与字段，供断言使用。"""

    def __init__(self):
        self.events = []

    def emit(self, event_type: str, **fields):
        self.events.append((event_type, fields))

    def types(self):
        return [e for e, _ in self.events]


class _FakeCore:
    def __init__(self):
        self.audit = _CaptureAudit()
        self.config = None


def _registry_with_fake_run_python():
    """构造带 fake run_python 的 registry（不依赖真实沙箱，确定性）。

    返回 (reg, core, captured)，其中 captured["calls"] 记录对 run_python 的委托调用。
    """
    core = _FakeCore()
    reg = ToolRegistry(core)

    captured = {"calls": []}

    async def _fake_run_python(code, permission_level=LEVEL_USER, **_kw):
        captured["calls"].append({"code": code, "permission_level": permission_level})
        return f"ran_python:{len(code)}"

    reg.tools["run_python"] = Tool(
        name="run_python",
        description="exec",
        parameters={"code": {"type": "string"}},
        handler=_fake_run_python,
        # 镜像真实 run_python 的 exec scope 授权，使执行门控真实生效
        granted_scopes={"exec": True},
    )
    return reg, core, captured


def test_d2_tool_is_not_capability_bound():
    """D2 边界：adapter 创建时 capability_ids 必须为空（那是 D3 的职责）。"""
    reg = ToolRegistry(_FakeCore())
    tool = make_code_exec_tool(reg)
    assert tool.capability_ids == [], "D2 不得绑定 capability（那是 D3）"
    assert tool.name == "code_exec"
    assert tool.granted_scopes is not None  # 默认空 scope 仍可构造


def test_d2_schema_rejects_unknown_field_and_does_not_delegate():
    """schema allowlist：传入未授权字段（如 permission_level）被拒绝并审计，且不委托。"""
    reg, core, captured = _registry_with_fake_run_python()
    tool = make_code_exec_tool(reg)
    token = reg.set_call_context(user_id=1, group_id=10, permission_level=LEVEL_USER)
    try:
        with pytest.raises(CodeExecSchemaViolation):
            asyncio.run(tool.handler(code="print(1)", permission_level=4))
    finally:
        reg.reset_call_context(token)

    denied = [f for evt, f in core.audit.events if evt == "execution_denied"]
    assert denied, "schema 违反必须产生 execution_denied"
    assert any(f.get("reason") == "schema_violation" for f in denied)
    # 拒绝发生在委托之前：run_python 绝不应被调用
    assert captured["calls"] == [], "schema 违反时绝不应委托到 run_python"


@pytest.mark.asyncio
async def test_d2_delegate_to_run_python_with_trusted_level():
    """委托路径：clean args 经 registry.call('run_python', ...) 复用已硬化基座，
    且 code 原样转发、permission_level 取自可信 ctx（无特权提升）。"""
    reg, core, captured = _registry_with_fake_run_python()
    tool = make_code_exec_tool(reg)
    # 测试脚手架注册（capability_ids=[]，不构成 D3 provider 挂载）
    reg.register(tool)

    token = reg.set_call_context(user_id=123, group_id=55, permission_level=2)
    try:
        out = await reg.call("code_exec", {"code": "print('hi')"}, permission_level=2)
    finally:
        reg.reset_call_context(token)

    assert "ran_python" in out
    # 委托发生，且 code 原样转发（无额外字段），permission_level 取自可信 ctx
    assert captured["calls"] == [{"code": "print('hi')", "permission_level": 2}], \
        f"委托参数不符合预期: {captured['calls']}"
    # 审计透明：仅 run_python 产生 execution_*，且 tool 字段为 run_python（无双审计）
    types = core.audit.types()
    assert types.count("execution_started") == 1, types
    assert types.count("execution_finished") == 1, types
    started = [f for evt, f in core.audit.events if evt == "execution_started"][0]
    assert started["tool"] == "run_python", "外层 code_exec 不应产生 execution 审计"


@pytest.mark.asyncio
async def test_d2_register_does_not_mount_as_provider():
    """D2：注册（测试脚手架）不得使其成为 code.execute 的 Provider（那是 D3）。"""
    reg, core, _ = _registry_with_fake_run_python()
    tool = make_code_exec_tool(reg)
    reg.register(tool)
    assert reg.capability_index.providers_for("code.execute") == [], \
        "D2 注册不得挂载为 capability provider（那是 D3）"


@pytest.mark.asyncio
async def test_d2_injected_permission_level_rejected_at_boundary():
    """无特权影响：普通 ctx(2) 但 args 注入 permission_level=4 时，注入字段在 schema
    边界被拒，run_python 不被调用（无法借注入提升沙箱层级）。"""
    reg, core, captured = _registry_with_fake_run_python()
    tool = make_code_exec_tool(reg)
    reg.register(tool)

    token = reg.set_call_context(user_id=123, group_id=55, permission_level=2)
    try:
        out = await reg.call(
            "code_exec",
            {"code": "print(1)", "permission_level": 4},
            permission_level=2,
        )
    finally:
        reg.reset_call_context(token)

    # 注入字段在 schema 边界被拒：执行安全失败，run_python 未被调用
    assert captured["calls"] == [], "注入字段不得穿透到 run_python"
    denied = [f for evt, f in core.audit.events if evt == "execution_denied"]
    assert denied and any(f.get("reason") == "schema_violation" for f in denied)
