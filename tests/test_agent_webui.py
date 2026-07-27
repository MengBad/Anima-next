"""Phase 5 WebUI 路由集成测试：/api/agent/* 端点 + 紧急停止贯通。"""

import types

from aiohttp.test_utils import TestClient, TestServer
from aiohttp import web

from anima.agent.owner_agent import OwnerAgent
from anima.agent.state import Step
from anima.webui.routes.agent import _register_agent


class _Mem:
    def __init__(self): self.data = {}
    def read_json(self, name, default=None): return self.data.get(name, default)
    def write_json(self, name, value): self.data[name] = value


class _FakeTools:
    async def call(self, name, args, level): return "ok"
    def get_call_context(self): return {}
    def set_call_context(self, **kw): return object()
    def reset_call_context(self, tok): pass


def _build_core(tmp_path):
    core = types.SimpleNamespace(
        persistence=_Mem(),
        config=types.SimpleNamespace(
            persistence=types.SimpleNamespace(data_dir=str(tmp_path))),
        llm=None, tools=_FakeTools(), gateway=None,
    )
    agent = OwnerAgent(core)
    core.agent = agent
    return core


async def test_agent_webui_routes(tmp_path):
    core = _build_core(tmp_path)
    s = types.SimpleNamespace(core=core)
    app = web.Application()
    _register_agent(app, s)

    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        # 列表（空）
        resp = await client.get("/api/agent/tasks")
        assert resp.status == 200
        body = await resp.json()
        assert body["items"] == []

        # 创建一个任务
        task = core.agent.store.create("帮我做X", 12345)
        task.steps = [Step(0, "a"), Step(1, "b")]
        core.agent.store.save(task)

        resp = await client.get("/api/agent/tasks")
        items = (await resp.json())["items"]
        assert len(items) == 1
        assert items[0]["id"] == task.id

        # 详情
        resp = await client.get(f"/api/agent/tasks/{task.id}")
        assert resp.status == 200
        assert (await resp.json())["request"] == "帮我做X"

        # 审计（执行器会写进度，这里至少是合法 JSON）
        resp = await client.get("/api/agent/audit")
        assert resp.status == 200
        assert "items" in (await resp.json())

        # 紧急停止
        resp = await client.post(f"/api/agent/tasks/{task.id}/cancel")
        assert resp.status == 200
        assert (await resp.json())["ok"] is True
        assert core.agent.store.get(task.id).status == "cancelled"
    finally:
        await client.close()
