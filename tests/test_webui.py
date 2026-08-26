import types
import re
import json

import aiohttp
import pytest

from anima.core.anima_core import AnimaCore
from anima.core.config import AnimaConfig, PermissionConfig, PersistenceConfig, WebUIConfig
from anima.webui import WebUIServer


@pytest.mark.asyncio
async def test_webui_observation_endpoints_and_token(tmp_path):
    config = AnimaConfig(
        persistence=PersistenceConfig(data_dir=str(tmp_path)),
        permissions=PermissionConfig(owners=[42]),
        webui=WebUIConfig(enabled=True, host="127.0.0.1", port=0, token="test-token"),
    )
    core = AnimaCore(config)
    core.audit.emit("test_event", ok=True)
    server = WebUIServer(core)
    await server.start()
    port = server._site._server.sockets[0].getsockname()[1]

    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://127.0.0.1:{port}/source") as response:
            assert response.status == 200
            source_html = await response.text()
            assert "AGPL-3.0-only" in source_html
            assert "对应源码" in source_html
        async with session.get(f"http://127.0.0.1:{port}/source/archive") as response:
            # Development trees do not carry a generated public archive.
            assert response.status == 404
        async with session.get(f"http://127.0.0.1:{port}/api/health") as response:
            assert response.status == 401
        async with session.get(f"http://127.0.0.1:{port}/") as response:
            assert response.status == 200
            assert "欢迎回来，主人" in await response.text()
        headers = {"Authorization": "Bearer test-token"}
        async with session.get(f"http://127.0.0.1:{port}/", headers=headers) as response:
            assert response.status == 200
            html = await response.text()
            assert "Anima · 生命控制台" in html
            assert '<div id="app">' in html
            assert response.headers["Content-Security-Policy"].startswith("default-src 'self'")
            script_path = re.search(r'src="(/assets/app-[^"]+\.js)"', html).group(1)
        async with session.get(f"http://127.0.0.1:{port}{script_path}") as response:
            assert response.status == 200
            assert "max-age=31536000" in response.headers["Cache-Control"]
            app_js = await response.text()
            assert "模型控制中心" in app_js
            assert "工程候选" in app_js
            assert "表情资产" in app_js
            assert "快速命令" in app_js
            assert "记忆模块" in app_js or "/api/memory/health" in app_js
            assert "自主探索" in app_js
            assert "/api/exploration" in app_js
            assert "审核池、图库与发送策略" in app_js
        async with session.post(f"http://127.0.0.1:{port}/api/auth/login", json={"token": "test-token"}) as response:
            assert response.status == 200
        async with session.get(f"http://127.0.0.1:{port}/?token=test-token") as response:
            assert response.status == 200
            assert "anima_webui_token" in response.headers.get("Set-Cookie", "")
        async with session.get(f"http://127.0.0.1:{port}/api/health", headers=headers) as response:
            assert response.status == 200
            health = await response.json()
            assert "channel_bound" in health
            assert "channel_connected" in health
            assert "channel" in health
            assert health["config_recovery"]["state"] == "idle"
        core.health_history.record(force=True)
        async with session.get(f"http://127.0.0.1:{port}/api/health/history?range=24h", headers=headers) as response:
            assert response.status == 200
            history = await response.json()
            assert history["samples"]
            assert "llm_failures" in history["samples"][-1]
        async with session.get(f"http://127.0.0.1:{port}/api/timeline", headers=headers) as response:
            assert (await response.json())["events"][0]["type"] == "test_event"
        async with session.get(f"http://127.0.0.1:{port}/api/life", headers=headers) as response:
            assert (await response.json())["agenda_pending"] == 0
        async with session.get(f"http://127.0.0.1:{port}/api/reply-quality", headers=headers) as response:
            assert response.status == 200
            quality = await response.json()
            assert quality["status"] == "healthy"
            assert quality["report"]["total_replies"] == 0
            assert quality["signals"] == []
            # L0 counters present in report schema
            for key in ("empty_should_reply", "tool_spins", "space_link_ok",
                        "space_link_fail", "react_max_rounds"):
                assert key in quality["report"]
        async with session.get(f"http://127.0.0.1:{port}/api/weekly-digest", headers=headers) as response:
            assert response.status == 200
            digest_empty = await response.json()
            assert digest_empty["status"] in {"empty", "ok"}
        async with session.post(f"http://127.0.0.1:{port}/api/weekly-digest", headers=headers,
                                json={"days": 7}) as response:
            assert response.status == 200
            digest = await response.json()
            assert digest["status"] == "ok"
            assert "digest" in digest and digest["digest"]["quality_totals"] is not None
            assert "周报摘要" in digest["text"]
        async with session.get(f"http://127.0.0.1:{port}/api/ops-quality", headers=headers) as response:
            assert response.status == 200
            ops = await response.json()
            assert ops["status"] in {"healthy", "watch", "attention"}
            assert "metrics" in ops and "events_backpressure_dropped" in ops["metrics"]
            assert isinstance(ops.get("signals"), list)
        async with session.get(f"http://127.0.0.1:{port}/api/memory/health", headers=headers) as response:
            assert response.status == 200
            memory = await response.json()
            assert memory["status"] in {"healthy", "watch", "attention"}
            assert "modules" in memory and isinstance(memory["modules"], list)
            module_ids = {item["id"] for item in memory["modules"]}
            assert {
                "chat_memory", "vector_memory", "episodes", "crossgroup",
                "self_notes", "cognitive_memory", "group_presence", "forgetting",
            } <= module_ids
            for item in memory["modules"]:
                assert {"id", "label", "status", "message", "metrics"} <= set(item)
                assert item["status"] in {"running", "ready", "degraded", "stopped", "disabled"}
            assert "gates" in memory
            assert "reply_only_when_mentioned" in memory["gates"]
            assert isinstance(memory.get("recent_vector"), list)
            assert isinstance(memory.get("chat_sessions"), list)
            assert isinstance(memory.get("crossgroup"), dict)
            assert isinstance(memory.get("signals"), list)
        async with session.get(f"http://127.0.0.1:{port}/api/exploration", headers=headers) as response:
            assert response.status == 200
            exploration = await response.json()
            assert exploration["health"]["enabled"] is True
            assert exploration["items"] == []
    await server.close()


@pytest.mark.asyncio
async def test_webui_modules_status_and_dashboard(tmp_path):
    config = AnimaConfig(
        persistence=PersistenceConfig(data_dir=str(tmp_path)),
        webui=WebUIConfig(enabled=True, host="127.0.0.1", port=0, token="mod-token"),
    )
    core = AnimaCore(config)
    core.audit.emit("boot", ok=True)
    server = WebUIServer(core)
    await server.start()
    port = server._site._server.sockets[0].getsockname()[1]
    headers = {"Authorization": "Bearer mod-token"}
    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://127.0.0.1:{port}/api/modules/status", headers=headers) as response:
            assert response.status == 200
            data = await response.json()
            assert data["status"] in ("running", "stopped")
            assert isinstance(data["modules"], list) and data["modules"]
            module_ids = {item["id"] for item in data["modules"]}
            assert {
                "gateway", "llm", "vector_memory", "chat_memory", "heartbeat", "watchdog",
                "audio",
                "life_scheduler", "cognitive_core", "tools", "skills",
                "self_learning", "external_exploration", "crossgroup", "task_agent", "approvals",
                "stickers", "vision", "image_generation", "monitors",
                "selfcode", "group_presence", "reply_quality", "sylann",
                "audit", "webui",
            } <= module_ids
            for m in data["modules"]:
                assert {"id", "label", "status", "message"} <= set(m)
                assert m["status"] in ("running", "ready", "degraded", "stopped", "disabled")
            audio = next(item for item in data["modules"] if item["id"] == "audio")
            assert audio["metrics"]["requests"] == 0
            crossgroup = next(item for item in data["modules"] if item["id"] == "crossgroup")
            assert "users" in crossgroup["metrics"]
            assert "groups" in crossgroup["metrics"]
            assert "facts" in crossgroup["metrics"]
            assert "runtime_loaded" in crossgroup["metrics"]
        async with session.get(f"http://127.0.0.1:{port}/", headers=headers) as response:
            assert response.status == 200
            html = await response.text()
            style_path = re.search(r'href="(/assets/app-[^"]+\.css)"', html).group(1)
        async with session.get(f"http://127.0.0.1:{port}{style_path}") as response:
            assert response.status == 200
            app_css = await response.text()
            assert "prefers-reduced-motion" in app_css
            assert ".mobile-nav" in app_css
            assert "@media" in app_css
        async with session.get(f"http://127.0.0.1:{port}/stickers", headers=headers) as response:
            assert response.status == 200
            html = await response.text()
            assert '<div id="app">' in html
            assert "/assets/app-" in html
        async with session.get(f"http://127.0.0.1:{port}/subscriptions", headers=headers) as response:
            assert response.status == 200
            html = await response.text()
            assert '<div id="app">' in html
            assert "/assets/app-" in html
        async with session.get(f"http://127.0.0.1:{port}/exploration", headers=headers) as response:
            assert response.status == 200
            assert '<div id="app">' in await response.text()
        async with session.get(f"http://127.0.0.1:{port}/", headers=headers) as response:
            script_path = re.search(r'src="(/assets/app-[^"]+\.js)"', await response.text()).group(1)
        async with session.get(f"http://127.0.0.1:{port}{script_path}") as response:
            app_js = await response.text()
            assert "订阅监控" in app_js
            assert "/subscriptions" in app_js or "subscriptions" in app_js
    await server.close()


@pytest.mark.asyncio
async def test_webui_napcat_diagnostic_is_read_only_and_sanitized(tmp_path):
    config = AnimaConfig(
        persistence=PersistenceConfig(data_dir=str(tmp_path)),
        permissions=PermissionConfig(owners=[42]),
        webui=WebUIConfig(enabled=True, host="127.0.0.1", port=0, token="napcat-token"),
    )
    core = AnimaCore(config)

    class Channel:
        name = "onebot11"

        def health(self):
            return {
                "connected": True,
                "events_received": 7,
                "events_dropped": 1,
                "outbound_successes": 3,
                "outbound_failures": 0,
                "last_outbound_error": "",
            }

        async def _call_action(self, action, params, timeout):
            assert action == "get_login_info"
            assert params == {}
            assert timeout == 8.0
            return {"user_id": 2752719822, "nickname": "哈基米"}

    core.gateway.bind(Channel())
    server = WebUIServer(core)
    await server.start()
    port = server._site._server.sockets[0].getsockname()[1]
    headers = {"Authorization": "Bearer napcat-token"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"http://127.0.0.1:{port}/api/diagnostics/napcat", headers=headers) as response:
                assert response.status == 200
                data = await response.json()
        assert data["ok"] is True
        assert data["action"] == "get_login_info"
        assert data["login"] == {"user_id": "2752719822", "nickname": "哈基米"}
        assert data["channel"]["events_received"] == 7
        assert "webui_napcat_diagnostic" in [event["type"] for event in core.audit.recent(10)]
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_sticker_policy_is_validated_hot_applied_and_persisted(tmp_path):
    config = AnimaConfig(
        persistence=PersistenceConfig(data_dir=str(tmp_path)),
        permissions=PermissionConfig(owners=[42]),
        webui=WebUIConfig(enabled=True, host="127.0.0.1", port=0, token="policy-token"),
    )
    core = AnimaCore(config)
    server = WebUIServer(core)
    await server.start()
    port = server._site._server.sockets[0].getsockname()[1]
    headers = {"Authorization": "Bearer policy-token"}
    policy = {
        "collect_mode": "probability",
        "collect_probability": 0.45,
        "require_owner_review": True,
        "send_probability": 0.2,
        "max_stickers": 321,
        "collect_blocklist": ["group:10001"],
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://127.0.0.1:{port}/api/stickers/policy",
                headers=headers, json=policy,
            ) as response:
                assert response.status == 200
                saved = await response.json()
                assert saved["collect_mode"] == "probability"
                assert saved["require_owner_review"] is True
            async with session.post(
                f"http://127.0.0.1:{port}/api/stickers/policy",
                headers=headers, json={"send_probability": 2},
            ) as response:
                assert response.status == 400
        assert core.config.sticker.max_stickers == 321
        restored = AnimaCore(AnimaConfig(
            persistence=PersistenceConfig(data_dir=str(tmp_path)),
        ))
        assert restored.config.sticker.collect_mode == "probability"
        assert restored.config.sticker.collect_blocklist == ["group:10001"]
    finally:
        await server.close()
        from anima.cognitive.stickers import close_sticker_manager
        close_sticker_manager()


@pytest.mark.asyncio
async def test_webui_rejects_control_calls_without_token(tmp_path):
    config = AnimaConfig(
        persistence=PersistenceConfig(data_dir=str(tmp_path)),
        webui=WebUIConfig(enabled=True, host="127.0.0.1", port=0),
    )
    server = WebUIServer(AnimaCore(config))
    await server.start()
    port = server._site._server.sockets[0].getsockname()[1]
    async with aiohttp.ClientSession() as session:
        async with session.post(f"http://127.0.0.1:{port}/api/skill-drafts/none/approve") as response:
            # P0-D1 fail-closed：token 未配置时中间件先于路由以 503 锁定全接口
            assert response.status == 503
    await server.close()


@pytest.mark.asyncio
async def test_ops_quality_keeps_recovered_llm_failures_in_history_only(tmp_path):
    config = AnimaConfig(
        persistence=PersistenceConfig(data_dir=str(tmp_path)),
        permissions=PermissionConfig(owners=[42]),
        webui=WebUIConfig(enabled=True, host="127.0.0.1", port=0, token="ops-token"),
    )
    core = AnimaCore(config)
    core.llm = types.SimpleNamespace(health=lambda: {
        "current_state": "healthy", "failure_count": 7,
        "last_error": "", "last_error_at": 100.0, "last_success_at": 200.0,
        "consecutive_failures": 0, "circuit_open_until": None,
        "degradation_count": 2, "failover_count": 1,
    })
    server = WebUIServer(core)
    await server.start()
    port = server._site._server.sockets[0].getsockname()[1]
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"http://127.0.0.1:{port}/api/ops-quality",
                headers={"Authorization": "Bearer ops-token"},
            ) as response:
                payload = await response.json()
        assert payload["status"] == "healthy"
        assert payload["signals"] == []
        assert any(item["key"] == "llm_failures_history" for item in payload["history_signals"])
        assert payload["metrics"]["llm_current_state"] == "healthy"
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_webui_owner_prepares_core_patch_before_approval(tmp_path, monkeypatch):
    config = AnimaConfig(
        persistence=PersistenceConfig(data_dir=str(tmp_path)),
        permissions=PermissionConfig(owners=[42]),
        webui=WebUIConfig(enabled=True, host="127.0.0.1", port=0, token="iterate-token"),
    )
    core = AnimaCore(config)
    fake_root = tmp_path / "project"
    target = fake_root / "anima" / "tools" / "__init__.py"
    target.parent.mkdir(parents=True)
    target.write_text("OLD = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        "anima.cognitive.controlled_iteration._project_root",
        lambda: fake_root,
    )

    class LLM:
        async def chat(self, *args, **kwargs):
            return "===FILE anima/tools/__init__.py===\nNEW = 2\n# candidate\n===END==="

    core.llm = LLM()
    task = core.tasks.create("工具结果误报", 42, ["生成修复候选"], kind="fault_ticket")
    core.tasks.set_proposal(task.id, {
        "signal": "tool_spins",
        "suggested_files": ["anima/tools/__init__.py"],
        "tests": ["tests/test_react.py"],
    })
    core.tasks.set_status(task.id, "awaiting_approval")

    server = WebUIServer(core)
    await server.start()
    port = server._site._server.sockets[0].getsockname()[1]
    headers = {"Authorization": "Bearer iterate-token"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://127.0.0.1:{port}/api/iterate/{task.id}/prepare",
                headers=headers,
            ) as response:
                assert response.status == 200
                prepared = await response.json()
            assert prepared["kind"] == "core_patch"
            assert prepared["status"] == "awaiting_approval"
            assert prepared["proposal"]["unified_diffs"]
            assert target.read_text(encoding="utf-8") == "OLD = 1\n"

            async with session.post(
                f"http://127.0.0.1:{port}/api/iterate/{task.id}/approve",
                headers=headers,
            ) as response:
                assert response.status == 200
                approved = await response.json()
            assert approved["status"] == "approved_pending_apply"
            assert target.read_text(encoding="utf-8") == "OLD = 1\n"
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_webui_skill_draft_preview_and_owner_approval_flow(tmp_path, monkeypatch):
    config = AnimaConfig(
        persistence=PersistenceConfig(data_dir=str(tmp_path)),
        permissions=PermissionConfig(owners=[42]),
        webui=WebUIConfig(enabled=True, host="127.0.0.1", port=0, token="draft-token"),
    )
    core = AnimaCore(config)

    class LLM:
        async def chat(self, *args, **kwargs):
            return json.dumps({
                "id": "status_check",
                "name": "Status check",
                "description": "Read a public status page",
                "min_level": "readonly",
                "parameters": {},
                "execution": {"type": "http_get", "template": "https://status.example.com/health"},
            })

    core.llm = LLM()
    core.skills = types.SimpleNamespace(reload=lambda: None)
    from anima.skills.drafts import SkillDraftManager
    core.skill_drafts = SkillDraftManager(str(tmp_path), core)

    from anima.monitors.http_page import HttpPageProvider, HttpPageStatus

    async def query(self, url, include_content=False, max_content_chars=0):
        return HttpPageStatus("healthy", "status.example.com", "Status OK", "HTTP 200", url)

    monkeypatch.setattr(HttpPageProvider, "query", query)
    server = WebUIServer(core)
    await server.start()
    port = server._site._server.sockets[0].getsockname()[1]
    headers = {"Authorization": "Bearer draft-token"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://127.0.0.1:{port}/api/skill-drafts",
                headers=headers, json={"request": "Check the public service status"},
            ) as response:
                assert response.status == 201
                draft = await response.json()
            draft_id = draft["id"]
            assert draft["status"] == "pending_review"

            async with session.post(
                f"http://127.0.0.1:{port}/api/skill-drafts/{draft_id}/preview",
                headers=headers,
            ) as response:
                assert response.status == 200
                previewed = await response.json()
            assert previewed["preview_result"]["state"] == "healthy"

            async with session.post(
                f"http://127.0.0.1:{port}/api/skill-drafts/{draft_id}/approve",
                headers=headers,
            ) as response:
                assert response.status == 200
                approved = await response.json()
            assert approved["status"] == "approved"
            assert (tmp_path / "skills" / "status_check" / "skill.yaml").exists()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_webui_owner_can_create_and_cancel_agenda(tmp_path):
    config = AnimaConfig(
        persistence=PersistenceConfig(data_dir=str(tmp_path)),
        permissions=PermissionConfig(owners=[42]),
        webui=WebUIConfig(enabled=True, host="127.0.0.1", port=0, token="control-token"),
    )
    core = AnimaCore(config)
    class Drafts:
        def __init__(self):
            self.items = {}

        async def create_from_request(self, request):
            item = types.SimpleNamespace(id="draft-web", request=request, status="pending_review")
            self.items[item.id] = item
            return item

    core.skill_drafts = Drafts()
    from anima.tools import LEVEL_OWNER, Tool, ToolRegistry
    core.tools = ToolRegistry(core)
    core.tools.register(Tool("skill_web_test", "WebUI 执行测试", {}, lambda: "web-skill-ok", LEVEL_OWNER))
    core.skills = types.SimpleNamespace(manifests={"web_test": types.SimpleNamespace(id="web_test")})
    from anima.monitors.subscriptions import Subscription, SubscriptionStore
    core.monitors = type("Monitors", (), {"store": SubscriptionStore(core.persistence)})()
    sub = core.monitors.store.add(Subscription("http_page", "https://example.com", [123]))
    server = WebUIServer(core)
    await server.start()
    port = server._site._server.sockets[0].getsockname()[1]
    headers = {"Authorization": "Bearer control-token"}
    async with aiohttp.ClientSession() as session:
        async with session.post(f"http://127.0.0.1:{port}/api/autonomy/pause", headers=headers) as response:
            assert (await response.json())["enabled"] is False
        async with session.post(f"http://127.0.0.1:{port}/api/autonomy/resume", headers=headers) as response:
            assert (await response.json())["enabled"] is True
        async with session.post(f"http://127.0.0.1:{port}/api/permissions/admins", headers=headers,
                                json={"qq": 10001}) as response:
            assert (await response.json())["admins"] == [10001]
        async with session.post(f"http://127.0.0.1:{port}/api/tasks", headers=headers,
                                json={"request": "审阅工程候选"}) as response:
            assert response.status == 201
            task = await response.json()
        core.tasks.set_status(task["id"], "awaiting_approval", "候选已测试")
        async with session.get(f"http://127.0.0.1:{port}/api/tasks/{task['id']}", headers=headers) as response:
            assert (await response.json())["status"] == "awaiting_approval"
        async with session.post(f"http://127.0.0.1:{port}/api/tasks/{task['id']}/approve", headers=headers) as response:
            assert (await response.json())["status"] == "completed"
        async with session.post(f"http://127.0.0.1:{port}/api/tasks", headers=headers,
                                json={"request": "驳回工程候选"}) as response:
            rejected_task = await response.json()
        core.tasks.set_status(rejected_task["id"], "awaiting_approval")
        async with session.post(f"http://127.0.0.1:{port}/api/tasks/{rejected_task['id']}/reject", headers=headers,
                                json={"reason": "需要补测试"}) as response:
            rejected = await response.json()
            assert rejected["status"] == "rejected"
            assert "需要补测试" in rejected["evidence"][-1]
        async with session.get(f"http://127.0.0.1:{port}/api/permissions", headers=headers) as response:
            assert (await response.json())["admins"] == [10001]
        async with session.delete(f"http://127.0.0.1:{port}/api/permissions/admins/10001", headers=headers) as response:
            assert (await response.json())["admins"] == []
        async with session.post(f"http://127.0.0.1:{port}/api/skill-drafts", headers=headers,
                                json={"request": "学习只读状态检查"}) as response:
            assert response.status == 201
            assert (await response.json())["request"] == "学习只读状态检查"
        async with session.post(f"http://127.0.0.1:{port}/api/skills/web_test/execute", headers=headers,
                                json={"args": {}}) as response:
            assert response.status == 200
            assert (await response.json())["result"] == "web-skill-ok"
        async with session.post(f"http://127.0.0.1:{port}/api/agenda", headers=headers,
                                json={"group_id": 123, "text_intent": "提醒大家测试", "priority": 0.7}) as response:
            assert response.status == 201
            item = await response.json()
        async with session.delete(f"http://127.0.0.1:{port}/api/agenda/{item['id']}", headers=headers) as response:
            assert response.status == 200
        async with session.post(f"http://127.0.0.1:{port}/api/subscriptions", headers=headers,
                                json={"platform": "bilibili_live", "subject_id": "12345", "target_groups": [123]}) as response:
            assert response.status == 201
            created_sub = await response.json()
        async with session.delete(f"http://127.0.0.1:{port}/api/subscriptions/{created_sub['id']}", headers=headers) as response:
            assert response.status == 200
        async with session.post(f"http://127.0.0.1:{port}/api/calendar/daily", headers=headers,
                                json={"group_id": 123, "time": "08:30", "text": "早安"}) as response:
            assert response.status == 201
            calendar_item = await response.json()
        async with session.get(f"http://127.0.0.1:{port}/api/calendar", headers=headers) as response:
            assert (await response.json())["items"][0]["id"] == calendar_item["id"]
        async with session.post(f"http://127.0.0.1:{port}/api/calendar/weekly", headers=headers,
                                json={"group_id": 123, "weekday": 1, "time": "09:30", "text": "周会"}) as response:
            assert response.status == 201
            weekly_item = await response.json()
            assert weekly_item["weekday"] == 1
        async with session.get(f"http://127.0.0.1:{port}/api/calendar", headers=headers) as response:
            assert (await response.json())["weekly_items"][0]["id"] == weekly_item["id"]
        async with session.delete(f"http://127.0.0.1:{port}/api/calendar/{calendar_item['id']}", headers=headers) as response:
            assert response.status == 200
        async with session.delete(f"http://127.0.0.1:{port}/api/calendar/{weekly_item['id']}", headers=headers) as response:
            assert response.status == 200
    await server.close()
