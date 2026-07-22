"""LLM 事实抽取：正常/无事实/短文本跳过/异常静默/content 兜底。"""

import json

import pytest

from anima.crossgroup.extraction import extract_user_facts


class _FakeLLM:
    """模拟网关返回 tool_calls（或仅 content）。"""

    def __init__(self, tool_args=None, content=""):
        self.tool_args = tool_args
        self.content = content

    async def chat_structured(self, messages, tools, temperature=0.8, max_tokens=1024):
        if self.tool_args is not None:
            return {
                "content": self.content,
                "tool_calls": [{
                    "id": "c1",
                    "type": "function",
                    "function": {
                        "name": "record_user_facts",
                        "arguments": json.dumps(self.tool_args, ensure_ascii=False),
                    },
                }],
            }
        return {"content": self.content, "tool_calls": []}


@pytest.mark.asyncio
async def test_extract_normal():
    llm = _FakeLLM(tool_args={
        "facts": [{"content": "正在学 Rust", "tags": ["编程"], "confidence": 0.9}]
    })
    facts = await extract_user_facts(llm, "我最近在学Rust", "Alice")
    assert len(facts) == 1
    assert facts[0]["content"] == "正在学 Rust"


@pytest.mark.asyncio
async def test_extract_empty_facts():
    llm = _FakeLLM(tool_args={"facts": []})
    assert await extract_user_facts(llm, "今天天气不错", "Bob") == []


@pytest.mark.asyncio
async def test_short_text_skipped():
    llm = _FakeLLM(tool_args={"facts": [{"content": "x"}]})
    assert await extract_user_facts(llm, "666", "Bob") == []


@pytest.mark.asyncio
async def test_exception_silent():
    """网关异常必须静默降级为 []，绝不中断调用方。"""
    class Boom:
        async def chat_structured(self, *a, **k):
            raise RuntimeError("gateway down")

    assert await extract_user_facts(Boom(), "我在学Go语言", "Cara") == []


@pytest.mark.asyncio
async def test_content_json_fallback():
    """部分网关不回 tool_calls，仅回 content JSON —— 应能兜底解析。"""
    llm = _FakeLLM(content='```json\n{"facts":[{"content":"考研中"}]}\n```')
    facts = await extract_user_facts(llm, "我最近在准备考研", "Dan")
    assert len(facts) == 1
    assert facts[0]["content"] == "考研中"
