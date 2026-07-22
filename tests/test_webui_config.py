import aiohttp
import pytest
import yaml

from anima.core.anima_core import AnimaCore
from anima.core.config import AnimaConfig, PermissionConfig, PersistenceConfig, WebUIConfig
from anima.webui import WebUIServer


@pytest.mark.asyncio
async def test_cognitive_config_get_and_post(tmp_path):
    config = AnimaConfig(
        persistence=PersistenceConfig(data_dir=str(tmp_path)),
        webui=WebUIConfig(enabled=True, host="127.0.0.1", port=0, token="cfg-token"),
    )
    core = AnimaCore(config)
    server = WebUIServer(core)
    await server.start()
    port = server._site._server.sockets[0].getsockname()[1]

    # 写回路径指向临时文件，避免污染真实仓库 config.yaml；不真重启服务
    tmp_cfg = tmp_path / "config.yaml"
    tmp_cfg.write_text("qq: {}\n", encoding="utf-8")
    server._config_yaml_path = lambda: tmp_cfg
    server._schedule_restart = lambda: None

    headers = {"Authorization": "Bearer cfg-token"}
    async with aiohttp.ClientSession() as session:
        # GET：默认全关
        async with session.get(f"http://127.0.0.1:{port}/api/cognitive/config", headers=headers) as r:
            assert r.status == 200
            d = await r.json()
            assert d["danger"]["enabled"] is False
            assert d["contradiction"]["enabled"] is False
            assert set(d["danger"].keys()) >= {
                "active_info_collection", "relationship_inference", "stance_propagation",
                "stance_propagation_confirm", "identity_crisis", "autonomous_web",
                "memory_infection", "memory_infection_confirm", "core_mutation",
                "core_mutation_confirm",
            }

        # POST：全开 danger + 矛盾检测
        body = {"danger": {k: True for k in d["danger"]}, "contradiction": {"enabled": True}}
        async with session.post(
            f"http://127.0.0.1:{port}/api/cognitive/config", headers=headers, json=body
        ) as r:
            assert r.status == 200
            out = await r.json()
            assert out["ok"] is True
            assert out["danger"]["enabled"] is True
            assert out["contradiction"]["enabled"] is True

        # 确认写回了临时 config.yaml（其余段与密钥原样保留）
        written = yaml.safe_load(tmp_cfg.read_text(encoding="utf-8"))
        assert written["danger"]["enabled"] is True
        assert written["danger"]["core_mutation_confirm"] is True
        assert written["contradiction"]["enabled"] is True
        assert written["qq"] == {}  # 原段保留

        # 非法字段类型应被 pydantic 拒
        async with session.post(
            f"http://127.0.0.1:{port}/api/cognitive/config", headers=headers,
            json={"danger": {"enabled": "not-a-bool"}},
        ) as r:
            assert r.status == 400
    await server.close()


@pytest.mark.asyncio
async def test_webui_group_policy_change_set_hot_applies(tmp_path):
    config = AnimaConfig(
        persistence=PersistenceConfig(data_dir=str(tmp_path)),
        permissions=PermissionConfig(owners=[42]),
        webui=WebUIConfig(enabled=True, host="127.0.0.1", port=0, token="group-policy-token"),
    )
    core = AnimaCore(config)
    server = WebUIServer(core)
    await server.start()
    port = server._site._server.sockets[0].getsockname()[1]
    headers = {"Authorization": "Bearer group-policy-token"}
    patch = {"permissions": {"group_policies": {
        "727631526": {"reply_enabled": True, "reply_only_when_mentioned": False,
                      "autonomy": False, "tools_enabled": False, "stickers_enabled": False}
    }}}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"http://127.0.0.1:{port}/api/config/validate", headers=headers, json={"patch": patch}) as response:
                assert response.status == 200
            async with session.post(f"http://127.0.0.1:{port}/api/config/change-sets", headers=headers, json={"patch": patch}) as response:
                assert response.status == 201
                change = await response.json()
            assert change["restart_required"] is False
            assert "群策略" in change["impact"]
            async with session.post(f"http://127.0.0.1:{port}/api/config/change-sets/{change['id']}/apply", headers=headers) as response:
                assert response.status == 200
        assert core.config.permissions.group_policies["727631526"]["tools_enabled"] is False
    finally:
        await server.close()
