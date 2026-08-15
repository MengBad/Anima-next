"""R6.1 —— python.exec 主体授权（principal authorization）硬边界验证。

目标：证明 ``python.exec`` 满足 **owner-only authorization**，且存在
不可绕过的执行边界：

- 非 owner / 未知 / 缺失 / 伪造 / 畸形主体 → 执行边界 DENY（**不触发任何沙箱**）
- owner + 有效授权 → 经 hardened sandbox 执行 + 审计
- schema 隐藏 / UI 不展示 / 名称猜测 均**不是**安全边界；直接调 capability /
  直接调 provider / legacy 路由 都必须经过同一 owner 硬边界
- R5 的 D1-D4 fail-closed 在 owner 边界之后仍然有效

纪律（R6.1 anchored）：
- 测试优先（test-first）：先有本矩阵，再据此实现/验收。
- 不扩大 blast radius：只验证 python.exec 的 owner 边界与既有 D1-D4；
  不假设其他 capability / WebUI / 权限模型变更。
- 任何未授权路径必须 DENY；任何 DENY 必须见审计证据；audit unavailable 必须 fail-closed。
"""
from __future__ import annotations

import pytest

from anima.capability import CapabilityContract, PermissionScopes
from anima.contracts.capability import Stability
from anima.core.config import AnimaConfig
from anima.runtime.providers.python_exec import PythonExecProvider, PythonExecPolicy
from anima.tools import ToolRegistry, _CALL_CONTEXT
from anima.tools.builtin.code_exec import make_python_exec_tool

# 真实生产 owner QQ（与 VPS config.permissions.owners 一致）。
OWNER = 1562290139
NON_OWNER = 999999
UNKNOWN = 4242424242

PYTHON_EXEC = "python.exec"
PYTHON_EXEC_SCHEMA = "python_exec"  # capability id 的 schema name（"." → "_"）


# --------------------------------------------------------------------------
# 脚手架
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_call_context():
    """teardown 清空 ``_CALL_CONTEXT``，防止 owner 上下文泄漏到后续测试（顺序隔离）。"""
    yield
    _CALL_CONTEXT.set({})


class _FakeAudit:
    """极简审计 sink（gateway / ToolRegistry 共用）。

    runtime resolver 的 ``_emit_audit`` 直接 ``sink(event)``（callable），故实现
    ``__call__``；同时为兼容也提供 ``emit_audit`` / ``emit``。
    """

    def __init__(self):
        self.events: list = []

    def __call__(self, event):
        self.events.append(event)

    def emit_audit(self, event_type: str, **fields):
        self.events.append({"event_type": event_type, **fields})

    def emit(self, event_type: str, **fields):
        self.events.append({"event_type": event_type, **fields})

    def by_type(self, event_type: str):
        return [e for e in self.events if getattr(e, "get", lambda k: None)("event_type") == event_type]

    def has(self, event_type: str) -> bool:
        return any(getattr(e, "get", lambda k: None)("event_type") == event_type for e in self.events)


class _FakeCore:
    def __init__(self, cfg: AnimaConfig, audit: _FakeAudit):
        self.config = cfg
        self.audit = audit


def _cfg(python_exec_enabled: bool = True, owner: int = OWNER) -> AnimaConfig:
    cfg = AnimaConfig()
    cfg.permissions.owners = [owner]
    cfg.capability_layer.capability_mode = True
    cfg.capability_layer.capability_gateway_mode = True
    cfg.capability_layer.python_exec_enabled = python_exec_enabled
    cfg.capability_layer.governance_audit_enabled = True
    cfg.capability_layer.python_exec_require_audit = True
    cfg.capability_layer.python_exec_permission_level = 2
    return cfg


def _registry(python_exec_enabled: bool = True, allowlist=None, owner: int = OWNER):
    """构造带 python.exec 治理入口 + gateway 的 ToolRegistry（审计 sink 已接）。"""
    cfg = _cfg(python_exec_enabled=python_exec_enabled, owner=owner)
    audit = _FakeAudit()
    core = _FakeCore(cfg, audit)
    reg = ToolRegistry(
        core,
        capability_mode=True,
        capability_gateway_mode=True,
        pilot_capability_allowlist=allowlist,
        governance_audit_sink=audit,
    )
    # 仅挂载 python.exec 入口（capability_index 可解析 + gateway 路由触发）。
    # 不拉取全部 builtin，避免无关副作用；gateway 自身 bootstrap 的其余能力
    # 仅注册 provider（闭包），不会在本测试中被调用。
    reg.capability_index.declare_contract(CapabilityContract(
        id=PYTHON_EXEC, version="1.0.0", domain="code",
        description="R6.1 test contract", stability=Stability.EXPERIMENTAL,
        permission=PermissionScopes(exec=True),
    ))
    reg.register(make_python_exec_tool(
        reg, capability_ids=[PYTHON_EXEC], granted_scopes={"exec": True},
        capability_only=True,
    ))
    # 真实 provider 执行会落到 sandbox.run_python；测试中以 monkeypatch 替换。
    return reg, core, audit


@pytest.fixture
def fake_isolator(monkeypatch):
    """本机（Windows dev）无 bwrap，isolator 默认不可用 → 模拟可用。

    真实隔离在 VPS/Linux+bwrap 上单独验证（见 production 验证）；此处只验证
    授权边界逻辑，故隔离可用性被 mock。
    """
    monkeypatch.setattr(
        "anima.tools.sandbox._available_isolator",
        lambda: ("fake_isolator", True),
    )


@pytest.fixture
def fake_run_python(monkeypatch, fake_isolator):
    captured = {"calls": []}

    async def _fake(code: str, permission_level: int = 2, **_kw):
        # gateway 执行器内部 ``await sandbox.run_python``，故 mock 必须是 async。
        captured["calls"].append((code, permission_level))
        return f"ran_python:{len(code)}"

    monkeypatch.setattr("anima.tools.sandbox.run_python", _fake)
    monkeypatch.setattr(
        "anima.runtime.providers.python_exec.sandbox.run_python", _fake
    )
    return captured


def _set_ctx(user_id=None, permission_level=2):
    _CALL_CONTEXT.set(
        {"user_id": user_id, "permission_level": permission_level}
        if user_id is not None else {}
    )


def _owner_executor():
    calls = {"n": 0}

    def _fake_executor(code: str, permission_level: int):
        calls["n"] += 1
        return f"RAN:{code}"

    return _fake_executor, calls


# --------------------------------------------------------------------------
# 一、Provider 硬边界（核心：不可绕过）
# --------------------------------------------------------------------------

async def test_owner_allowed_executes(fake_isolator):
    """owner 主体 → 执行成功，executor 实际被调用。"""
    _set_ctx(user_id=OWNER)
    ex, calls = _owner_executor()
    p = PythonExecProvider(ex, policy=PythonExecPolicy(), owner_ids=[OWNER])
    res = p.invoke(PYTHON_EXEC, {"code": "print(1+1)"}, ("r", "t", 0))
    assert res.success is True, res.error
    assert calls["n"] == 1


async def test_non_owner_denied_no_exec():
    """非 owner（即使知道能力名）→ DENY，且 executor 绝不被调用。"""
    _set_ctx(user_id=NON_OWNER)
    ex, calls = _owner_executor()
    p = PythonExecProvider(ex, policy=PythonExecPolicy(), owner_ids=[OWNER])
    res = p.invoke(PYTHON_EXEC, {"code": "open('/tmp/R6_1_SHOULD_NOT_EXIST','w')"}, ("r", "t", 0))
    assert res.success is False
    assert res.denied_reason == "principal_not_authorized"
    assert calls["n"] == 0, "非 owner 不应触达 executor"


async def test_missing_identity_denied():
    """call context 缺失 → DENY（fail-closed），不触达 executor。"""
    _set_ctx(user_id=None)
    ex, calls = _owner_executor()
    p = PythonExecProvider(ex, policy=PythonExecPolicy(), owner_ids=[OWNER])
    res = p.invoke(PYTHON_EXEC, {"code": "x"}, ("r", "t", 0))
    assert res.success is False
    assert res.denied_reason == "principal_not_authorized"
    assert calls["n"] == 0


async def test_empty_owner_list_fail_closed():
    """owner_ids 空（未配置 owner）→ 即便 ctx 声称 owner 也 DENY。"""
    _set_ctx(user_id=OWNER)
    ex, calls = _owner_executor()
    p = PythonExecProvider(ex, policy=PythonExecPolicy(), owner_ids=[])
    res = p.invoke(PYTHON_EXEC, {"code": "x"}, ("r", "t", 0))
    assert res.success is False
    assert res.denied_reason == "principal_not_authorized"
    assert calls["n"] == 0


async def test_forged_owner_denied():
    """伪造 owner（ctx 写非 owner 的伪造 user_id）→ DENY。

    信任来源只能是 pipeline 设定的 call context；调用方无法用参数伪造身份。
    """
    _set_ctx(user_id=UNKNOWN)
    ex, calls = _owner_executor()
    p = PythonExecProvider(ex, policy=PythonExecPolicy(), owner_ids=[OWNER])
    res = p.invoke(PYTHON_EXEC, {"code": "x"}, ("r", "t", 0))
    assert res.success is False
    assert res.denied_reason == "principal_not_authorized"
    assert calls["n"] == 0


async def test_malformed_principal_denied():
    """畸形 call context（缺失 user_id 字段）→ DENY。"""
    _CALL_CONTEXT.set({"permission_level": 2})  # 无 user_id
    ex, calls = _owner_executor()
    p = PythonExecProvider(ex, policy=PythonExecPolicy(), owner_ids=[OWNER])
    res = p.invoke(PYTHON_EXEC, {"code": "x"}, ("r", "t", 0))
    assert res.success is False
    assert res.denied_reason == "principal_not_authorized"
    assert calls["n"] == 0


async def test_provider_direct_call_still_enforced(fake_isolator):
    """反绕过：直接实例化并调用 provider（不经 gateway / call_capability）
    仍必须经过 owner 硬边界 —— 非 owner 直接调用 → DENY。"""
    _set_ctx(user_id=NON_OWNER)
    ex, calls = _owner_executor()
    p = PythonExecProvider(ex, policy=PythonExecPolicy(), owner_ids=[OWNER])
    res = p.invoke(PYTHON_EXEC, {"code": "1/0"}, ("r", "t", 0))
    assert res.success is False
    assert res.denied_reason == "principal_not_authorized"
    assert calls["n"] == 0
    # owner 直接调用 → ALLOW（证明所有者确实可控）
    _set_ctx(user_id=OWNER)
    res2 = p.invoke(PYTHON_EXEC, {"code": "ok"}, ("r", "t", 0))
    assert res2.success is True


# --------------------------------------------------------------------------
# 二、D1-D4 在 owner 边界之后仍然有效（R5 回归，聚焦 provider）
# --------------------------------------------------------------------------

async def test_d1_kill_switch_after_owner():
    _set_ctx(user_id=OWNER)
    ex, calls = _owner_executor()
    p = PythonExecProvider(ex, policy=PythonExecPolicy(kill_switch=True), owner_ids=[OWNER])
    res = p.invoke(PYTHON_EXEC, {"code": "x"}, ("r", "t", 0))
    assert res.success is False
    assert res.denied_reason == "kill_switch_active"
    assert calls["n"] == 0


async def test_d2_no_grant_after_owner():
    _set_ctx(user_id=OWNER)
    ex, calls = _owner_executor()
    p = PythonExecProvider(ex, policy=PythonExecPolicy(grant=False), owner_ids=[OWNER])
    res = p.invoke(PYTHON_EXEC, {"code": "x"}, ("r", "t", 0))
    assert res.success is False
    assert res.denied_reason == "no_exec_grant"
    assert calls["n"] == 0


async def test_d3_audit_unavailable_after_owner():
    _set_ctx(user_id=OWNER)
    ex, calls = _owner_executor()
    p = PythonExecProvider(
        ex, policy=PythonExecPolicy(require_audit=True, audit_sink_ok=False),
        owner_ids=[OWNER],
    )
    res = p.invoke(PYTHON_EXEC, {"code": "x"}, ("r", "t", 0))
    assert res.success is False
    assert res.denied_reason == "audit_sink_unavailable"
    assert calls["n"] == 0


async def test_d4_isolator_missing_after_owner(monkeypatch):
    _set_ctx(user_id=OWNER)
    ex, calls = _owner_executor()
    p = PythonExecProvider(ex, policy=PythonExecPolicy(), owner_ids=[OWNER])
    # 强制 isolator 不可用（D4 fail-closed 真实触发条件）
    monkeypatch.setattr(
        "anima.tools.sandbox._available_isolator", lambda: ("", False)
    )
    res = p.invoke(PYTHON_EXEC, {"code": "x"}, ("r", "t", 0))
    assert res.success is False
    assert res.denied_reason == "isolator_unavailable"
    assert calls["n"] == 0


# --------------------------------------------------------------------------
# 三、call_capability 路由级（gateway 路径）
# --------------------------------------------------------------------------

async def test_call_capability_owner_allowed(fake_run_python):
    """owner 经 call_capability 路由 → ALLOW + 实际执行（gateway 路径打通）。

    call_capability 把 ProviderResult 翻译回 str（治理路径设计），故成功时 res 为
    执行输出字符串；核心断言是「执行确实发生」（fake_run_python 被调用）。
    """
    reg, core, audit = _registry()
    _set_ctx(user_id=OWNER)
    res = await reg.call_capability(PYTHON_EXEC, {"code": "print('R6_1_OWNER_EXEC_OK')"})
    assert fake_run_python["calls"], "owner 路径应触达 sandbox.run_python"
    assert isinstance(res, str) and len(res) > 0, f"owner 执行应输出，得到 {res!r}"


async def test_call_capability_non_owner_denied(fake_run_python):
    """非 owner 经 call_capability 路由（直接指定能力名）→ DENY 且不执行。"""
    reg, core, audit = _registry()
    _set_ctx(user_id=NON_OWNER)
    res = await reg.call_capability(PYTHON_EXEC, {"code": "open('/tmp/x','w')"})
    # gateway 失败 → call_capability 返回安全失败字符串；核心断言：绝不执行。
    assert fake_run_python["calls"] == [], "非 owner 不得触达 sandbox"


async def test_call_capability_missing_identity_denied(fake_run_python):
    reg, core, audit = _registry()
    _set_ctx(user_id=None)
    res = await reg.call_capability(PYTHON_EXEC, {"code": "x"})
    assert fake_run_python["calls"] == []


async def test_call_capability_disabled_denied(fake_run_python):
    """python_exec_enabled=False → fail-closed DENY（不路由 gateway / 不执行）。"""
    reg, core, audit = _registry(python_exec_enabled=False)
    _set_ctx(user_id=OWNER)
    res = await reg.call_capability(PYTHON_EXEC, {"code": "x"})
    assert fake_run_python["calls"] == [], "禁用时不得执行"


async def test_call_capability_empty_allowlist_owner_ok(fake_run_python):
    """空 allowlist 下 owner 仍 ALLOW（owner-only 策略：owner 恒放行）。"""
    reg, core, audit = _registry(allowlist=[])
    _set_ctx(user_id=OWNER)
    res = await reg.call_capability(PYTHON_EXEC, {"code": "print(1)"})
    assert fake_run_python["calls"], "owner 路径应触达 sandbox.run_python"
    assert isinstance(res, str) and len(res) > 0, f"owner 执行应输出，得到 {res!r}"


# --------------------------------------------------------------------------
# 四、schema 隐藏 ≠ 安全边界 + 反绕过
# --------------------------------------------------------------------------

async def test_schema_hidden_from_non_owner_but_bypass_denied():
    """非 owner 看不到 schema；但直接指定能力名仍 DENY（隐藏不是边界）。"""
    reg, core, audit = _registry()
    _set_ctx(user_id=NON_OWNER)
    schemas = reg.get_schemas(capability_mode=True, caller="user:999999")
    # python.exec 的 schema（名 python_exec）不应出现在非 owner 的 schema 中（governed, 未放行）
    assert not any(
        s.get("function", {}).get("name") == PYTHON_EXEC_SCHEMA for s in schemas
    )
    # 但即便知道名称，直接调用仍 DENY（schema 隐藏不是安全边界）
    res = await reg.call_capability(PYTHON_EXEC, {"code": "x"})
    assert not (hasattr(res, "success") and res.success)


async def test_owner_sees_schema_and_can_call():
    reg, core, audit = _registry()
    _set_ctx(user_id=OWNER)
    schemas = reg.get_schemas(capability_mode=True, caller="user:1562290139")
    # owner 经 pilot 放行，python.exec 的 schema（名 python_exec）应可见。
    assert any(
        s.get("function", {}).get("name") == PYTHON_EXEC_SCHEMA for s in schemas
    )


# --------------------------------------------------------------------------
# 五、pilot gate 语义
# --------------------------------------------------------------------------

def test_pilot_gate_owner_always_permitted():
    reg, core, audit = _registry(allowlist=[])
    _set_ctx(user_id=OWNER)
    assert reg.pilot_capability_permitted(caller=None) is True


def test_pilot_gate_non_owner_empty_denied():
    reg, core, audit = _registry(allowlist=[])
    _set_ctx(user_id=NON_OWNER)
    assert reg.pilot_capability_permitted(caller=None) is False


def test_pilot_gate_non_owner_in_allowlist_denied_by_provider():
    """即便非 owner 被加入 allowlist（配置误配），provider 硬边界仍 DENY。"""
    reg, core, audit = _registry(allowlist=["user:999999"])
    _set_ctx(user_id=NON_OWNER)
    # pilot 放行（allowlist 命中），但 provider 边界拦截
    assert reg.pilot_capability_permitted(caller=None) is True
    # 真实执行仍被 provider 拒绝
    ex, calls = _owner_executor()
    p = PythonExecProvider(ex, policy=PythonExecPolicy(), owner_ids=[OWNER])
    res = p.invoke(PYTHON_EXEC, {"code": "x"}, ("r", "t", 0))
    assert res.success is False
    assert res.denied_reason == "principal_not_authorized"


# --------------------------------------------------------------------------
# 六、审计证据
# --------------------------------------------------------------------------

async def test_deny_audit_evidence():
    _set_ctx(user_id=NON_OWNER)
    ex, calls = _owner_executor()
    p = PythonExecProvider(ex, policy=PythonExecPolicy(), owner_ids=[OWNER])
    res = p.invoke(PYTHON_EXEC, {"code": "x"}, ("r", "t", 0))
    assert res.success is False
    assert res.denied_reason == "principal_not_authorized"
    assert res.audit.get("event") == "authorization_denied"
    assert res.audit.get("owner_authorized") is False


async def test_allow_audit_evidence(fake_isolator):
    _set_ctx(user_id=OWNER)
    ex, calls = _owner_executor()
    p = PythonExecProvider(ex, policy=PythonExecPolicy(), owner_ids=[OWNER])
    res = p.invoke(PYTHON_EXEC, {"code": "ok"}, ("r", "t", 0))
    assert res.success is True
    assert res.audit.get("owner_authorized") is True
    assert res.audit.get("capability") == PYTHON_EXEC


# --------------------------------------------------------------------------
# 七、配置篡改 / 权限注入（fail-closed）
# --------------------------------------------------------------------------

async def test_permission_level_injection_ignored():
    """调用方无法通过 request 参数提升为 owner。"""
    _set_ctx(user_id=NON_OWNER)
    ex, calls = _owner_executor()
    p = PythonExecProvider(ex, policy=PythonExecPolicy(), owner_ids=[OWNER])
    # 试图用 arguments 注入 permission_level / principal —— 应被忽略
    res = p.invoke(
        PYTHON_EXEC,
        {"code": "x", "permission_level": 4, "principal": "user:1562290139"},
        ("r", "t", 0),
    )
    assert res.success is False
    assert res.denied_reason == "principal_not_authorized"
