"""query_web_memory —— 查询 Bot 之前从网页搜索中学到的知识（P1）。

放在 web_search 之前注册，使 ReAct 在搜网前先命中已记忆的网页知识，
减少无效搜网与隐私外传。仅依赖标准库路径，无循环依赖。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("anima.tools.web_memory")


def tool_query_web_memory(query: str) -> str:
    """查询 Bot 已从网页搜索中学到的知识。

    当用户问常识性/事实性问题（历史、技术、定义、天气、新闻等），先调用此工具；
    若返回有效记忆则无需重复搜网。仅命中缓存时才返回内容，否则提示去搜网。
    """
    query = str(query or "").strip()
    if not query:
        return "请提供要查询的关键词。"
    try:
        from anima.memory.web_knowledge import WebKnowledgeStore
        store = WebKnowledgeStore()
        matches = store.query(query, top_k=3)
    except Exception as e:
        logger.warning("query_web_memory 读取失败: %s", e)
        return "没有相关的网络记忆，建议直接搜索网页。"

    if not matches:
        return "没有相关的网络记忆，建议直接搜索网页。"

    lines = []
    for m in matches:
        src = ", ".join(u for u in m.source_urls[:2] if u)
        src_part = f" (来源: {src})" if src else ""
        lines.append(
            f"[来自网页，{m.confidence:.0%}可信度] {m.summary}{src_part}"
        )
    return "以下是之前从网页学到的相关内容：\n" + "\n".join(lines)
