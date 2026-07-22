"""深度认知观测面板（Task #9 D）端点测试。"""

import time

import aiohttp
import pytest

from anima.core.anima_core import AnimaCore
from anima.core.config import AnimaConfig, PersistenceConfig, PermissionConfig, WebUIConfig
from anima.webui import WebUIServer
from anima.cognitive.persona_checkpoint import (
    set_base_dir, read_persona_text, read_proposed_text, list_checkpoints,
    snapshot_persona, rollback_persona,
)


@pytest.mark.asyncio
async def test_cognitive_endpoints_shape_and_auth(tmp_path):
    config = AnimaConfig(
        persistence=PersistenceConfig(data_dir=str(tmp_path)),
        permissions=PermissionConfig(owners=[42]),
        webui=WebUIConfig(enabled=True, host="127.0.0.1", port=0, token="cog-token"),
    )
    core = AnimaCore(config)
    server = WebUIServer(core)
    await server.start()
    port = server._site._server.sockets[0].getsockname()[1]
    headers = {"Authorization": "Bearer cog-token"}

    async with aiohttp.ClientSession() as session:
        # 未授权应 401
        async with session.get(f"http://127.0.0.1:{port}/api/cognitive/overview") as r:
            assert r.status == 401

        # 总览
        async with session.get(f"http://127.0.0.1:{port}/api/cognitive/overview", headers=headers) as r:
            assert r.status == 200
            d = await r.json()
            for k in ("contradictions", "relations", "desires", "scars",
                     "identity_stability", "danger_enabled", "persona_prompt_len", "drift"):
                assert k in d
            assert "daily_spent" in d["drift"]
            assert "window_spent" in d["drift"]

        # 各面板基础形状
        for ep in ("contradictions", "relations", "identity", "desires", "scars", "prompt", "persona", "drift"):
            async with session.get(f"http://127.0.0.1:{port}/api/cognitive/{ep}", headers=headers) as r:
                assert r.status == 200, ep
                body = await r.json()
                assert (
                    "items" in body or "fragments" in body or "current" in body
                    or "stability" in body or "daily_spent" in body
                ), ep

        # Owner 暂停反刍
        async with session.post(
            f"http://127.0.0.1:{port}/api/cognitive/drift",
            headers=headers,
            json={"action": "pause", "hours": 1},
        ) as r:
            assert r.status == 200
            body = await r.json()
            assert body.get("ok") is True
            assert body.get("status", {}).get("rumination_paused") is True

        # 认知页已经进入同一个 Vue 控制台，而不是另一套遗留 HTML。
        async with session.get(f"http://127.0.0.1:{port}/cognitive", headers=headers) as r:
            assert r.status == 200
            html = await r.text()
            assert "Anima · 生命控制台" in html
            assert '<div id="app">' in html
            assert "/assets/app-" in html

        # 不存在的检查点回滚 → 404
        async with session.post(f"http://127.0.0.1:{port}/api/cognitive/persona/rollback",
                                headers=headers, json={"checkpoint": "nope"}) as r:
            assert r.status == 404
    await server.close()


@pytest.mark.asyncio
async def test_cognitive_contradiction_and_persona_flow(tmp_path):
    config = AnimaConfig(
        persistence=PersistenceConfig(data_dir=str(tmp_path)),
        permissions=PermissionConfig(owners=[42]),
        webui=WebUIConfig(enabled=True, host="127.0.0.1", port=0, token="cog-token"),
    )
    core = AnimaCore(config)
    server = WebUIServer(core)
    await server.start()
    port = server._site._server.sockets[0].getsockname()[1]
    headers = {"Authorization": "Bearer cog-token"}

    # 预置一条矛盾记录到 data_dir
    from anima.core.persistence import PersistenceProvider
    PersistenceProvider(str(tmp_path)).write_json("contradictions.json", [
        {"timestamp": time.time(), "user_id": 7, "group_id": 99,
         "description": "用户说住北京但之前说不住", "current_text": "我现在住北京"},
    ])

    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://127.0.0.1:{port}/api/cognitive/contradictions",
                               headers=headers) as r:
            d = await r.json()
            assert d["count"] == 1
            assert d["items"][0]["user_id"] == 7

    # persona 回滚流程（重定向到 tmp，避免污染仓库 config/）
    set_base_dir(tmp_path)
    try:
        (tmp_path / "persona_core.yaml").write_text(
            "system_prompt: 你是哈基米\n用户主权不可侵犯: true\n", encoding="utf-8")
        ts = snapshot_persona("test")
        assert ts is not None
        # 篡改当前文件，再回滚
        (tmp_path / "persona_core.yaml").write_text(
            "system_prompt: 被篡改\n", encoding="utf-8")
        assert "被篡改" in read_persona_text()
        async with aiohttp.ClientSession() as session:
            async with session.post(f"http://127.0.0.1:{port}/api/cognitive/persona/rollback",
                                    headers=headers, json={"checkpoint": ts}) as r:
                assert r.status == 200
        assert "哈基米" in read_persona_text()
        assert ts in [c["ts"] for c in list_checkpoints()]
    finally:
        set_base_dir(None)
    await server.close()
