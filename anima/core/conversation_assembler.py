"""将连续私聊输入合并为一个可理解的语义回合。"""

from __future__ import annotations

import asyncio
import copy
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _PendingConversation:
    events: list[dict[str, Any]] = field(default_factory=list)
    first_at: float = field(default_factory=time.monotonic)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    timer: asyncio.Task[None] | None = None


class ConversationAssembler:
    """按私聊用户聚合短时间连续消息。

    第一条消息的调用者等待窗口结束并获得合并事件；窗口期间的后续调用立即
    返回 ``None``，因此同一串输入只会触发一次模型调用。
    """

    def __init__(self, wait_sec: float = 1.5, max_wait_sec: float = 8.0):
        self.wait_sec = max(0.0, wait_sec)
        self.max_wait_sec = max(self.wait_sec, max_wait_sec)
        self._pending: dict[int, _PendingConversation] = {}
        self._lock = asyncio.Lock()

    async def ingest(self, event: dict[str, Any]) -> dict[str, Any] | None:
        user_id = int(event.get("user_id", 0))
        async with self._lock:
            pending = self._pending.get(user_id)
            is_first = pending is None
            if pending is None:
                pending = _PendingConversation(events=[copy.deepcopy(event)])
                self._pending[user_id] = pending
            else:
                pending.events.append(copy.deepcopy(event))
            self._schedule(pending)

        if not is_first:
            return None
        await pending.ready.wait()
        async with self._lock:
            current = self._pending.pop(user_id, pending)
        return self._merge(current.events)

    def _schedule(self, pending: _PendingConversation) -> None:
        if pending.timer and not pending.timer.done():
            pending.timer.cancel()
        elapsed = time.monotonic() - pending.first_at
        delay = min(self.wait_sec, max(0.0, self.max_wait_sec - elapsed))
        pending.timer = asyncio.create_task(self._release_after(pending, delay))

    @staticmethod
    async def _release_after(pending: _PendingConversation, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            pending.ready.set()
        except asyncio.CancelledError:
            return

    @staticmethod
    def _merge(events: list[dict[str, Any]]) -> dict[str, Any]:
        merged = copy.deepcopy(events[-1])
        texts = [str(item.get("raw_message") or item.get("message") or "").strip()
                 for item in events]
        text = "\n".join(item for item in texts if item)[:6000]
        merged["raw_message"] = text
        merged["message"] = text
        merged["assembled_message_ids"] = [
            item.get("message_id") for item in events if item.get("message_id") is not None
        ]
        # 连续输入可能是“先发图/语音，再补一句说明”。必须合并所有媒体，不能只
        # 保留最后一条事件，否则文本组装成功却把真正被询问的附件丢掉。
        images: list[dict[str, Any]] = []
        seen_images: set[str] = set()
        for item in events:
            for image in item.get("images") or []:
                if not isinstance(image, dict):
                    continue
                identity = str(image.get("file") or image.get("url") or image.get("id") or image)
                if identity in seen_images:
                    continue
                seen_images.add(identity)
                images.append(copy.deepcopy(image))
        if images:
            merged["images"] = images[:8]

        records: list[dict[str, Any]] = []
        seen_records: set[str] = set()
        for item in events:
            for record in item.get("records") or item.get("audio") or []:
                if not isinstance(record, dict):
                    continue
                identity = str(record.get("file") or record.get("url") or record)
                if identity in seen_records:
                    continue
                seen_records.add(identity)
                records.append(copy.deepcopy(record))
        if records:
            merged["records"] = records[:4]

        reply_ids = [item.get("reply_message_id") for item in events if item.get("reply_message_id")]
        if reply_ids:
            merged["reply_message_id"] = reply_ids[-1]
        merged["assembled_count"] = len(events)
        return merged

    async def close(self) -> None:
        async with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            if item.timer and not item.timer.done():
                item.timer.cancel()
            item.ready.set()
