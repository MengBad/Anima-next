"""P0-02 WebUI 稳定性端点测试（只读，不触生产）。

验证 /api/os/overview 聚合 P0-02 稳定性记录，且 /api/os/command 的 stability 意图
只返回只读数据、不自我授权任何执行。
"""
import pytest

from anima.core.anima_core import AnimaCore
from anima.core.config import AnimaConfig, PermissionConfig, PersistenceConfig, WebUIConfig
from anima.webui import WebUIServer


@pytest.fixture
async def client(tmp_path):
    config = AnimaConfig(
        persistence=PersistenceConfig(data_dir=str(tmp_path)),
        permissions=PermissionConfig(owners=[42]),
        webui=WebUIConfig(enabled=True, host="127.0.0.1", port=0, token="test-token"),
    )
    core = AnimaCore(config)
    server = WebUIServer(core)
    await server.start()
    port = server._site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    headers = {"Authorization": "Bearer test-token"}
    async with __import__("aiohttp").ClientSession() as session:
        yield session, base, headers
    await server.close()


@pytest.mark.asyncio
async def test_os_overview_includes_stability(client):
    session, base, headers = client
    async with session.get(f"{base}/api/os/overview", headers=headers) as r:
        assert r.status == 200
        body = await r.json()
        assert "stability" in body
        # 无历史数据 → 各窗口 data_complete=False（诚实，不编造）
        for w in ("7d", "30d", "90d"):
            assert w in body["stability"]


@pytest.mark.asyncio
async def test_os_command_stability_readonly(client):
    session, base, headers = client
    async with session.post(f"{base}/api/os/command", headers=headers,
                            json={"command": "近 30 天稳定性稳不稳？"}) as r:
        assert r.status == 200
        body = await r.json()
        cmd = body["command"]
        assert cmd["intent"] == "stability"
        assert cmd["needs_confirmation"] is False
        assert "stability" in body


@pytest.mark.asyncio
async def test_os_command_observe_readonly(client):
    session, base, headers = client
    async with session.post(f"{base}/api/os/command", headers=headers,
                            json={"command": "观察一下最近环境异常趋势"}) as r:
        assert r.status == 200
        body = await r.json()
        cmd = body["command"]
        assert cmd["intent"] == "observe"
        assert cmd["needs_confirmation"] is False
        assert "environment" in body


@pytest.mark.asyncio
async def test_os_overview_includes_environment(client):
    session, base, headers = client
    async with session.get(f"{base}/api/os/overview", headers=headers) as r:
        assert r.status == 200
        body = await r.json()
        assert "environment" in body


@pytest.mark.asyncio
async def test_os_command_impact_readonly(client):
    session, base, headers = client
    async with session.post(f"{base}/api/os/command", headers=headers,
                            json={"command": "演化影响评分如何？效果值不值？"}) as r:
        assert r.status == 200
        body = await r.json()
        cmd = body["command"]
        assert cmd["intent"] == "impact"
        assert cmd["needs_confirmation"] is False
        assert "impact" in body


@pytest.mark.asyncio
async def test_os_overview_includes_impact(client):
    session, base, headers = client
    async with session.get(f"{base}/api/os/overview", headers=headers) as r:
        assert r.status == 200
        body = await r.json()
        assert "impact" in body


@pytest.mark.asyncio
async def test_os_command_society_readonly(client):
    session, base, headers = client
    async with session.post(f"{base}/api/os/command", headers=headers,
                            json={"command": "Agent 社会辩论：要不要自动升级依赖？"}) as r:
        assert r.status == 200
        body = await r.json()
        cmd = body["command"]
        assert cmd["intent"] == "society"
        assert cmd["needs_confirmation"] is False
        assert "society" in body
        assert body["society"]["verdict"]["judge_id"] == "judge"
