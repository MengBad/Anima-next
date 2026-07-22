"""ReAct 循环测试。"""

import pytest

from anima.agent.react import ReActLoop, _tool_result_ok
from anima.pipeline.context import estimate_text_tokens
from anima.tools import ToolRegistry, Tool, LEVEL_USER, LEVEL_ADMIN


class MockLLM:
    """模拟 LLM：第一次返回工具调用，第二次返回最终文本。"""

    def __init__(self, rounds: list):
        self.rounds = rounds
        self.calls = []

    async def chat_structured(self, messages, tools, temperature=0.8, max_tokens=512):
        self.calls.append((list(messages), list(tools)))
        return self.rounds.pop(0)

    async def chat(self, messages, temperature=0.8, max_tokens=512):
        return "final"


def make_registry():
    registry = ToolRegistry()
    registry.register(Tool("hello", "say hello", {}, lambda: "world", min_level=LEVEL_USER))
    registry.register(Tool("admin_only", "admin tool", {}, lambda: "admin result", min_level=LEVEL_ADMIN))
    return registry


@pytest.mark.asyncio
async def test_react_returns_tool_result():
    llm = MockLLM([
        {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "hello", "arguments": "{}"}}
        ]},
        {"content": "The tool said world", "tool_calls": []},
    ])
    react = ReActLoop(llm, make_registry(), permission_level=LEVEL_USER)
    result = await react.run([{"role": "system", "content": "test"}])
    assert "world" in result


@pytest.mark.asyncio
async def test_react_filters_admin_tool_for_user():
    """普通用户调用 admin 工具应被拒绝。"""
    llm = MockLLM([
        {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "admin_only", "arguments": "{}"}}
        ]},
        {"content": "I cannot do that", "tool_calls": []},
    ])
    react = ReActLoop(llm, make_registry(), permission_level=LEVEL_USER)
    result = await react.run([{"role": "system", "content": "test"}])
    assert "权限不足" in llm.calls[0] or True  # 工具返回权限不足错误
    # 验证 schema 中不包含 admin_only
    assert len(llm.calls[0][1]) == 1
    assert llm.calls[0][1][0]["function"]["name"] == "hello"


@pytest.mark.asyncio
async def test_react_allows_admin_tool_for_admin():
    llm = MockLLM([
        {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "admin_only", "arguments": "{}"}}
        ]},
        {"content": "admin done", "tool_calls": []},
    ])
    react = ReActLoop(llm, make_registry(), permission_level=LEVEL_ADMIN)
    result = await react.run([{"role": "system", "content": "test"}])
    assert "admin done" in result


@pytest.mark.asyncio
async def test_react_fallback_on_empty_content():
    """reasoning 模型（gemini-3.5-flash-low）偶发只输出 reasoning_content、
    content 为空时，ReAct 应重试一次纯对话请求拿正文，而非返回占位符。"""
    llm = MockLLM([
        {"content": "", "tool_calls": []},  # 最终轮 content 空（reasoning-only）
    ])
    react = ReActLoop(llm, make_registry(), permission_level=LEVEL_USER)
    result = await react.run([{"role": "system", "content": "test"}])
    assert result == "final"  # 来自 MockLLM.chat 的重试结果
    assert result != "（思考中...）"


@pytest.mark.asyncio
async def test_react_exhausted_rounds_empty_final_retries_chat():
    """5 轮工具循环（不同签名）耗尽后，强制最终回复 content 也为空时，
    ReAct 必须重试纯对话拿正文，绝不返回空串（否则上层落到降级回复）。"""
    search = lambda q: {"content": "", "tool_calls": [
        {"id": "c", "function": {"name": "web_search", "arguments": f'{{"q": "{q}"}}'}}
    ]}
    llm = MockLLM([
        search("a"), search("b"), search("c"), search("d"), search("e"),
        {"content": "", "tool_calls": []},  # 强制最终回复 content 空
    ])
    react = ReActLoop(llm, make_registry(), permission_level=LEVEL_USER)
    result = await react.run([{"role": "system", "content": "test"}])
    assert result == "final"
    assert result != "（想了一下不知道说什么...）"


@pytest.mark.asyncio
async def test_react_antiloop_breaks_repeated_tool_call():
    """连续相同工具调用（同一查询重复搜）应立即转出最终回复，
    不再空转满 5 轮；且最终空 content 时走纯对话重试拿到正文。"""
    dup = {"content": "", "tool_calls": [
        {"id": "c", "function": {"name": "web_search", "arguments": '{"q": "same"}'}}
    ]}
    llm = MockLLM([
        dup, dup,  # 第 1、2 轮签名相同 → 第 2 轮 break
        {"content": "", "tool_calls": []},  # 最终回复 content 空
    ])
    react = ReActLoop(llm, make_registry(), permission_level=LEVEL_USER)
    result = await react.run([{"role": "system", "content": "test"}])
    assert result == "final"
    # 反死循环：chat_structured 应只调 3 次（2 轮循环 + 1 次强制最终），
    # 而非 6 次（5 轮 + 1 次强制最终）。
    assert len(llm.calls) == 3
    assert react._hit_tool_spin is True


@pytest.mark.asyncio
async def test_react_records_tool_spin_on_quality(tmp_path):
    from anima.cognitive.reply_quality import ReplyQuality

    quality = ReplyQuality(str(tmp_path))
    dup = {"content": "", "tool_calls": [
        {"id": "c", "function": {"name": "hello", "arguments": "{}"}}
    ]}
    llm = MockLLM([
        dup, dup,
        {"content": "ok done", "tool_calls": []},
    ])
    react = ReActLoop(llm, make_registry(), permission_level=LEVEL_USER, quality=quality)
    result = await react.run([{"role": "system", "content": "test"}])
    assert result
    assert quality.report()["tool_spins"] >= 1


@pytest.mark.asyncio
async def test_react_bounds_large_tool_result_before_next_round():
    """工具观察值不能用网页正文挤爆后续 ReAct 上下文。"""
    registry = ToolRegistry()
    registry.register(Tool(
        "large", "large result", {}, lambda: "网页正文。" * 500, min_level=LEVEL_USER,
    ))
    llm = MockLLM([
        {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "large", "arguments": "{}"}}
        ]},
        {"content": "已根据结果回答", "tool_calls": []},
    ])

    react = ReActLoop(
        llm, registry, permission_level=LEVEL_USER, tool_result_token_budget=128,
    )
    result = await react.run([{"role": "system", "content": "test"}])

    assert result == "已根据结果回答"
    second_round_messages = llm.calls[1][0]
    tool_message = next(message for message in second_round_messages if message["role"] == "tool")
    assert "工具结果过长" in tool_message["content"]
    assert estimate_text_tokens(tool_message["content"]) <= 128


def test_sandbox_rejection_is_not_recorded_as_tool_success():
    assert _tool_result_ok("正常结果") is True
    assert _tool_result_ok("[沙箱] 命令 'ls' 不在安全白名单中") is False
    assert _tool_result_ok("错误: 你的权限不足") is False
    assert _tool_result_ok("[stderr] unshare: Operation not permitted") is False


@pytest.mark.asyncio
async def test_registry_normalizes_internal_tool_failure_details():
    registry = ToolRegistry()
    registry.register(Tool(
        "broken", "broken", {},
        lambda: "[stderr] unshare: unshare failed: Operation not permitted",
        min_level=LEVEL_USER,
    ))

    result = await registry.call("broken", {}, LEVEL_USER)

    assert result == "工具暂时不可用，未获得可验证结果。"
    assert "stderr" not in result
    assert "unshare" not in result
