import asyncio
import json

import pytest

from anima.channels.onebot11 import OneBot11Adapter


@pytest.mark.asyncio
async def test_background_event_limit_drops_only_when_backlogged(monkeypatch):
    """消息洪峰不能无限创建后台任务，WS 接收循环仍应保持可用。"""
    adapter = OneBot11Adapter(event_concurrency=1, max_pending_events=1)
    release = asyncio.Event()

    async def blocked_dispatch(_raw):
        await release.wait()

    monkeypatch.setattr(adapter, "_dispatch_event", blocked_dispatch)
    raw = json.dumps({"post_type": "meta_event", "meta_event_type": "lifecycle"})
    await adapter._handle_message(raw, background=True)
    await asyncio.sleep(0)
    assert len(adapter._event_tasks) == 1

    await adapter._handle_message(raw, background=True)
    assert adapter.events_dropped == 1
    assert adapter.health()["events_backpressure_dropped"] == 1
    assert adapter.health()["event_drop_reasons"] == {
        "backpressure": 1,
        "unsupported": 0,
        "self_echo": 0,
        "duplicate": 0,
    }

    release.set()
    await asyncio.gather(*list(adapter._event_tasks), return_exceptions=True)


@pytest.mark.asyncio
async def test_health_exposes_event_backpressure_limits():
    adapter = OneBot11Adapter(event_concurrency=3, max_pending_events=9)
    health = adapter.health()
    assert health["event_concurrency"] == 3
    assert health["max_pending_events"] == 9
    assert health["active_event_tasks"] == 0
    assert health["events_backpressure_dropped"] == 0


@pytest.mark.asyncio
async def test_priority_private_message_uses_soft_backpressure_slot(monkeypatch):
    """私聊在硬上限已满时仍可占用有限软容量，避免被普通洪峰误杀。"""
    adapter = OneBot11Adapter(event_concurrency=1, max_pending_events=1)
    release = asyncio.Event()

    async def blocked_dispatch(_raw):
        await release.wait()

    monkeypatch.setattr(adapter, "_dispatch_event", blocked_dispatch)
    noise = json.dumps({"post_type": "meta_event", "meta_event_type": "lifecycle"})
    private = json.dumps({
        "post_type": "message",
        "message_type": "private",
        "user_id": 10001,
        "message": [{"type": "text", "data": {"text": "主人私聊"}},],
    })
    await adapter._handle_message(noise, background=True)
    await asyncio.sleep(0)
    assert len(adapter._event_tasks) == 1

    await adapter._handle_message(private, background=True)
    assert len(adapter._event_tasks) == 2
    assert adapter.events_dropped == 0

    release.set()
    await asyncio.gather(*list(adapter._event_tasks), return_exceptions=True)
