from __future__ import annotations

import pytest

from anima.core.anima_core import AnimaCore
from anima.core.config import AnimaConfig, PersistenceConfig
from anima.tools import LEVEL_ADMIN, Tool, ToolRegistry


@pytest.mark.asyncio
async def test_danger_tool_waits_for_owner_then_executes_and_survives_restart(tmp_path):
    config = AnimaConfig(persistence=PersistenceConfig(data_dir=str(tmp_path)))
    core = AnimaCore(config)
    calls: list[str] = []

    async def handler(value: str) -> str:
        calls.append(value)
        return "已执行:" + value

    registry = ToolRegistry(core)
    registry.register(Tool("danger_test", "测试", {"value": {"type": "string"}}, handler,
                           min_level=LEVEL_ADMIN, danger=True))
    core.tools = registry
    waiting = await registry.call("danger_test", {"value": "x"}, LEVEL_ADMIN)
    approval_id = waiting.split("id=", 1)[1].split("，", 1)[0]
    assert calls == []

    # 模拟重启：审批参数来自持久化队列，而非临时闭包或内存标志。
    restarted = AnimaCore(config)
    resumed = ToolRegistry(restarted)
    resumed.register(Tool("danger_test", "测试", {"value": {"type": "string"}}, handler,
                          min_level=LEVEL_ADMIN, danger=True))
    restarted.tools = resumed
    assert await resumed.approve_and_execute(approval_id, 42) == "已执行:x"
    assert calls == ["x"]
    assert restarted.approvals.items[approval_id].status == "approved"


@pytest.mark.asyncio
async def test_danger_rejection_never_executes(tmp_path):
    core = AnimaCore(AnimaConfig(persistence=PersistenceConfig(data_dir=str(tmp_path))))
    called = False

    def handler() -> str:
        nonlocal called
        called = True
        return "bad"

    registry = ToolRegistry(core)
    registry.register(Tool("danger_test", "测试", {}, handler, min_level=LEVEL_ADMIN, danger=True))
    core.tools = registry
    waiting = await registry.call("danger_test", {}, LEVEL_ADMIN)
    approval_id = waiting.split("id=", 1)[1].split("，", 1)[0]
    registry.reject_approval(approval_id, 42)
    assert called is False
    with pytest.raises(ValueError):
        await registry.approve_and_execute(approval_id, 42)
