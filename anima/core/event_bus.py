"""EventBus —— 轻量异步主题订阅 / 发布。"""

from __future__ import annotations
import asyncio
from collections import defaultdict
from typing import Awaitable, Callable, Any

Handler = Callable[[Any], Awaitable[None]]


class EventBus:
    def __init__(self):
        self._subs: dict[str, set[Handler]] = defaultdict(set)

    def subscribe(self, topic: str, handler: Handler) -> None:
        self._subs[topic].add(handler)

    def unsubscribe(self, topic: str, handler: Handler) -> None:
        self._subs[topic].discard(handler)

    async def publish(self, topic: str, payload: Any = None) -> None:
        handlers = list(self._subs.get(topic, ()))
        if not handlers:
            return
        await asyncio.gather(
            *(h(payload) for h in handlers), return_exceptions=True
        )
