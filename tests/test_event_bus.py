"""事件总线测试。"""

import pytest
from anima.core.event_bus import EventBus


@pytest.mark.asyncio
async def test_pubsub() -> None:
    bus = EventBus()
    got = []

    async def h(p):
        got.append(p)

    bus.subscribe("tick", h)
    await bus.publish("tick", 42)
    assert got == [42]


@pytest.mark.asyncio
async def test_unsubscribe() -> None:
    bus = EventBus()
    got = []

    async def h(p):
        got.append(p)

    bus.subscribe("x", h)
    bus.unsubscribe("x", h)
    await bus.publish("x", "should not arrive")
    assert got == []


@pytest.mark.asyncio
async def test_no_subscribers() -> None:
    bus = EventBus()
    await bus.publish("ghost")  # 不应抛异常
