"""Gateway —— 消息路由中枢：接收渠道事件 + 出站发送。"""

from __future__ import annotations
import logging
from typing import TYPE_CHECKING, Optional, Any

if TYPE_CHECKING:
    from anima.core.anima_core import AnimaCore
    from anima.channels.onebot11 import OneBot11Adapter

logger = logging.getLogger("anima.gateway")


# 出站消息结构
class OutboundMsg:
    __slots__ = ("detail_type", "target_id", "message", "auto_escape", "extra")
    def __init__(
        self,
        detail_type: str,
        target_id: int,
        message: str | list[dict[str, Any]],
        auto_escape: bool = False,
        extra: Optional[dict] = None,
    ):
        self.detail_type = detail_type
        self.target_id = target_id
        self.message = message
        self.auto_escape = auto_escape
        self.extra = extra or {}

    def is_rich(self) -> bool:
        return isinstance(self.message, list)


class Gateway:
    """消息路由中枢。"""

    def __init__(self, core: "AnimaCore"):
        self.core = core
        self.channel: Optional["OneBot11Adapter"] = None

    def bind(self, channel: "OneBot11Adapter") -> None:
        """绑定渠道适配器。"""
        self.channel = channel
        logger.info("Gateway 绑定渠道: %s", channel.name)

    async def on_event(self, event: dict) -> None:
        """入站事件：已由渠道发布到 EventBus，Gateway 仅做日志/统计。"""
        topic = event.get("topic", "?")
        user_id = event.get("user_id", "?")
        # 聊天正文属于用户数据，不应在默认 systemd journal 中长期留存。
        # 链路定位使用 topic / user / 长度即可；需要正文时再临时提升调试级别。
        text_len = len(event.get("text", "") or "")
        logger.info("[入站] %s | user=%s | text_len=%d", topic, user_id, text_len)

    async def send(self, msg: OutboundMsg) -> Optional[int]:
        """出站消息：通过绑定的渠道发送。

        Returns:
            message_id 或 None（发送失败）
        """
        if self.channel is None:
            logger.warning("Gateway 未绑定渠道，消息丢弃")
            return None

        message_id = await self.channel.send_message(
            detail_type=msg.detail_type,
            target_id=msg.target_id,
            message=msg.message,
            auto_escape=msg.auto_escape,
            extra=msg.extra,
        )
        audit = getattr(self.core, "audit", None)
        if audit:
            if isinstance(msg.message, str):
                text_length = len(msg.message)
            elif isinstance(msg.message, list):
                text_length = sum(
                    len(seg.get("text", "")) for seg in msg.message
                    if isinstance(seg, dict)
                )
            else:
                text_length = 0
            audit.emit(
                "message_sent",
                detail_type=msg.detail_type,
                target_id=msg.target_id,
                message_id=message_id,
                ok=message_id is not None,
                text_length=text_length,
            )
        quality = getattr(self.core, "reply_quality", None)
        if quality:
            try:
                quality.record_outbound(msg.message, message_id is not None)
            except Exception:
                logger.exception("回复质量出站统计失败")
        return message_id

    async def send_file(
        self,
        msg: OutboundMsg,
    ) -> tuple[Optional[int], Optional[dict]]:
        """出站文件：上传文件并发送提示消息。

        msg.extra 中可包含:
            - file_path: 本地文件路径（必填）
            - name: 自定义文件名
            - caption: 上传后提示文字
        """
        if self.channel is None:
            logger.warning("Gateway 未绑定渠道，文件消息丢弃")
            return None, None

        extra = msg.extra or {}
        file_path = extra.get("file_path")
        if not file_path:
            logger.warning("send_file 缺少 file_path")
            return None, None

        return await self.channel.send_file(
            detail_type=msg.detail_type,
            target_id=msg.target_id,
            file_path=file_path,
            name=extra.get("name"),
            caption=msg.message if isinstance(msg.message, str) else "",
        )
