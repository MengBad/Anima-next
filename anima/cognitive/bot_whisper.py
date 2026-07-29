"""Bot 间私聊通道（可选高级）。

通过 NapCat 私聊接口让 Bot 之间私下沟通，群友不可见。
- 所有私聊内容强制标记 [Bot内部]，防止被误展示给人类或误当成用户消息。
- 每日上限受 SocialConfig.bot_whisper_daily_limit 约束。
- 私聊内容绝不写入 chat_memory.json（不污染人类对话历史）。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger("anima.cognitive.bot_whisper")

_INTERNAL_TAG = "[Bot内部]"


def compose_whisper(
    target_uid: Any,
    message: str,
    data_dir: str = "data",
    now: float | None = None,
    daily_limit: int = 10,
) -> Optional[str]:
    """构造一条 Bot 内部私聊消息（带 [Bot内部] 标记），并做日限额检查。

    返回最终文本；超过日限额返回 None（不发送）。不实际发送、不写记忆。
    """
    if not message or not str(target_uid):
        return None
    now = now or time.time()
    from anima.cognitive.bot_social import BotCooldownStore
    store = BotCooldownStore(data_dir)
    if store.whisper_count_today(now) >= int(daily_limit):
        logger.info("[Bot私聊] 已达每日上限 %d，跳过", daily_limit)
        return None
    return f"{_INTERNAL_TAG} {message}"


async def whisper_to_bot(
    gateway: Any,
    target_uid: Any,
    message: str,
    data_dir: str = "data",
    now: float | None = None,
    daily_limit: int = 10,
) -> Optional[str]:
    """经 NapCat 私聊向另一个 Bot 发送内部消息，返回已发送文本或 None。

    私聊内容不进入 chat_memory.json。
    """
    text = compose_whisper(target_uid, message, data_dir, now, daily_limit)
    if text is None:
        return None
    now = now or time.time()
    try:
        from anima.core.gateway import OutboundMsg
        await gateway.send(OutboundMsg("private", int(target_uid), text))
        from anima.cognitive.bot_social import BotCooldownStore
        BotCooldownStore(data_dir).bump_whisper_count(now)
        logger.info("[Bot私聊] → Bot %s: %s", target_uid, message)
        return text
    except Exception:  # noqa: BLE001
        logger.exception("[Bot私聊] 发送失败")
        return None
