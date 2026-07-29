"""合并转发工具（A3 阶段新增）。

把近期高显著度的群聊 episode 整理成一份摘要文本，由 Bot 自行决定
是否作为合并转发消息发出。工具本身只负责产出文本，不涉及出站。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from anima.core.persistence import PersistenceProvider
from anima.memory.episodes import EpisodicMemory
from anima.tools import Tool, LEVEL_USER, get_call_context

logger = logging.getLogger("anima.tools.builtin.merge")


def _build_forward(messages: List[Dict], title: str = "群友意见整理") -> str:
    """构建合并转发内容。"""
    content = f"【{title}】\n\n"
    for i, msg in enumerate(messages, 1):
        content += f"{i}. {msg['user']}: {msg['text']}\n"
    return content


async def tool_merge_forward(query: str = "") -> str:
    """合并转发工具：整理近期相关群聊为一份摘要。"""
    ctx = get_call_context()
    detail_type = ctx.get("detail_type", "group")
    if detail_type != "group":
        return "本工具仅支持群聊"

    try:
        persistence = PersistenceProvider("data")
        memory = EpisodicMemory(persistence)
        memory.restore()
        relevant = [
            {"text": e.summary, "user": e.user_id}
            for e in memory.episodes[-10:]
            if getattr(e, "salience", 0) > 0.3
        ]
        if not relevant:
            return "未找到相关消息"
        return _build_forward(relevant)
    except Exception as exc:
        logger.warning("合并转发失败: %s", exc)
        return "整理失败喵，稍后再试"


def make_merge_forward() -> Tool:
    return Tool(
        "merge_forward",
        "把多条消息合并成转发消息（A3 阶段新增功能）",
        {"query": {"type": "string", "description": "用户的整理请求"}},
        tool_merge_forward,
        min_level=LEVEL_USER,
    )
