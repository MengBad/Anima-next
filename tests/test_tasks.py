import types

import pytest

from anima.core.persistence import PersistenceProvider
from anima.core.tasks import OwnerTaskAgent, TaskStore


def test_task_store_rejects_reexecution(tmp_path):
    store = TaskStore(PersistenceProvider(str(tmp_path)))
    task = store.create("检查日志", 42, ["收集证据"])
    assert store.claim(task.id).status == "running"
    with pytest.raises(ValueError, match="不可重复执行"):
        store.claim(task.id)


@pytest.mark.asyncio
async def test_task_agent_keeps_pending_approval(monkeypatch, tmp_path):
    store = TaskStore(PersistenceProvider(str(tmp_path)))
    task = store.create("检查高风险动作", 42, ["诊断"])

    class FakeLoop:
        tool_trace = [{"result": "等待主人审批: id=abc", "ok": True}]

        def __init__(self, *args, **kwargs):
            pass

        async def run(self, *args, **kwargs):
            return "已收集证据，等待审批"

    monkeypatch.setattr("anima.agent.react.ReActLoop", FakeLoop)
    core = types.SimpleNamespace(tasks=store, llm=object(), tools=object())
    result = await OwnerTaskAgent(core).execute(task)
    assert "等待审批" in result
    assert store.get(task.id).status == "awaiting_approval"
