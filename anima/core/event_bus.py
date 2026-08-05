"""EventBus —— 轻量异步主题订阅 / 发布。"""

from __future__ import annotations
import asyncio
import logging
from collections import defaultdict
from typing import Awaitable, Callable, Any

logger = logging.getLogger("anima.eventbus")
Handler = Callable[[Any], Awaitable[None]]


class EventBus:
    def __init__(self, raise_on_error: bool = False):
        self._subs: dict[str, set[Handler]] = defaultdict(set)
        # 测试可置 True 让 handler 异常直接重抛（暴露被静默吞掉的真因）；
        # 生产默认 False：异常只 log + 隔离，绝不拖垮整个事件分发。
        self.raise_on_error = raise_on_error

    def subscribe(self, topic: str, handler: Handler) -> None:
        self._subs[topic].add(handler)

    def unsubscribe(self, topic: str, handler: Handler) -> None:
        self._subs[topic].discard(handler)

    async def publish(self, topic: str, payload: Any = None) -> None:
        handlers = list(self._subs.get(topic, ()))
        if not handlers:
            return
        results = await asyncio.gather(
            *(h(payload) for h in handlers), return_exceptions=True
        )
        for r in results:
            if isinstance(r, Exception):
                if self.raise_on_error:
                    raise r
                logger.exception("EventBus handler 异常 (topic=%s)", topic, exc_info=r)
