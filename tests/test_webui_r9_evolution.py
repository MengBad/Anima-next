"""R9 WebUI 受控自我进化端点测试。

验证 /api/evolution/* 四个端点的语义：列出 / 发起提案（安全围栏）/ 批准 / 驳回，
且批准只放行「提案」不触发部署（fail-closed）。不依赖真实 LLM 或部署脚本。
"""

import pytest

from anima.core.anima_core import AnimaCore
from anima.core.config import AnimaConfig, PermissionConfig, PersistenceConfig, WebUIConfig
from anima.webui import WebUIServer


class _FakeSC:
    """最小 SelfCodeManager：只实现 create_engineering_task（propose 阶段用到）。"""

    async def create_engineering_task(self, goal, creator):
        return type("T", (), {"id": "task-fake"})()


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

    # 隔离 SelfCode 真实依赖：propose 阶段只用到 create_engineering_task。
    import anima.cognitive.selfcode as sc_mod
    import anima.agent.self_evolution as se_mod
    orig = sc_mod.get_selfcode_manager
    sc_mod.get_selfcode_manager = lambda c: _FakeSC()
    # 测试隔离：重置进程内单例审批门，避免跨测试提案泄漏。
    se_mod._GATE_SINGLETON = None
    try:
        async with __import__("aiohttp").ClientSession() as session:
            yield session, base, headers
    finally:
        sc_mod.get_selfcode_manager = orig
        se_mod._GATE_SINGLETON = None
    await server.close()


@pytest.mark.asyncio
async def test_evolution_endpoints_propose_approve_reject(client):
    session, base, headers = client

    # 1) 列表初始为空
    async with session.get(f"{base}/api/evolution/proposals", headers=headers) as r:
        assert r.status == 200
        body = await r.json()
        assert body["items"] == []

    # 2) 发起一条非核心提案 → pending_owner
    async with session.post(f"{base}/api/evolution/propose", headers=headers,
                            json={"goal": "降低长对话回复延迟", "target_files": ["anima/tools/foo.py"]}) as r:
        assert r.status == 201
        body = await r.json()
        assert body["decision"]["decision"] == "pending_owner"
        pid = body["proposal"]["id"]
        assert body["proposal"]["touches_core"] is False

    # 3) 列表出现该提案
    async with session.get(f"{base}/api/evolution/proposals", headers=headers) as r:
        items = (await r.json())["items"]
        assert len(items) == 1
        assert items[0]["id"] == pid
        assert items[0]["decision"] == "pending_owner"

    # 4) 批准提案 → 仅放行提案，不部署
    async with session.post(f"{base}/api/evolution/{pid}/approve", headers=headers) as r:
        assert r.status == 200
        body = await r.json()
        assert body["decision"] == "approved"

    # 5) 批准后状态可见
    async with session.get(f"{base}/api/evolution/proposals", headers=headers) as r:
        items = (await r.json())["items"]
        assert items[0]["decision"] == "approved"
        assert items[0]["status"] == "approved"

    # 6) 驳回另一条（先发起）
    async with session.post(f"{base}/api/evolution/propose", headers=headers,
                            json={"goal": "新增观测工具"}) as r:
        pid2 = (await r.json())["proposal"]["id"]
    async with session.post(f"{base}/api/evolution/{pid2}/reject", headers=headers,
                            json={"reason": "暂不处理"}) as r:
        assert r.status == 200
        assert (await r.json())["decision"] == "rejected"


@pytest.mark.asyncio
async def test_evolution_propose_core_file_rejected_by_fence(client):
    session, base, headers = client

    # 发起触核心文件的提案 → 安全围栏直接 rejected
    async with session.post(f"{base}/api/evolution/propose", headers=headers,
                            json={"goal": "改权限模型", "target_files": ["anima/capability/rbac.py"]}) as r:
        assert r.status == 201
        body = await r.json()
        assert body["decision"]["decision"] == "rejected"
        assert body["proposal"]["touches_core"] is True

    # 触核心提案不出现可批准操作（在列表中应被标记 rejected）
    async with session.get(f"{base}/api/evolution/proposals", headers=headers) as r:
        items = (await r.json())["items"]
        assert items[0]["decision"] == "rejected"
        assert items[0]["touches_core"] is True
