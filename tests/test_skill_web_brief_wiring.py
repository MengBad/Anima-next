# -*- coding: utf-8 -*-
"""`skill_web_brief` 端到端接线自检（2026-10-10）。

## 为什么需要它
`skill_web_brief` 在生产**近 40 天 0 次调用**（516 条 `tool_executed` 里一条都没有）。
在把流量导给它之前，必须先证明「它一旦被调用是能跑通的」——
否则就是把真实用户请求推进一个没验证过的出口。

## 它守的是什么
`SkillRegistry._run_composed_tool`（`skills/registry.py:214`）的三段接线：

    LLM 参数 -> tools.call("web_search", args, level) -> _summarize(template + 原文)

1. 参数原样透传（`query` / `num_results`，不多不少）；
2. **模板真的进了整理 prompt**（GEPA 演进的就是这段文本——它进不去，自进化无从谈起）；
3. 原文真的进了整理 prompt（否则整理器只能瞎编）；
4. 整理失败 ⇒ 回落到原文（不静默变空、不抛给用户一个空壳）。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from anima.skills.registry import SkillRegistry
from anima.tools import LEVEL_USER, Tool, ToolRegistry

BUILTIN_ROOT = Path(__file__).resolve().parents[1] / "anima" / "skills" / "builtin"


class _FakeLLM:
    """记录被要求整理的内容；``empty=True`` 模拟整理失败。"""

    def __init__(self, empty: bool = False):
        self.calls: list[list[dict]] = []
        self.empty = empty

    async def chat(self, messages, **kwargs):
        self.calls.append(list(messages))
        return "" if self.empty else "1. 要点甲（来源：example.com，2026-10-09）"


def _build(llm_empty: bool = False):
    calls: list[tuple] = []

    async def _web_search(**kwargs):
        calls.append(("web_search", dict(kwargs)))
        return "原始检索结果：甲公司在 2026-10-09 发布了 v2.0（来源 example.com）"

    core = SimpleNamespace()
    reg = ToolRegistry(core)
    reg.register(Tool(name="web_search", description="联网搜索",
                     parameters={"query": {"type": "string", "description": "关键词"},
                                 "num_results": {"type": "integer", "description": "条数"}},
                     handler=_web_search, min_level=LEVEL_USER))
    core.tools = reg
    core.llm = _FakeLLM(empty=llm_empty)
    core.config = SimpleNamespace(skills=SimpleNamespace(allow_prompt=True))

    sr = SkillRegistry(Path("/nonexistent"), core, builtin_root=BUILTIN_ROOT)
    sr.load()
    sr.install_into_tools()
    return core, reg, sr, calls


def test_web_brief_is_installed_as_a_tool():
    _, reg, _, _ = _build()
    assert "skill_web_brief" in reg.tools, "web_brief 没有被装进工具面"


def test_web_brief_tool_carries_the_template_for_audit():
    _, reg, _, _ = _build()
    t = reg.tools["skill_web_brief"]
    # 审计三字段之一：事后要能从 tool_executed 恢复「这次用的是哪个模板」
    assert t.template, "template 为空 ⇒ GEPA 无法从审计恢复模板，自进化断了数据源"
    assert "来源" in t.template


async def test_composed_tool_receives_exactly_the_declared_args():
    """参数必须原样透传：多了会撞 web_search 的未知参数，少了查不到东西。"""
    core, reg, _, calls = _build()
    tool = reg.tools["skill_web_brief"]

    out = await tool.handler(query="甲公司 最新版本", num_results=5)

    assert calls and calls[0][0] == "web_search"
    assert calls[0][1] == {"query": "甲公司 最新版本", "num_results": 5}
    assert "要点甲" in out


async def test_summary_prompt_carries_both_template_and_raw_material():
    """GEPA 演进的正是这段 template —— 它没进 prompt 就等于演进了个寂寞。"""
    core, _, _, _ = _build()
    tool = core.tools.tools["skill_web_brief"]

    await tool.handler(query="甲公司 最新版本")

    assert core.llm.calls, "整理器根本没被调用 ⇒ content_summary 没生效"
    prompt = "\n".join(m.get("content", "") for m in core.llm.calls[0])
    assert "来源" in prompt, "执行模板没进整理 prompt"
    assert "原始检索结果" in prompt, "检索原文没进整理 prompt ⇒ 整理器只能编造"


async def test_summarize_failure_falls_back_to_raw_result():
    """整理失败必须回落到原文，绝不能给用户一个空壳。"""
    core, _, _, _ = _build(llm_empty=True)
    tool = core.tools.tools["skill_web_brief"]

    out = await tool.handler(query="甲公司 最新版本")

    assert "原始检索结果" in out, "整理为空时没有回落到原文"
    assert out.strip(), "回落结果为空 ⇒ 用户会收到空回复"


def test_web_brief_does_not_require_prompt_execution():
    """type=tool 不应走 prompt 分支（那需要 allow_prompt 且会另起一次 LLM）。"""
    _, reg, sr, _ = _build()
    manifest = sr.manifests["web_brief"]
    assert manifest.execution.type == "tool"
    assert manifest.execution.tool == "web_search"
    assert manifest.execution.content_summary is True
