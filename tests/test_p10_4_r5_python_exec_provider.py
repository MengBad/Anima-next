"""P10.4-R5 PHASE 7 / 8c — python.exec provider fail-closed (RED -> GREEN).

目标
----
``PythonExecProvider`` 在 kill_switch / 无 grant / 审计下沉不可用 / 隔离器不可用 时
**拒绝执行**（fail-closed），仅在全部满足时委托 run_python 并返回严格 ProviderResult
（含隔离 / 审计证据）。

本文件在 provider 创建前为 RED（``PythonExecProvider`` 不存在 / 拒绝分支未实现），
创建并接线后转 GREEN。

范围纪律（R5 #3/#8）
------------------
- 仅验证 provider 的 fail-closed 契约；不注册进生产默认 registry（部署属 PHASE 13+，
  未授权）。``tools/__init__.py`` / legacy 注册逻辑不变。
- 不部署、不改 config.yaml / capability_levels、不碰 sandbox_write / execution_bridge /
  identity policy。
"""
from __future__ import annotations

import pytest

from anima.runtime.capability_resolver import (
    CapabilityContract,
    CapabilityNeed,
    CapabilityRegistry,
    CapabilityResolver,
)
from anima.runtime.providers.base import ProviderResult
from anima.runtime.providers.python_exec import (
    PythonExecPolicy,
    PythonExecProvider,
)
from anima.tools import sandbox, _CALL_CONTEXT


# R6.1：provider 现在强制 owner-only 边界。R5 的 fail-closed 测试需在 owner
# 上下文中运行，以便 D1-D4 门仍能在其后被验证（owner 边界通过后才到 D1-D4）。
OWNER_QQ = 10001


@pytest.fixture(autouse=True)
def _owner_call_context():
    _CALL_CONTEXT.set({"user_id": OWNER_QQ, "permission_level": 2})
    yield
    # 关键：reset 防止 call context 泄漏到后续测试（避免顺序依赖污染）。
    _CALL_CONTEXT.set({})


def _fake_executor(code: str, permission_level: int) -> str:
    return f"RAN:{code}"


def _provider(policy: PythonExecPolicy | None = None) -> PythonExecProvider:
    return PythonExecProvider(_fake_executor, policy=policy, owner_ids=[OWNER_QQ])


def _exec_id_of(key) -> str:
    return "pyexec:" + ":".join(str(x) for x in key)


# --- D1 kill_switch -------------------------------------------------------
def test_deny_when_kill_switch_active():
    p = _provider(PythonExecPolicy(kill_switch=True))
    r = p.invoke("python.exec", {"code": "1+1"}, ("r", "t", 0))
    assert isinstance(r, ProviderResult)
    assert r.success is False
    assert r.denied_reason == "kill_switch_active"
    assert r.exit_code is None


# --- D2 no grant ----------------------------------------------------------
def test_deny_when_no_exec_grant():
    p = _provider(PythonExecPolicy(grant=False))
    r = p.invoke("python.exec", {"code": "1+1"}, ("r", "t", 0))
    assert r.success is False
    assert r.denied_reason == "no_exec_grant"


# --- D3 audit sink down ---------------------------------------------------
def test_deny_when_audit_sink_unavailable():
    p = _provider(PythonExecPolicy(require_audit=True, audit_sink_ok=False))
    r = p.invoke("python.exec", {"code": "1+1"}, ("r", "t", 0))
    assert r.success is False
    assert r.denied_reason == "audit_sink_unavailable"


# --- D4 isolator unavailable ----------------------------------------------
def test_deny_when_isolator_unavailable(monkeypatch):
    monkeypatch.setattr(sandbox, "_available_isolator", lambda: ("", ""))
    p = _provider(PythonExecPolicy())  # 其余全允许，但隔离器不可用
    r = p.invoke("python.exec", {"code": "1+1"}, ("r", "t", 0))
    assert r.success is False
    assert r.denied_reason == "isolator_unavailable"
    assert r.audit.get("isolator") is False


# --- success path ---------------------------------------------------------
def test_success_when_all_preconditions_met(monkeypatch):
    # dev 环境无隔离器（Windows），模拟 VPS 有 bwrap 以走通成功路径。
    monkeypatch.setattr(sandbox, "_available_isolator", lambda: ("bwrap", "/usr/bin/bwrap"))
    p = _provider(PythonExecPolicy())
    r = p.invoke("python.exec", {"code": "1+1"}, ("r", "t", 0))
    assert r.success is True
    assert r.denied_reason is None
    assert r.data == "RAN:1+1"
    assert r.execution_id == _exec_id_of(("r", "t", 0))
    assert r.exit_code == 0
    assert r.bytes == len("RAN:1+1")
    assert r.stdout_hash.startswith("sha256:")
    assert r.audit.get("isolator") is True
    assert r.audit.get("delegated") == "run_python"


# --- executor raises SandboxDenied -> deny --------------------------------
def test_deny_when_executor_raises_sandbox_denied(monkeypatch):
    monkeypatch.setattr(sandbox, "_available_isolator", lambda: ("bwrap", "/usr/bin/bwrap"))

    def _boom(code, permission_level):
        raise sandbox.SandboxDenied("isolation failed at runtime")

    p = PythonExecProvider(_boom, policy=PythonExecPolicy(), owner_ids=[OWNER_QQ])
    r = p.invoke("python.exec", {"code": "1+1"}, ("r", "t", 0))
    assert r.success is False
    assert r.denied_reason == "sandbox_denied"
    assert r.audit.get("sandbox_denied") is True


# --- resolver integration (success path propagates strict fields) ---------
def test_resolver_success_propagates_strict_fields(monkeypatch):
    # 模拟 VPS 有隔离器，使 provider 被选且成功委托。
    monkeypatch.setattr(sandbox, "_available_isolator", lambda: ("bwrap", "/usr/bin/bwrap"))
    reg = CapabilityRegistry()
    reg.register(
        CapabilityContract(
            capability_id="python.exec",
            permission="exec",
            side_effect=True,   # 代码执行可能改变外部状态（诚实声明）
            idempotent=False,  # 非幂等（诚实声明；须与 provider 运行时真相一致）
        ),
        _provider(PythonExecPolicy()),
    )
    resolver = CapabilityResolver(reg)
    result = resolver.resolve(
        CapabilityNeed("python.exec", args={"code": "1+1"}), ("r", "t", 0)
    )
    assert isinstance(result, ProviderResult)
    assert result.success is True
    assert result.denied_reason is None
    assert result.audit.get("isolator") is True
    assert result.execution_id == _exec_id_of(("r", "t", 0))
