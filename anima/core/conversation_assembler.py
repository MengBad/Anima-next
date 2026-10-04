"""将连续输入合并为一个可理解的语义回合。

支持私聊和群聊两种模式：
- 私聊：按用户聚合，1.5s 窗口，8s 上限
- 群聊：按 (群, 用户) 聚合，1s 窗口，6s 上限，自适应防抖

🔴 群窗口（2026-10-03 调参）：2.0s → **1.0s**。只读取证 72h/1454 条群消息 + 离线复现
  GroupDebouncer：窗口 2.0s→1.0s 回合数仅 +4%（多出的回合大多仍被闸门 skip），却让
  每条群消息少等 1.0s（短句 3.0s→1.5s）。原窗口相对收益（合并率 11.2%）**过度配置**。
"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# 「事件正文」唯一口径。🔴 别在这里另写 `raw_message or message` 的 `or` 链：
# `raw_message` 恒缺（onebot11 只设 `message`）、`message` 是 CQ 原文 ⇒ 卡片/引用类
# 消息会被 CQ 清洗整段剥空（2026-09-25 定案）。
from anima.constants import event_text


@dataclass
class _PendingConversation:
    events: list[dict[str, Any]] = field(default_factory=list)
    first_at: float = field(default_factory=time.monotonic)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    timer: asyncio.Task[None] | None = None


def _carry_reply_attribution(merged: dict[str, Any], events: list[dict[str, Any]]) -> None:
    """把**引用相关字段**显式搬进合并结果。绝不抛出。

    🔴 为什么必须显式搬（2026-09-29 复核发现）：
    `_merge` 由 `deepcopy(events[-1])` 起步 + **白名单并集**，而 `reply_sender_*` /
    `reply_text` 是 handler 在**单条 event 上**落的（`_record_reply_attribution`），
    且落点在合并器**之前**（`handler._handle_inner`：落字段 → `ingest`）⇒ 群友
    「先引用、再补一句」时归属只挂在第一条上，合并后全丢。
    实测后果：引用**别人**的回合，组帧归属退化成空串（比旧文案还少一句）。

    语义与 `reply_message_id = reply_ids[-1]` 对齐：取**最后一条带引用的 event**，
    保证「被引 id」与「被引来源 / 被引正文」同源，不出现串台。
    """
    ref = None
    for item in events:
        if item.get("reply_message_id"):
            ref = item
    if ref is None:
        return
    for key in ("reply_text", "reply_sender_id", "reply_sender_name", "reply_is_unknown"):
        if ref.get(key) not in (None, ""):
            merged[key] = ref[key]


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
        # 「直接呼叫」归属必须取并集，不能继承最后一条：群友最自然的习惯是
        # 先 @bot 再补一句，若 merged 只带最后一条的 mentions_self=False，
        # 拼接后的 raw_message 里又含 [CQ:at,self]，回复闸门会把整轮误判成
        # 「@ 了别人」而静默吞掉（实测：@在前+补句在后 → mention_only/idle 零回复）。
        merged["mentions_self"] = any(bool(e.get("mentions_self")) for e in events)
        merged["reply_to_self"] = any(bool(e.get("reply_to_self")) for e in events)
        # 兼容字段：`raw_message` / `message` **保持传输原文语义**（CQ 原文），
        # 只是不再被当成正文消费（历史消费点见 `constants.event_text` 注释）。
        raw = "\n".join(t for t in (
            str(item.get("raw_message") or item.get("message") or "").strip()
            for item in events) if t)[:6000]
        merged["raw_message"] = raw
        merged["message"] = raw
        # 对话正文（唯一消费口径）。🔴 必须**显式写**：`merged` 由最后一条深拷贝而来，
        # 不写就只剩最后一条的正文 —— 拼接回合会「看得见补句、看不见前提」。
        merged["text"] = "\n".join(t for t in (
            event_text(item).strip() for item in events) if t)[:6000]
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
        _carry_reply_attribution(merged, events)
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


# ---------------------------------------------------------------------------
# B1: 群聊防抖（参考 astrbot_plugin_continuous_message）
# ---------------------------------------------------------------------------

class GroupDebouncer:
    """群聊消息防抖：同一用户 1s 内连续消息合并为一个语义回合。

    与私聊 ConversationAssembler 类似，但：
    - key 为 (group_id, user_id)
    - 支持自适应防抖（短句延长等待，长句缩短）
    - 支持撤回消息过滤
    - 指令消息（!anima）立即处理不进入合并
    """

    def __init__(
        self,
        wait_sec: float = 1.0,
        max_wait_sec: float = 6.0,
        adaptive: bool = True,
        short_msg_threshold: int = 10,
    ):
        self.wait_sec = max(0.0, wait_sec)
        self.max_wait_sec = max(self.wait_sec, max_wait_sec)
        self.adaptive = adaptive
        self.short_msg_threshold = short_msg_threshold
        self._pending: dict[tuple[int, int], _PendingConversation] = {}
        self._lock = asyncio.Lock()

    def _is_command(self, event: dict[str, Any]) -> bool:
        """判断是否为指令消息（不进入防抖）。

        🔴 走 `event_text`：此前读 CQ 原文，`[CQ:at,qq=bot] !anima …` 的前缀是 CQ 码
        ⇒ `startswith("!anima")` 永不命中，等于「@ 了 bot 的指令被当普通消息防抖」。
        """
        text = event_text(event)
        return text.strip().startswith("!anima")

    def _compute_delay(self, pending: _PendingConversation) -> float:
        """计算自适应延迟。"""
        elapsed = time.monotonic() - pending.first_at
        base_delay = self.wait_sec

        if self.adaptive and pending.events:
            # 获取最后一条消息的文本长度
            last_event = pending.events[-1]
            text = event_text(last_event)
            text_len = len(text.strip())

            if text_len <= self.short_msg_threshold:
                # 短句：用户可能还在打字，延长等待
                base_delay = min(self.wait_sec * 1.5, 3.0)
            elif text_len > 50:
                # 长句：已表达完整，缩短等待
                base_delay = max(self.wait_sec * 0.5, 1.0)

        # 不超过总上限
        return min(base_delay, max(0.0, self.max_wait_sec - elapsed))

    async def ingest(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """摄入群消息。返回合并结果或 None（表示已被合并）。"""
        # 指令消息立即处理
        if self._is_command(event):
            return event

        group_id = int(event.get("group_id", 0))
        user_id = int(event.get("user_id", 0))
        key = (group_id, user_id)

        async with self._lock:
            pending = self._pending.get(key)
            is_first = pending is None
            if pending is None:
                pending = _PendingConversation(events=[copy.deepcopy(event)])
                self._pending[key] = pending
            else:
                pending.events.append(copy.deepcopy(event))
            self._schedule(pending, key)

        if not is_first:
            return None
        await pending.ready.wait()
        async with self._lock:
            current = self._pending.pop(key, pending)
        # 可观测性（2026-10-03）：此前合并完全不打日志 ⇒ 合并率只能靠离线模拟猜。
        # 单条消息不记，避免刷屏；≥2 条才留痕（供 `[防抖]` 计数比对窗口调参效果）。
        if len(current.events) > 1:
            logger.info("[防抖] 合并 %d 条群消息 (群=%s 用户=%s)",
                        len(current.events), key[0], key[1])
        return self._merge(current.events)

    def remove_recalled(self, group_id: int, user_id: int, message_id: Any) -> None:
        """撤回消息过滤：从防抖队列中移除已撤回的消息。"""
        key = (group_id, user_id)
        pending = self._pending.get(key)
        if pending is None:
            return
        pending.events = [
            e for e in pending.events
            if e.get("message_id") != message_id
        ]

    def _schedule(self, pending: _PendingConversation, key: tuple[int, int]) -> None:
        if pending.timer and not pending.timer.done():
            pending.timer.cancel()
        delay = self._compute_delay(pending)
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
        """合并多条群消息。"""
        if not events:
            return {}
        merged = copy.deepcopy(events[-1])
        # 「直接呼叫」归属必须取并集，不能继承最后一条：群友最自然的习惯是
        # 先 @bot 再补一句，若 merged 只带最后一条的 mentions_self=False，
        # 拼接后的 raw_message 里又含 [CQ:at,self]，回复闸门会把整轮误判成
        # 「@ 了别人」而静默吞掉（实测：@在前+补句在后 → mention_only/idle 零回复）。
        merged["mentions_self"] = any(bool(e.get("mentions_self")) for e in events)
        merged["reply_to_self"] = any(bool(e.get("reply_to_self")) for e in events)
        # 兼容字段：`raw_message` / `message` **保持传输原文语义**（CQ 原文）。
        raw = "\n".join(t for t in (
            str(item.get("raw_message") or item.get("message") or "").strip()
            for item in events) if t)[:6000]
        merged["raw_message"] = raw
        merged["message"] = raw
        # 对话正文（唯一消费口径，见 `constants.event_text`）。与私聊合并器同源 ——
        # 两个 `_merge` 各写一份是历史包袱，改动必须成对，否则两条路径悄悄分叉。
        merged["text"] = "\n".join(t for t in (
            event_text(item).strip() for item in events) if t)[:6000]
        merged["assembled_message_ids"] = [
            item.get("message_id") for item in events if item.get("message_id") is not None
        ]
        # 合并图片
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
        # 保留最后一个引用 ID
        reply_ids = [item.get("reply_message_id") for item in events if item.get("reply_message_id")]
        if reply_ids:
            merged["reply_message_id"] = reply_ids[-1]
        _carry_reply_attribution(merged, events)
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
