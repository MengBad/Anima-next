"""x_search 工具：让 Agent 随时调用 X（Twitter）搜索/查内容，不依赖特定触发词。

Agent 在需要实时、X 平台专属、或用户明确要求「去 X 查/在 X 上搜」的信息时使用。
结果经 x_browser 的合规闸门（政治拒、违法R18 拒）后返回文本摘要。
未登录态时结果可能受限，会在返回里注明。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("anima.tools.x_search")


async def tool_x_search(query: str, limit: int = 5) -> str:
    """在 X（Twitter）上搜索内容并返回摘要。

    适用于：需要 X 平台实时/专属信息（如某账号动态、某话题讨论、用户要求
    「去 X 查一下 XX」）。返回命中的若干条推文文本（已过滤政治与违法内容）。
    注意：结果受账号登录态与每日额度限制；若返回为空可能是额度用尽或未登录。

    Args:
        query: 搜索关键词（中文/英文均可）。
        limit: 返回条数，默认 5，范围 1-10。
    """
    query = str(query or "").strip()
    if not query:
        return "未提供搜索关键词。"
    try:
        from anima.cognitive.x_browser import get_x_browser
    except Exception as e:  # noqa: BLE001
        logger.warning("x_browser 不可用: %s", e)
        return "X 搜索模块不可用（x_browser 未加载）。"

    limit = max(1, min(10, int(limit or 5)))
    try:
        session = get_x_browser()
        if session is None or not session.enabled:
            return "X 浏览器未启用（config.x_browser.enabled=false）。"
        res = await session.search(query, limit=limit)
    except Exception as e:  # noqa: BLE001
        logger.warning("x_search 调用失败（已隔离）: %s", e)
        return f"X 搜索失败：{e}"

    if not res.get("ok"):
        note = res.get("note", "")
        return f"X 搜索未完成：{note}" if note else "X 搜索未完成（未知原因）。"

    tweets = res.get("tweets", [])
    if not tweets:
        note = res.get("note", "")
        return ("X 上未找到相关公开内容。" + (f"（{note}）" if note else ""))

    lines = []
    for i, t in enumerate(tweets[:limit], 1):
        lines.append(f"{i}. {t}")
    header = "X 搜索结果（已过滤政治/违法内容）："
    note = res.get("note", "")
    if note:
        header += f"\n（{note}）"
    return header + "\n" + "\n".join(lines)
