"""群荣誉读者工具（A5 阶段新增，只读）。

注意：当前 Gateway 只暴露 send/send_file，并未提供原始 OneBot
call_action 接口，因此本工具在网关未开放该能力时优雅降级，
绝不抛出未捕获异常拖垮 bot。
"""
from __future__ import annotations

from typing import Any

from anima.tools import Tool, LEVEL_USER, get_call_context


async def tool_get_group_honor(group_id: int = 0, user_id: int = 0) -> str:
    """只读获取群荣誉信息（A5 严格只读）。"""
    ctx = get_call_context()
    gid = int(group_id or ctx.get("group_id") or 0)
    if not gid:
        return "没拿到群号，本喵不知道要查哪个群喵"

    try:
        from anima.core.gateway import get_gateway

        gateway = get_gateway()
        if gateway is None or not hasattr(gateway, "call_action"):
            return "本喵暂时还不能实时拉群荣誉榜喵（网关未开放该接口）"
        honor = await gateway.call_action(
            "get_group_honor_info", {"group_id": gid, "type": "talkative"}
        )
        if honor and "current_talkative" in honor:
            cur = honor["current_talkative"]
            return (
                f"本群本周话痨王是 {cur.get('nickname', '某人')}，"
                f"已经连续霸榜 {cur.get('day_count', 0)} 天了喵"
            )
    except Exception:
        return "这个群好像没开荣誉榜喵"
    return "本喵只读群荣誉信息喵"


def make_group_honor() -> Tool:
    return Tool(
        "get_group_honor",
        "只读获取群荣誉/活跃榜数据（A5 阶段新增功能）",
        {"group_id": {"type": "integer", "description": "群号，可省略（默认当前群）"}},
        tool_get_group_honor,
        min_level=LEVEL_USER,
    )
