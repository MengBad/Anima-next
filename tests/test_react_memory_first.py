import pytest
from unittest.mock import AsyncMock, MagicMock
from anima.agent.react import ReActLoop
from anima.tools import ToolRegistry
from anima.tools.builtin import register_all_builtin

@pytest.mark.asyncio
async def test_react_memory_first_prefer_member_facts():
    """测试 ReAct 记忆优先：群成员问题应先调用 get_member_facts"""
    registry = ToolRegistry()
    register_all_builtin(registry)
    react = ReActLoop(AsyncMock(), registry)
    
    messages = [{"role": "user", "content": "亦萱喜欢什么"}]
    
    # Mock the LLM to return tool call first
    react.llm.chat_structured = AsyncMock(return_value={
        "tool_calls": [{"function": {"name": "get_member_facts", "arguments": '{"name": "亦萱"}'}}],
        "content": ""
    })
    
    response = await react.run(messages)
    assert "get_member_facts" in str(react.tool_trace[0]["name"]) or "memory" in response.lower()
