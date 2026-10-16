"""群荣誉榜工具（只读）。

P2-4 修复——**这是典型的「假完整」**：

本工具自 A5 起就注册在默认工具表里，schema 正常、权限 LEVEL_USER、描述体面，
但实现里写的是 ``from anima.core.gateway import get_gateway``，而该函数在
``anima/core/gateway.py`` 中**从未定义**。import 又被 ``try/except`` 包住，
于是它既不报错也不工作，**100%** 走 ``except`` 分支回一句
「这个群好像没开荣誉榜喵」——把「我们的 bug」伪装成「群没开这个功能」，
连日志都没有。同类问题见 ``cognitive/intent_generator.py``（P1-2，已修）。

现在改为与 ``social.py`` 相同的真实通道访问路径：

    core.gateway.channel（OneBot11Adapter）→ get_group_honor_info

只读、无副作用、无提权。通道未连接 / 接口不支持时**如实说明**，
绝不伪造荣誉数据。
"""
from __future__ import annotations

from typing import Any

from anima.tools import Tool, LEVEL_USER, get_call_context
from anima.tools.builtin.social import _get_channel

# OneBot11 get_group_honor_info 的五类榜单 → 中文名。
# ``current_talkative`` 是独立字段（当前龙王），其余为列表。
_HONOR_LABELS: dict[str, str] = {
    "performer": "群聊之火",
    "legend": "群聊炽焰",
    "strong_newbie": "冒尖小春笋",
    "emotion": "快乐源泉",
    "talkative": "龙王榜",
}
_HONOR_TYPE_ALIASES: dict[str, str] = {
    "": "all", "all": "all", "全部": "all",
    "talkative": "talkative", "龙王": "talkative",
    "performer": "performer", "群聊之火": "performer",
    "legend": "legend", "群聊炽焰": "legend",
    "strong_newbie": "strong_newbie", "冒尖小春笋": "strong_newbie",
    "emotion": "emotion", "快乐源泉": "emotion",
}


def _entry_label(item: Any) -> str:
    """把一条荣誉条目渲染成「昵称（描述）」。"""
    if not isinstance(item, dict):
        return str(item)
    who = str(item.get("nickname") or item.get("user_id") or "").strip()
    desc = str(item.get("description") or "").strip()
    if desc:
        return f"{who}（{desc}）"
    return who or "某人"


def make_group_honor(core) -> Tool:
    """构造 get_group_honor 工具（需要 core 才能取到 OneBot11 通道）。"""

    async def _tool_get_group_honor(group_id: int = 0,
                                    honor_type: str = "all",
                                    user_id: int = 0) -> str:
        """只读获取群荣誉榜（龙王 / 群聊之火 / 群聊炽焰等）。"""
        ch = _get_channel(core)
        if ch is None or not hasattr(ch, "get_group_honor_info"):
            return "群荣誉榜查询不可用（OneBot 通道未连接）"

        ctx = get_call_context()
        gid = int(group_id or ctx.get("group_id") or 0)
        if not gid:
            return "需要指定群号才能查荣誉榜"

        ht = _HONOR_TYPE_ALIASES.get(str(honor_type or "").strip().lower())
        if ht is None:
            return (f"不认识的荣誉类型「{honor_type}」；"
                    f"可用：all / talkative / performer / legend / strong_newbie / emotion")

        try:
            info = await ch.get_group_honor_info(gid, ht)
        except Exception as exc:  # 通道异常绝不拖垮 bot
            return "群荣誉榜查询出错，暂时拿不到数据"
        if not isinstance(info, dict) or not info:
            # 与「群没开这个功能」区分：这里是我们拿不到数据，必须如实说。
            return (f"没能从 OneBot 取到群 {gid} 的荣誉榜数据"
                    f"（可能未连接、超时，或该实现不支持此接口）")

        lines: list[str] = []
        # 当前龙王：独立字段，带连续霸榜天数
        cur = info.get("current_talkative")
        if isinstance(cur, dict) and (cur.get("nickname") or cur.get("user_id")):
            days = cur.get("day_count")
            suffix = f"，已连续霸榜 {days} 天" if days else ""
            lines.append(f"当前龙王：{_entry_label(cur)}{suffix}")

        for key, label in _HONOR_LABELS.items():
            items = info.get(f"{key}_list") if key != "talkative" else info.get("talkative_list")
            if not isinstance(items, list) or not items:
                continue
            tops = [_entry_label(it) for it in items[:3]]
            lines.append(f"{label}：{'、'.join(tops)}")

        # 指定某个成员时，回答「ta 有没有上榜」而不是整份榜单
        if user_id:
            uid = int(user_id)
            hit: list[str] = []
            if isinstance(cur, dict) and int(cur.get("user_id") or 0) == uid:
                hit.append("当前龙王")
            for key, label in _HONOR_LABELS.items():
                items = info.get("talkative_list") if key == "talkative" else info.get(f"{key}_list")
                if not isinstance(items, list):
                    continue
                for it in items:
                    if isinstance(it, dict) and int(it.get("user_id") or 0) == uid:
                        hit.append(label)
                        break
            if hit:
                return f"{uid} 在群 {gid} 的荣誉：{'、'.join(hit)}"
            return f"{uid} 目前不在群 {gid} 的荣誉榜上"

        if not lines:
            return f"群 {gid} 目前没有可读的荣誉数据（可能荣誉榜未开启，或群里还没产生记录）"
        return f"群 {gid} 荣誉榜：\n" + "\n".join(f"- {ln}" for ln in lines)

    return Tool(
        "get_group_honor",
        "只读查询本群荣誉榜：当前龙王、群聊之火、群聊炽焰、冒尖小春笋、快乐源泉。"
        "当有人问「谁是龙王」「这个群最活跃的是谁」「最近谁话最多」时调用。"
        "只读、不可写，拿不到数据时会如实说明，不要把空结果编成结论。",
        {
            "group_id": {"type": "integer", "description": "群号，可省略（默认当前群）"},
            "honor_type": {"type": "string",
                           "description": "荣誉类型，默认 all；可选 talkative/performer/legend/strong_newbie/emotion"},
            "user_id": {"type": "integer", "description": "可选：只查某人是否上榜"},
        },
        _tool_get_group_honor,
        min_level=LEVEL_USER,
        # 三个参数都可省略（默认当前群 / 全部榜单 / 不限定成员）。
        # 不显式声明会让 to_openai_schema 回退成「全部必填」，与描述自相矛盾。
        required=[],
    )
