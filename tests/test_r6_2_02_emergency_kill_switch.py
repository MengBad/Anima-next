"""R6.2-02 Emergency global capability kill switch — fail-closed 测试。

验证：
  * 全局 kill switch 开启 → 所有 governed capability 调用 DENY（含 owner）。
  * 开启时不调用 executor（执行被边界拦截）。
  * kill switch 关闭 → 正常执行。
  * 通用 capability 路由同样被总闸拦截。
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


def test_kill_switch_blocks_owner(fake_run):
    calls, fake = fake_run
    gs = GovernanceState()
    gs.set_global_kill_switch(True)
    provider = PythonExecProvider(executor=fake, policy=PythonExecPolicy(),
                                  owner_ids=[OWNER], governance_state=gs)
    _set_ctx(OWNER)
    res = provider.invoke("python.exec", {"code": CODE}, ("r", "t", 0))
    assert res.success is False
    assert res.denied_reason == "global_kill_switch_active"
    assert calls == []


def test_kill_switch_blocks_non_owner(fake_run):
    calls, fake = fake_run
    gs = GovernanceState()
    gs.set_global_kill_switch(True)
    provider = PythonExecProvider(executor=fake, policy=PythonExecPolicy(),
                                  owner_ids=[OWNER], governance_state=gs)
    _set_ctx(NON_OWNER)
    res = provider.invoke("python.exec", {"code": CODE}, ("r", "t", 0))
    assert res.success is False
    assert res.denied_reason == "global_kill_switch_active"
    assert calls == []


def test_kill_switch_off_allows_owner(fake_run):
    calls, fake = fake_run
    gs = GovernanceState()  # kill switch 默认关闭
    provider = PythonExecProvider(executor=fake, policy=PythonExecPolicy(),
                                  owner_ids=[OWNER], governance_state=gs)
    _set_ctx(OWNER)
    res = provider.invoke("python.exec", {"code": CODE}, ("r", "t", 0))
    assert res.success is True
    assert calls == [CODE]


def test_kill_switch_toggle_then_restore(fake_run):
    calls, fake = fake_run
    gs = GovernanceState()
    provider = PythonExecProvider(executor=fake, policy=PythonExecPolicy(),
                                  owner_ids=[OWNER], governance_state=gs)
    _set_ctx(OWNER)
    gs.set_global_kill_switch(True)
    blocked = provider.invoke("python.exec", {"code": CODE}, ("r", "t", 0))
    assert blocked.denied_reason == "global_kill_switch_active"
    gs.set_global_kill_switch(False)
    allowed = provider.invoke("python.exec", {"code": CODE}, ("r", "t", 0))
    assert allowed.success is True
    assert calls == [CODE]
