"""LLM 事实抽取：正常/无事实/短文本跳过/异常静默/content 兜底。

注意：extract_user_facts 现返回字典 {"facts", "nicknames", "relations"}，
任何失败均返回全空字典（非 []）。
"""

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
    result = await extract_user_facts(llm, "我最近在学Rust", "Alice")
    assert len(result["facts"]) == 1
    assert result["facts"][0]["content"] == "正在学 Rust"
    assert result["nicknames"] == []
    assert result["relations"] == []


@pytest.mark.asyncio
async def test_extract_with_nickname_and_relation():
    """抽取应同时带回外号与关系边。"""
    llm = _FakeLLM(tool_args={
        "facts": [{"content": "考研中"}],
        "nicknames": ["企鹅仔"],
        "relations": [{"target": "李四", "relation": "室友", "note": "同寝"}],
    })
    result = await extract_user_facts(llm, "企鹅仔最近在考研，和李四同寝", "张三")
    assert result["nicknames"] == ["企鹅仔"]
    assert result["relations"][0]["target"] == "李四"
    assert result["relations"][0]["relation"] == "室友"


@pytest.mark.asyncio
async def test_extract_empty_facts():
    llm = _FakeLLM(tool_args={"facts": []})
    result = await extract_user_facts(llm, "今天天气不错", "Bob")
    assert result == {"facts": [], "nicknames": [], "relations": []}


@pytest.mark.asyncio
async def test_short_text_skipped():
    llm = _FakeLLM(tool_args={"facts": [{"content": "x"}]})
    result = await extract_user_facts(llm, "666", "Bob")
    assert result["facts"] == []


@pytest.mark.asyncio
async def test_exception_silent():
    """网关异常必须静默降级为空字典，绝不中断调用方。"""
    class Boom:
        async def chat_structured(self, *a, **k):
            raise RuntimeError("gateway down")

    result = await extract_user_facts(Boom(), "我在学Go语言", "Cara")
    assert result == {"facts": [], "nicknames": [], "relations": []}


@pytest.mark.asyncio
async def test_content_json_fallback():
    """部分网关不回 tool_calls，仅回 content JSON —— 应能兜底解析。"""
    llm = _FakeLLM(content='```json\n{"facts":[{"content":"考研中"}]}\n```')
    result = await extract_user_facts(llm, "我最近在准备考研", "Dan")
    assert len(result["facts"]) == 1
    assert result["facts"][0]["content"] == "考研中"


def test_looks_informative_pure_symbols():
    from anima.crossgroup.extraction import _looks_informative
    assert _looks_informative('😂😂😂') is False
    assert _looks_informative('？？？') is False
    assert _looks_informative('   ') is False
    assert _looks_informative('哈哈哈') is True
    assert _looks_informative('I am learning Rust') is True


async def test_extract_skips_pure_symbol_without_llm():
    # P5: 纯表情/符号不调 LLM，直接返回全空字典
    result = await extract_user_facts(_FakeLLM(tool_args='{}'), '😂😂😂')
    assert result == {'facts': [], 'nicknames': [], 'relations': []}
