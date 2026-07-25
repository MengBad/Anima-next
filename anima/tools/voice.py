"""AI 语音消息工具 —— 参考 astrbot_plugin_qzone_tools 的 send_ai_voice 实现。

通过 NapCat 的 send_ai_voice API 发送 QQ 官方 TTS 语音消息。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

TOOL_META = {
    "name": "send_voice",
    "description": "用语音说一句话。使用 QQ 官方 AI 语音。",
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "要说的内容",
            },
        },
        "required": ["text"],
    },
    "permission": "user",
}


async def run(text: str, context: dict[str, Any]) -> str:
    """发送语音消息。"""
    core = context.get("core")
    if core is None:
        return "错误：无法获取 core"

    voice_cfg = getattr(core.config, "voice", None)
    if voice_cfg and not getattr(voice_cfg, "enabled", False):
        return "语音功能未启用"

    detail_type = context.get("detail_type", "group")
    group_id = context.get("group_id")
    user_id = context.get("user_id")

    try:
        gateway = core.gateway
        # NapCat send_ai_voice API
        if detail_type == "group" and group_id:
            await gateway._call_action("send_ai_voice", {
                "group_id": group_id,
                "text": text,
            })
        else:
            await gateway._call_action("send_ai_voice", {
                "user_id": user_id,
                "text": text,
            })
        logger.info("语音消息已发送: %s", text[:30])
        return f"已发送语音：{text[:30]}..."
    except Exception as e:
        logger.exception("语音发送失败")
        return f"语音发送失败：{e}"
