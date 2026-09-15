"""P2-4 测试：群荣誉榜工具真实接线（修「假完整」）+ 生产接线清单一致性。

背景：``anima/tools/builtin/honor.py`` 自 A5 起注册在默认工具表，但实现调用的是
``anima.core.gateway.get_gateway`` —— 该函数全树无定义，import 又被 try/except
吞掉，于是工具**永远**回「这个群好像没开荣誉榜喵」：注册表里有、schema 正常、
实际 100% 失效，且把「我们的 bug」说成「群没开这个功能」。

本测试锁死三件事：
1. 通道可用时必须真的拿到荣誉数据（不是恒失败）；
2. 通道拿不到数据时必须**如实说明**，不能伪装成「群没开功能」；
3. 代码里不再出现 `get_gateway` 这个幽灵符号（防止回归）。
"""

from __future__ import annotations

import inspect

import pytest

from anima.tools import LEVEL_USER, create_default_registry
from anima.tools.builtin.honor import make_group_honor


# ---------------------------------------------------------------------------
# 替身：最小 core + 可编程通道
# ---------------------------------------------------------------------------

class _FakeChannel:
    def __init__(self, payload):
        self.payload = payload
        self.calls: list[tuple[str, str]] = []

    async def get_group_honor_info(self, group_id: int, honor_type: str = "all"):
        self.calls.append((int(group_id), honor_type))
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class _FakeGateway:
    def __init__(self, channel):
        self.channel = channel


class _FakeCore:
    def __init__(self, channel=None):
        self.gateway = _FakeGateway(channel)


HONOR_PAYLOAD = {
    "group_id": 727631526,
    "current_talkative": {"user_id": 1001, "nickname": "话痨甲", "day_count": 4},
    "talkative_list": [
        {"user_id": 1001, "nickname": "话痨甲", "description": "7天"},
        {"user_id": 1002, "nickname": "话痨乙"},
    ],
    "performer_list": [{"user_id": 1003, "nickname": "火苗丙", "description": "3天"}],
    "legend_list": [],
    "strong_newbie_list": [{"user_id": 1004, "nickname": "竹笋丁"}],
    "emotion_list": [],
}


# ---------------------------------------------------------------------------
# 1. 真接线：通道在 → 拿得到数据
# ---------------------------------------------------------------------------

async def test_honor_tool_returns_real_data_when_channel_available():
    ch = _FakeChannel(HONOR_PAYLOAD)
    tool = make_group_honor(_FakeCore(ch))

    out = await tool.handler(group_id=727631526)

    assert "荣誉榜" in out
    assert "话痨甲" in out and "4 天" in out          # 当前龙王 + 霸榜天数
    assert "群聊之火" in out and "火苗丙" in out        # performer_list
    assert "冒尖小春笋" in out and "竹笋丁" in out       # strong_newbie_list
    # 空榜单不渲染，避免刷屏
    assert "群聊炽焰" not in out and "快乐源泉" not in out
    # 真的走了通道，且默认 all
    assert ch.calls == [(727631526, "all")]


async def test_honor_tool_asks_specific_member():
    tool = make_group_honor(_FakeCore(_FakeChannel(HONOR_PAYLOAD)))

    inside = await tool.handler(group_id=727631526, user_id=1003)
    outside = await tool.handler(group_id=727631526, user_id=9999)

    assert "火苗丙" not in inside and "群聊之火" in inside   # 只回答「在不在榜」
    assert "不在" in outside


async def test_honor_tool_defaults_to_current_group_from_context():
    from anima.tools import _CALL_CONTEXT

    tool = make_group_honor(_FakeCore(_FakeChannel(HONOR_PAYLOAD)))
    token = _CALL_CONTEXT.set({"group_id": 727631526})
    try:
        out = await tool.handler()          # 不传 group_id
    finally:
        _CALL_CONTEXT.reset(token)
    assert "727631526" in out


async def test_honor_type_alias_and_rejection():
    ch = _FakeChannel(HONOR_PAYLOAD)
    tool = make_group_honor(_FakeCore(ch))

    await tool.handler(group_id=1, honor_type="龙王")
    assert ch.calls[-1] == (1, "talkative")

    bad = await tool.handler(group_id=1, honor_type="不许存在的荣誉")
    assert "不认识的荣誉类型" in bad
    assert ch.calls[-1][1] == "talkative"   # 没被非法参数带下去


# ---------------------------------------------------------------------------
# 2. 拿不到数据 → 如实说明（不是「群没开这个功能」）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [None, {}, "不是字典"])
async def test_honor_tool_is_honest_when_data_unavailable(payload):
    out = await make_group_honor(_FakeCore(_FakeChannel(payload))).handler(group_id=7)

    assert "没能从 OneBot 取到" in out
    # 旧实现在这里说谎：把链路故障说成「群没开荣誉榜」
    assert "没开荣誉榜" not in out


async def test_honor_tool_channel_exception_does_not_crash_bot():
    """通道抛异常不应该掀翻 bot（工具出站契约要求返回字符串）。"""
    tool = make_group_honor(_FakeCore(_FakeChannel(RuntimeError("ws down"))))
    out = await tool.handler(group_id=7)
    assert "群荣誉榜查询出错" in out or "没能从 OneBot 取到" in out


async def test_honor_tool_without_channel_says_so():
    assert "通道未连接" in await make_group_honor(_FakeCore(None)).handler(group_id=7)
    # core 本身缺失（理论上不会发生，但别炸）
    assert "通道未连接" in await make_group_honor(None).handler(group_id=7)


async def test_honor_tool_without_group_id():
    out = await make_group_honor(_FakeCore(_FakeChannel(HONOR_PAYLOAD))).handler()
    assert "需要指定群号" in out


# ---------------------------------------------------------------------------
# 3. 回归锁：幽灵符号与注册条件
# ---------------------------------------------------------------------------

def _executable_code(src: str) -> str:
    """去掉注释与所有字符串常量，只留可执行符号。

    docstring / 注释里可以（也应该）记录这段历史；测试要抓的是**真正的符号引用**。
    """
    import ast

    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = ""
    return ast.unparse(tree)


def test_no_ghost_get_gateway_symbol_in_honor_module():
    """`get_gateway` 从未定义过；honor.py 不得再引用它（P2-4 根因）。"""
    import anima.tools.builtin.honor as honor_mod

    assert "get_gateway" not in _executable_code(inspect.getsource(honor_mod))


def test_get_gateway_symbol_referenced_nowhere_under_anima():
    """全包不得再引用这个不存在的符号（intent_generator 的 P1-2 同型问题）。"""
    import pathlib

    root = pathlib.Path(inspect.getfile(create_default_registry)).parents[1]  # anima/
    offenders = []
    for p in root.rglob("*.py"):
        text = p.read_text(encoding="utf-8", errors="replace")
        if "get_gateway" not in text:
            continue
        try:
            code = _executable_code(text)
        except SyntaxError:      # 语法坏掉的模块另有测试负责
            continue
        if "get_gateway" in code:
            offenders.append(str(p.relative_to(root)))
    assert not offenders, f"仍有模块引用未定义的 get_gateway: {offenders}"


def test_get_group_honor_registered_only_with_core():
    """与 social 系列一致：需要 core 的工具不该在无 core 时假装存在。"""
    assert "get_group_honor" not in create_default_registry().tools
    assert "get_group_honor" in create_default_registry(_FakeCore(_FakeChannel(HONOR_PAYLOAD))).tools


def test_get_group_honor_is_user_level_readonly():
    tool = make_group_honor(_FakeCore(_FakeChannel(HONOR_PAYLOAD)))
    assert tool.min_level == LEVEL_USER
    schema = tool.to_openai_schema()
    props = schema["function"]["parameters"]["properties"]
    assert {"group_id", "honor_type", "user_id"} <= set(props)
    assert schema["function"]["parameters"].get("required", []) == []
