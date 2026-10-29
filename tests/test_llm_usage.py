import tempfile

import aiohttp
import pytest

from anima.core.config import AnimaConfig, PermissionConfig, PersistenceConfig
from anima.core.anima_core import AnimaCore
from anima.core.config_service import ConfigService
from anima.llm import LLMClient
from anima.llm.usage import UsageEvent, UsageLedger, current_call_info, llm_scope, scoped_llm


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
