"""受治理 capability 路由**不得阻塞事件循环**（2026-09-23 修复）。

背景（生产实测，faulthandler 全线程栈坐实）
--------------------------------------------
``search.web`` 经 ``ToolRegistry.call_capability`` → ``_governed_capability_route``
→ ``CapabilityGateway.handle`` → Provider executor →  ``anima.tools._run_in_thread``。
``_run_in_thread`` 是 **sync** 契约（F5.4b I2/I3 冻结），内部 ``t.join()`` 会**阻塞调用线程**。
而 ``_governed_capability_route`` 此前直接在**事件循环线程**上同步调用 ``handle`` ⇒
一次联网检索的**全程**（网络 connect 卡顿可达数十秒）都在阻塞 loop。

生产证据
- 2026-09-21 09:21:15 ``Timeout (0:01:15)!`` 栈转储中可见
  ``anima/tools/__init__.py:253 in _worker``（桥接线程的嵌套 loop），
  与其并行的是 ``web.py in _fetch_page`` 卡在 ``socket.create_connection``；
- 24h 窗口 ``事件循环延迟`` 告警 8 条，峰值 23.099s，与 ``search_web`` 工具调用窗口重合。

本测试用「gateway 同步 sleep」模拟该阻塞，断言 loop 期间**仍能推进其他任务**。
"""
from __future__ import annotations

import asyncio
import inspect
import threading
import time

import pytest

from anima.tools import ToolRegistry, get_call_context


class _Result:
    success = True
    error = None
    data = "ok"


def _ret() -> _Result:
    return _Result()


class _SlowSyncGateway:
    """模拟受治理 gateway：**同步**阻塞（真实实现里是 ``t.join()``）。"""

    def __init__(self, delay: float):
        self.delay = delay
        self.calls = 0
        self.thread = None

    def handle(self, need, execution_key):  # noqa: ANN001
        self.calls += 1
        self.thread = threading.current_thread()
        time.sleep(self.delay)
        return _ret()


class _CtxProbeGateway:
    def handle(self, need, execution_key):  # noqa: ANN001
        self.seen = get_call_context()
        return _ret()


def _registry_with(gateway) -> ToolRegistry:
    """注入替身 gateway。

    用私有槽 ``_capability_gateway``（``capability_gateway`` 是惰性构建的**只读**
    property）：本测试只验证**调用点是否离开 loop 线程**，不验证 gateway 构建——
    构建面由 ``tests/test_capability_gateway_adoption.py`` 覆盖。
    """
    registry = ToolRegistry()
    registry._capability_gateway = gateway
    return registry


# --------------------------------------------------------------------------- 1
@pytest.mark.asyncio
async def test_governed_route_does_not_block_event_loop():
    """gateway 同步执行期间，loop 上的其他任务必须继续推进（本次修复的核心断言）。"""
    gateway = _SlowSyncGateway(0.35)
    registry = _registry_with(gateway)

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0)          # 让 ticker 先跑起来
    ticks = 0                       # 归零基线：只统计 gateway 执行期间的推进次数

    try:
        result = await registry._governed_capability_route("search.web", {}, "user:1")
    finally:
        task.cancel()

    assert gateway.calls == 1
    assert result == "ok"
    assert ticks >= 3, (
        f"事件循环被受治理路由阻塞：gateway 同步执行 0.35s 期间 ticker 仅推进 {ticks} 次"
        "（预期 >=3；0 表示 loop 完全停转）"
    )


# --------------------------------------------------------------------------- 2
@pytest.mark.asyncio
async def test_governed_route_runs_gateway_off_the_main_thread():
    """受治理执行必须离开 loop 线程 —— 否则 ``t.join()`` 必然阻塞 loop。"""
    gateway = _SlowSyncGateway(0.0)
    registry = _registry_with(gateway)
    main_thread = threading.current_thread()

    await registry._governed_capability_route("search.web", {}, "user:1")

    assert gateway.thread is not None
    assert gateway.thread is not main_thread


# --------------------------------------------------------------------------- 3
@pytest.mark.asyncio
async def test_governed_route_preserves_call_context_across_threads():
    """R3 context fidelity：离线程不得丢 ``_CALL_CONTEXT``（permission_level 由它决定）。"""
    gateway = _CtxProbeGateway()
    registry = _registry_with(gateway)

    token = registry.set_call_context(permission_level=4, group_id="727631526")
    try:
        await registry._governed_capability_route("search.web", {}, "user:1")
    finally:
        registry.reset_call_context(token)

    assert gateway.seen.get("permission_level") == 4
    assert gateway.seen.get("group_id") == "727631526"


# --------------------------------------------------------------------------- 4
def test_sync_contract_of_gateway_handle_is_unchanged():
    """F5.4b I2/I3 冻结：``CapabilityGateway.handle`` 必须仍是 **sync** 函数。

    本次修复只允许改**调用点**（``await asyncio.to_thread(handle, ...)``），
    不允许把 ``handle`` 变成协程 —— 那会破坏 Line B 的 sync 契约。
    """
    from anima.capability.governance.gateway import CapabilityGateway

    assert inspect.iscoroutinefunction(CapabilityGateway.handle) is False
