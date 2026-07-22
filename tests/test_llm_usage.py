import tempfile

import aiohttp
import pytest

from anima.core.config import AnimaConfig, PermissionConfig, PersistenceConfig, WebUIConfig
from anima.core.anima_core import AnimaCore
from anima.core.config_service import ConfigService
from anima.llm import LLMClient
from anima.llm.usage import UsageEvent, UsageLedger, current_call_info, llm_scope, scoped_llm
from anima.webui import WebUIServer


def test_usage_ledger_persists_summary_and_masks_estimate():
    with tempfile.TemporaryDirectory() as root:
        ledger = UsageLedger(root)
        with llm_scope("vision", "describe", "trace-1"):
            info = UsageEvent(1.0, "trace-1", "vision", "describe", "gemini", "flash", "flash", 10, 5, 0, 15, 42.5, 0, "ok")
        ledger.record(info)
        summary = ledger.summary(start=0, end=2)
        assert summary["calls"] == 1
        assert summary["total_tokens"] == 15
        assert summary["models"][0]["model"] == "flash"
        assert summary["modules"][0]["module"] == "vision"
        assert summary["p95_latency_ms"] == 42.5
        assert ledger.events(start=0, end=2)[0]["estimated"] is False


def test_llm_client_module_route_and_usage_parsers():
    client = LLMClient("http://127.0.0.1:1/v1", "key", model="default", module_models={"vision": "vision-model"})
    with llm_scope("vision"):
        assert client.model_for() == "vision-model"
    assert client.model_for(explicit="explicit") == "explicit"
    exact = client._usage_from_response({"usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}})
    assert exact == (3, 4, 0, 7, False)
    gemini = client._usage_from_response({"usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 6}})
    assert gemini[:4] == (2, 6, 0, 8)
    assert gemini[4] is False


@pytest.mark.asyncio
async def test_scoped_llm_marks_nested_fake_client_calls():
    class FakeClient:
        async def chat(self, messages):
            info = current_call_info()
            return info.module, info.operation

    @scoped_llm("bilibili", "summary")
    async def summarize(client):
        return await client.chat([])

    assert await summarize(FakeClient()) == ("bilibili", "summary")


@pytest.mark.asyncio
async def test_config_service_change_set_is_validated_and_secret_is_masked(tmp_path):
    config = AnimaConfig(
        persistence=PersistenceConfig(data_dir=str(tmp_path)),
        permissions=PermissionConfig(owners=[42]),
        webui=WebUIConfig(enabled=True, token="web-token"),
    )
    core = AnimaCore(config)
    service = ConfigService(core)
    item = service.create_change_set({"llm": {"model": "gemini-3.1-pro-low", "api_key": "new-secret"}, "autonomy": {"active_reply_prob": 0.12}})
    assert item["status"] == "pending"
    assert item["after"]["llm"]["api_key"] == {"configured": True}
    assert item["restart_required"] is True
    applied = service.apply(item["id"])
    assert applied["status"] == "applied"
    assert core.config.llm.model == "gemini-3.1-pro-low"
    assert core.config.autonomy.active_reply_prob == 0.12
    history = service.list_change_sets()
    assert history[0]["id"] == item["id"]
    assert history[0]["patch"]["llm"]["api_key"] == {"configured": True}
    service.rollback(item["id"])
    assert core.config.llm.model == "gpt-4o-mini"


def test_config_service_reject_marks_pending_without_touching_runtime(tmp_path):
    config = AnimaConfig(
        persistence=PersistenceConfig(data_dir=str(tmp_path)),
        permissions=PermissionConfig(owners=[42]),
    )
    core = AnimaCore(config)
    service = ConfigService(core)
    item = service.create_change_set({"autonomy": {"active_reply_prob": 0.99}})

    rejected = service.reject(item["id"], "预检失败后不再部署")

    assert rejected["status"] == "rejected"
    assert rejected["rejection_reason"] == "预检失败后不再部署"
    # 拒绝变更不得改动运行时；默认主动概率以 AutonomyConfig 为准（当前 0.08）
    assert core.config.autonomy.active_reply_prob == AnimaConfig().autonomy.active_reply_prob
    assert service.get_change_set(item["id"])["status"] == "rejected"


@pytest.mark.asyncio
async def test_config_service_syncs_green_heartbeat_fields(tmp_path):
    config = AnimaConfig(
        persistence=PersistenceConfig(data_dir=str(tmp_path)),
        permissions=PermissionConfig(owners=[42]),
    )
    core = AnimaCore(config)

    class HeartbeatProbe:
        active_reply_prob = 0.04
        active_global_cooldown_sec = 1800
        active_daily_max_per_group = 3
        daily_max = 20

    probe = HeartbeatProbe()
    core.heartbeat = probe
    service = ConfigService(core)
    item = service.create_change_set({"autonomy": {
        "active_reply_prob": 0.1,
        "active_global_cooldown_sec": 600,
        "active_daily_max_per_group": 5,
        "daily_max_per_group": 25,
    }})
    assert item["restart_required"] is False

    service.apply(item["id"])

    assert probe.active_reply_prob == 0.1
    assert probe.active_global_cooldown_sec == 600
    assert probe.active_daily_max_per_group == 5
    assert probe.daily_max == 25


@pytest.mark.asyncio
async def test_green_config_rollback_does_not_schedule_restart(tmp_path):
    config = AnimaConfig(
        persistence=PersistenceConfig(data_dir=str(tmp_path)),
        permissions=PermissionConfig(owners=[42]),
        webui=WebUIConfig(enabled=True, host="127.0.0.1", port=0, token="web-token"),
    )
    core = AnimaCore(config)
    core.config_path = str(tmp_path / "config.yaml")
    server = WebUIServer(core)
    await server.start()
    port = server._site._server.sockets[0].getsockname()[1]
    headers = {"Authorization": "Bearer web-token"}
    scheduled = []
    server._schedule_restart = lambda: scheduled.append(True)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://127.0.0.1:{port}/api/config/change-sets",
                headers=headers,
                json={"patch": {"autonomy": {"active_reply_prob": 0.05}}},
            ) as response:
                change = await response.json()
                assert response.status == 201
            async with session.post(
                f"http://127.0.0.1:{port}/api/config/change-sets/{change['id']}/apply",
                headers=headers,
            ) as response:
                applied = await response.json()
                assert response.status == 200
                assert applied["restart_scheduled"] is False
            async with session.post(
                f"http://127.0.0.1:{port}/api/config/change-sets/{change['id']}/rollback",
                headers=headers,
            ) as response:
                rolled = await response.json()
                assert response.status == 200
                assert rolled["restart_scheduled"] is False
        assert scheduled == []
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_webui_llm_center_does_not_echo_secret(tmp_path):
    config = AnimaConfig(
        persistence=PersistenceConfig(data_dir=str(tmp_path)),
        permissions=PermissionConfig(owners=[42]),
        webui=WebUIConfig(enabled=True, host="127.0.0.1", port=0, token="web-token"),
    )
    core = AnimaCore(config)
    server = WebUIServer(core)
    await server.start()
    port = server._site._server.sockets[0].getsockname()[1]
    headers = {"Authorization": "Bearer web-token"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/api/config/state", headers=headers) as response:
                payload = await response.json()
                assert response.status == 200
                assert payload["state"]["webui"]["token"] == {"configured": True}
                assert "web-token" not in await response.text()
            async with session.post(f"http://127.0.0.1:{port}/api/config/change-sets", headers=headers,
                                    json={"patch": {"llm": {"model": "route-model"}}}) as response:
                assert response.status == 201
                change = await response.json()
            async with session.post(f"http://127.0.0.1:{port}/api/config/change-sets/{change['id']}/apply", headers=headers) as response:
                assert response.status == 200
                assert (await response.json())["restart_scheduled"] is False
        assert core.config.llm.model == "route-model"
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_webui_exposes_masked_config_history_for_owner(tmp_path):
    config = AnimaConfig(
        persistence=PersistenceConfig(data_dir=str(tmp_path)),
        permissions=PermissionConfig(owners=[42]),
        webui=WebUIConfig(enabled=True, host="127.0.0.1", port=0, token="web-token"),
    )
    core = AnimaCore(config)
    server = WebUIServer(core)
    await server.start()
    port = server._site._server.sockets[0].getsockname()[1]
    headers = {"Authorization": "Bearer web-token"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://127.0.0.1:{port}/api/config/change-sets",
                headers=headers,
                json={"patch": {"llm": {"api_key": "history-secret", "model": "history-model"}}},
            ) as response:
                assert response.status == 201
                change = await response.json()
            async with session.get(
                f"http://127.0.0.1:{port}/api/config/change-sets?limit=1",
                headers=headers,
            ) as response:
                payload = await response.json()
                assert response.status == 200
                assert payload["changes"][0]["id"] == change["id"]
                assert payload["changes"][0]["patch"]["llm"]["api_key"] == {"configured": True}
                assert "history-secret" not in await response.text()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_webui_can_reject_pending_config_change(tmp_path):
    config = AnimaConfig(
        persistence=PersistenceConfig(data_dir=str(tmp_path)),
        permissions=PermissionConfig(owners=[42]),
        webui=WebUIConfig(enabled=True, host="127.0.0.1", port=0, token="web-token"),
    )
    core = AnimaCore(config)
    server = WebUIServer(core)
    await server.start()
    port = server._site._server.sockets[0].getsockname()[1]
    headers = {"Authorization": "Bearer web-token"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://127.0.0.1:{port}/api/config/change-sets",
                headers=headers,
                json={"patch": {"autonomy": {"active_reply_prob": 0.9}}},
            ) as response:
                change = await response.json()
                assert response.status == 201
            async with session.post(
                f"http://127.0.0.1:{port}/api/config/change-sets/{change['id']}/reject",
                headers=headers,
                json={"reason": "不在本轮发布"},
            ) as response:
                rejected = await response.json()
                assert response.status == 200
                assert rejected["status"] == "rejected"
                assert rejected["rejection_reason"] == "不在本轮发布"
        assert core.config.autonomy.active_reply_prob == AnimaConfig().autonomy.active_reply_prob
        assert any(item.get("type") == "webui_config_rejected" for item in core.audit.recent())
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_llm_change_preflight_keeps_live_config_on_bad_provider(tmp_path):
    async def models(request):
        if request.headers.get("Authorization") != "Bearer live-key":
            return aiohttp.web.json_response({"error": "unauthorized"}, status=401)
        return aiohttp.web.json_response({"data": [{"id": "good-model"}]})

    upstream = aiohttp.web.Application()
    upstream.router.add_get("/v1/models", models)
    upstream_runner = aiohttp.web.AppRunner(upstream)
    await upstream_runner.setup()
    upstream_site = aiohttp.web.TCPSite(upstream_runner, "127.0.0.1", 0)
    await upstream_site.start()
    upstream_port = upstream_site._server.sockets[0].getsockname()[1]
    endpoint = f"http://127.0.0.1:{upstream_port}/v1"
    config = AnimaConfig(
        llm={"endpoint": endpoint, "api_key": "live-key", "model": "good-model"},
        persistence=PersistenceConfig(data_dir=str(tmp_path)),
        permissions=PermissionConfig(owners=[42]),
        webui=WebUIConfig(enabled=True, host="127.0.0.1", port=0, token="web-token"),
    )
    core = AnimaCore(config)
    server = WebUIServer(core)
    await server.start()
    port = server._site._server.sockets[0].getsockname()[1]
    headers = {"Authorization": "Bearer web-token"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://127.0.0.1:{port}/api/config/change-sets",
                headers=headers,
                json={"patch": {"llm": {"model": "missing-model"}}},
            ) as response:
                missing = await response.json()
                assert response.status == 201
            async with session.post(
                f"http://127.0.0.1:{port}/api/config/change-sets/{missing['id']}/apply",
                headers=headers,
            ) as response:
                assert response.status == 400
                assert "模型不可用" in await response.text()
            assert core.config.llm.model == "good-model"

            async with session.post(
                f"http://127.0.0.1:{port}/api/config/change-sets",
                headers=headers,
                json={"patch": {"llm": {"api_key": "bad-key"}}},
            ) as response:
                bad_key = await response.json()
                assert response.status == 201
            async with session.post(
                f"http://127.0.0.1:{port}/api/config/change-sets/{bad_key['id']}/apply",
                headers=headers,
            ) as response:
                assert response.status == 400
                assert "HTTP 401" in await response.text()
            assert core.config.llm.api_key.get_secret_value() == "live-key"

            async with session.post(
                f"http://127.0.0.1:{port}/api/config/change-sets",
                headers=headers,
                json={"patch": {"llm": {"model": "good-model"}}},
            ) as response:
                good = await response.json()
                assert response.status == 201
            async with session.post(
                f"http://127.0.0.1:{port}/api/config/change-sets/{good['id']}/apply",
                headers=headers,
            ) as response:
                assert response.status == 200
                assert (await response.json())["status"] == "applied"
    finally:
        await server.close()
        await upstream_runner.cleanup()
