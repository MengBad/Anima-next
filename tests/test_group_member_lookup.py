"""群成员查询（含显示名→QQ 解析）回归测试。

核心能力：用户说"看看张三的信息"时，bot 应能按显示名/群名片把名字解析成
QQ 号再查资料，而不是只能接收裸 QQ 号。多命中要给候选列表让用户确认，
无命中要明确告知。

权限：按用户 q-0 选择保持 LEVEL_USER 开放（任意成员 @bot 即可查任意人），
本次不做限制闸。
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from anima.tools.builtin.social import (
    make_get_group_members,
    _resolve_name_to_uids,
)

GROUP = 727631526

MEMBERS = [
    {"user_id": 111, "card": "张三", "nickname": "zhangsan"},
    {"user_id": 222, "card": "李四", "nickname": "lisi"},
    {"user_id": 333, "card": "张三丰", "nickname": "zsf"},
]


def _ch():
    ch = MagicMock()
    ch.get_group_member_list = AsyncMock(return_value=MEMBERS)
    ch.get_group_member_info = AsyncMock(
        return_value={"card": "张三", "nickname": "zhangsan", "role": "member"})
    return ch


def _core(ch):
    core = MagicMock()
    gw = MagicMock()
    gw.channel = ch
    core.gateway = gw
    return core


def test_resolve_exact_single():
    """精确匹配显示名 → 返回唯一 uid，无候选。"""
    ch = _ch()
    uid, cands = asyncio.run(_resolve_name_to_uids(ch, GROUP, "张三"))
    assert uid == 111 and cands is None


def test_resolve_ambiguous():
    """模糊前缀「张」命中两人 → uid=None 且给出候选列表。"""
    ch = _ch()
    uid, cands = asyncio.run(_resolve_name_to_uids(ch, GROUP, "张"))
    assert uid is None and cands is not None and len(cands) == 2


def test_resolve_notfound():
    """无匹配 → (None, None)。"""
    ch = _ch()
    uid, cands = asyncio.run(_resolve_name_to_uids(ch, GROUP, "王五"))
    assert uid is None and cands is None


def test_tool_by_name():
    """按名字查 → 返回该成员资料（含 QQ 号）。"""
    ch = _ch()
    core = _core(ch)
    tool = make_get_group_members(core)
    out = asyncio.run(tool(group_id=GROUP, name="张三"))
    assert "张三" in out and "111" in out


def test_tool_ambiguous_name():
    """按模糊名查多命中 → 提示匹配到多个。"""
    ch = _ch()
    core = _core(ch)
    tool = make_get_group_members(core)
    out = asyncio.run(tool(group_id=GROUP, name="张"))
    assert "匹配到多个" in out


def test_tool_notfound_name():
    """按名字查无果 → 明确告知没找到。"""
    ch = _ch()
    core = _core(ch)
    tool = make_get_group_members(core)
    out = asyncio.run(tool(group_id=GROUP, name="王五"))
    assert "没找到" in out


def test_tool_by_user_id_still_works():
    """原有按 QQ 号查的路径不受影响。"""
    ch = _ch()
    core = _core(ch)
    tool = make_get_group_members(core)
    out = asyncio.run(tool(group_id=GROUP, user_id=111))
    assert "张三" in out and "111" in out
