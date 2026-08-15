"""R6.2-01 Capability Revocation — 执行边界拒绝测试。

验证：
  * 能力 REVOKED 后，owner / non-owner 调用均被 DENY（executor 调用 0 次）。
  * 撤销在 execution boundary 强制，不依赖 UI 隐藏。
  * restore 后能力恢复 ACTIVE 可正常执行。
  * 通用 capability 路由（ToolRegistry.call_capability）同样拒绝已撤销能力。
"""
from __future__ import annotations

import pytest

from anima.capability.governance.state import GovernanceState
from anima.runtime.providers.python_exec import PythonExecProvider, PythonExecPolicy


OWNER = "1562290139"
NON_OWNER = "999999"
CODE = "print('hi')"


def _set_ctx(uid, level=2):
    from anima.tools import _CALL_CONTEXT
    _CALL_CONTEXT.set({
        "user_id": str(uid),
        "permission_level": level,
        "channel": "group",
        "is_owner": False,
    })


@pytest.fixture
def fake_run(monkeypatch):
    """模拟真实隔离器可用（Windows dev 无 bwrap），并让执行落到本 fake（共享 calls）。"""
    calls = []
    monkeypatch.setattr(
        "anima.tools.sandbox._available_isolator",
        lambda: ("fake_isolator", True),
    )

    def _fake(code, level):
        calls.append(code)
        return "ok", 0

    monkeypatch.setattr("anima.tools.sandbox.run_python", _fake)
    return calls, _fake


# ---------------- GovernanceState 单元 ----------------

def test_revoke_status_and_restore():
    gs = GovernanceState()
    assert gs.status("python.exec") == GovernanceState.ACTIVE
    gs.revoke("python.exec")
    assert gs.is_revoked("python.exec")
    assert gs.status("python.exec") == GovernanceState.REVOKED
    gs.restore("python.exec")
    assert not gs.is_revoked("python.exec")
    assert gs.status("python.exec") == GovernanceState.ACTIVE


# ---------------- Provider 边界（owner / non-owner） ----------------

def test_revoked_denies_owner(fake_run):
    calls, fake = fake_run
    gs = GovernanceState()
    gs.revoke("python.exec")
    provider = PythonExecProvider(executor=fake, policy=PythonExecPolicy(),
                                  owner_ids=[OWNER], governance_state=gs)
    _set_ctx(OWNER)
    res = provider.invoke("python.exec", {"code": CODE}, ("r", "t", 0))
    assert res.success is False
    assert res.denied_reason == "capability_revoked"
    assert calls == []


def test_revoked_denies_non_owner(fake_run):
    calls, fake = fake_run
    gs = GovernanceState()
    gs.revoke("python.exec")
    provider = PythonExecProvider(executor=fake, policy=PythonExecPolicy(),
                                  owner_ids=[OWNER], governance_state=gs)
    _set_ctx(NON_OWNER)
    res = provider.invoke("python.exec", {"code": CODE}, ("r", "t", 0))
    assert res.success is False
    assert res.denied_reason == "capability_revoked"
    assert calls == []


def test_restore_re_enables_execution(fake_run):
    calls, fake = fake_run
    gs = GovernanceState()
    gs.revoke("python.exec")
    provider = PythonExecProvider(executor=fake, policy=PythonExecPolicy(),
                                  owner_ids=[OWNER], governance_state=gs)
    _set_ctx(OWNER)
    denied = provider.invoke("python.exec", {"code": CODE}, ("r", "t", 0))
    assert denied.denied_reason == "capability_revoked"
    gs.restore("python.exec")
    allowed = provider.invoke("python.exec", {"code": CODE}, ("r", "t", 0))
    assert allowed.success is True
    assert calls == [CODE]


# ---------------- 通用 capability 路由拒绝 ----------------

async def test_revoked_blocks_call_capability():
    from anima.tools import ToolRegistry

    class _Core:
        pass
    reg = ToolRegistry(core=_Core(), capability_mode=True, capability_gateway_mode=True,
                       governance_audit_sink=None)
    gs = GovernanceState()
    gs.revoke("python.exec")
    reg.governance_state = gs
    _set_ctx(OWNER)
    result = await reg.call_capability("python.exec", {"code": CODE})
    assert isinstance(result, str)
    assert "python.exec" in result
