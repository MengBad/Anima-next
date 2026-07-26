"""内置社交/群管理工具（需要 OneBot11 通道）。"""

from __future__ import annotations

import logging

from anima.tools import get_call_context

logger = logging.getLogger("anima.tools.social")


def _get_channel(core):
    """helper: 拿到 OneBot11 通道。"""
    gw = getattr(core, "gateway", None)
    return getattr(gw, "channel", None) if gw else None


def make_group_poke(core):
    async def _tool_group_poke(user_id: int, group_id: int | None = None) -> str:
        """戳一戳群里某人。"""
        ch = _get_channel(core)
        if ch is None or not hasattr(ch, "send_group_poke"):
            return "戳一戳功能不可用（OneBot 通道未连接）"
        ctx = get_call_context()
        gid = group_id or ctx.get("group_id")
        if not gid:
            return "需要指定群号才能在群里戳人"
        ok = await ch.send_group_poke(int(gid), int(user_id))
        return f"已戳了 {user_id} 一下" if ok else "戳一戳失败，可能对方不在群里"
    return _tool_group_poke


def make_get_group_members(core):
    async def _tool_get_group_members(group_id: int | None = None,
                                       user_id: int | None = None) -> str:
        """查看群成员列表或某人信息。"""
        ch = _get_channel(core)
        if ch is None:
            return "群成员查询不可用（OneBot 通道未连接）"
        ctx = get_call_context()
        gid = group_id or ctx.get("group_id")
        if not gid:
            return "需要指定群号才能查询群成员"
        gid = int(gid)
        if user_id:
            info = await ch.get_group_member_info(gid, int(user_id))
            if not info:
                return f"没找到 {user_id} 在群 {gid} 里的信息"
            name = info.get("card") or info.get("nickname") or str(user_id)
            role = info.get("role", "member")
            return f"群成员：{name}（QQ {user_id}，角色 {role}）"
        members = await ch.get_group_member_list(gid)
        if not members:
            return f"获取群 {gid} 成员列表失败"
        total = len(members)
        sample = members[:15]
        lines = [f"群 {gid} 共 {total} 人："]
        for m in sample:
            name = m.get("card") or m.get("nickname") or str(m.get("user_id", "?"))
            lines.append(f"- {name} ({m.get('user_id', '?')})")
        if total > 15:
            lines.append(f"…等共 {total} 人")
        return "\n".join(lines)
    return _tool_get_group_members


def make_get_group_info(core):
    async def _tool_get_group_info(group_id: int | None = None) -> str:
        """查看群基本信息。"""
        ch = _get_channel(core)
        if ch is None or not hasattr(ch, "get_group_info"):
            return "群信息查询不可用"
        ctx = get_call_context()
        gid = group_id or ctx.get("group_id")
        if not gid:
            return "需要指定群号"
        info = await ch.get_group_info(int(gid))
        if not info:
            return f"获取群 {gid} 信息失败"
        name = info.get("group_name", "未知")
        count = info.get("member_count", "?")
        max_count = info.get("max_member_count", "?")
        return f"群名：{name}，成员 {count}/{max_count}"
    return _tool_get_group_info


def make_set_group_card(core):
    async def _tool_set_group_card(user_id: int, card: str,
                                    group_id: int | None = None) -> str:
        """修改群名片。"""
        ch = _get_channel(core)
        if ch is None or not hasattr(ch, "set_group_card"):
            return "修改名片功能不可用"
        ctx = get_call_context()
        gid = group_id or ctx.get("group_id")
        if not gid:
            return "需要指定群号"
        ok = await ch.set_group_card(int(gid), int(user_id), card)
        return f"已将 {user_id} 的群名片改为「{card}」" if ok else "修改失败，可能权限不足"
    return _tool_set_group_card


def make_group_ban(core):
    async def _tool_group_ban(user_id: int, minutes: int = 10,
                               group_id: int | None = None) -> str:
        """禁言某人。"""
        ch = _get_channel(core)
        if ch is None or not hasattr(ch, "set_group_ban"):
            return "禁言功能不可用"
        ctx = get_call_context()
        gid = group_id or ctx.get("group_id")
        if not gid:
            return "需要指定群号"
        duration = max(0, int(minutes)) * 60  # 分钟转秒
        ok = await ch.set_group_ban(int(gid), int(user_id), duration)
        if minutes == 0:
            return f"已解除 {user_id} 的禁言" if ok else "解禁失败"
        return f"已禁言 {user_id} {minutes} 分钟" if ok else "禁言失败，可能权限不足或对方是管理员"
    return _tool_group_ban


def make_group_kick(core):
    async def _tool_group_kick(user_id: int, group_id: int | None = None) -> str:
        """踢出某人。"""
        ch = _get_channel(core)
        if ch is None or not hasattr(ch, "set_group_kick"):
            return "踢人功能不可用"
        ctx = get_call_context()
        gid = group_id or ctx.get("group_id")
        if not gid:
            return "需要指定群号"
        ok = await ch.set_group_kick(int(gid), int(user_id))
        return f"已将 {user_id} 移出群聊" if ok else "踢人失败，可能权限不足或对方是管理员"
    return _tool_group_kick
