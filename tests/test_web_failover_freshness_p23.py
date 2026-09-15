"""P2-3 测试：搜网多源 failover（DDG 死代码接线）+ 时效话术进常规收尾路径。

覆盖：
- ``_search`` 降级链：Tavily → SearXNG → Bing → DuckDuckGo，命中即停
- ``_search_ddg`` 真的会被调用（此前是死代码）
- 全部通道失败 → 抛错并由 web_search 转化为「环境问题，不要编答案」的诚实话术
- ReAct：时效工具结果后注入时效约束（只注入一次）
- ReAct：回答无日期痕迹时追加时效脚注；有日期/未用时效工具/开关关闭时不加
"""

import asyncio

import pytest

from anima.agent import react as react_mod
from anima.agent.react import ReActLoop, _FRESHNESS_FOOTER, _FRESHNESS_GUIDANCE
from anima.tools import LEVEL_USER


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------

class _CtxComposer:
    def compact_tool_result(self, text, **kw):
        return text


def _make_loop(llm, *, freshness_footer=True, results=None):
    class _Tools:
        def get_schemas(self, level, **kw):
            return []

        async def call(self, name, args, level):
            return (results or {}).get(name, "ok")

    loop = ReActLoop(llm, _Tools(), max_rounds=3, permission_level=LEVEL_USER,
                     freshness_footer=freshness_footer)
    loop._context_composer = _CtxComposer()
    return loop


class _ScriptedLLM:
    """按调用次数依次返回结构化响应。"""

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    async def chat_structured(self, messages, tools, temperature=0.8, max_tokens=512):
        self.calls.append(("structured", [m.get("content") for m in messages]))
        return self.steps.pop(0) if self.steps else {"content": "兜底", "tool_calls": []}

    async def chat(self, messages, temperature=0.8, max_tokens=512, **kw):
        self.calls.append(("chat", [m.get("content") for m in messages]))
        return "兜底"


def _tool_call(name, args="{}", call_id="c1"):
    return {"id": call_id, "function": {"name": name, "arguments": args}}


# ---------------------------------------------------------------------------
# 1. 多源 failover
# ---------------------------------------------------------------------------

class _Chan:
    """预留：通道级替身。当前实现通过 monkeypatch urllib 观察通道顺序。"""

    def __init__(self, behavior):
        self.behavior = behavior


def test_ddg_is_in_failover_chain():
    """P2-3 核心：_search_ddg 此前已实现却从未被调用（死代码），必须接进链路。"""
    import inspect

    import anima.tools.builtin.web as web

    src = inspect.getsource(web.tool_web_search)
    assert "_search_ddg" in src, "DDG 未被 search 链引用"
    # 顺序断言：tavily 在前，ddg 在最后
    chain = src[src.index("for name, fn in (("):]
    order = [chain.index(x) for x in ("searxng", "bing", "duckduckgo")]
    assert order == sorted(order), "降级顺序应为 searxng → bing → duckduckgo"


async def test_all_channels_fail_returns_honest_environment_message(monkeypatch):
    """全部通道在 HTTP 层抛错 → 明确说是环境问题，且不要让模型以为「网上没有」。"""
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no net")))
    monkeypatch.delenv("ANIMA_TAVILY_API_KEY", raising=False)

    import anima.tools.builtin.web as web

    out = await web.tool_web_search("今天的新闻", num_results=2)
    assert "全部通道都不通" in out
    assert "不要凭记忆补答案" in out
    assert "searxng:fail" in out and "duckduckgo:fail" in out


async def test_channels_respond_but_empty_is_not_reported_as_outage(monkeypatch):
    """通道 HTTP 成功但零结果 → 说「没搜到」，不能说成网络故障（反之亦然）。

    这两者的用户观感完全不同：前者换个词就能救，后者是我们链路的问题。
    """
    import urllib.request

    class _Resp:
        headers = None

        def read(self):
            return b"<html><body>nothing</body></html>"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())
    monkeypatch.delenv("ANIMA_TAVILY_API_KEY", raising=False)

    import anima.tools.builtin.web as web

    out = await web.tool_web_search("某个很窄的关键词", num_results=2)
    assert "没有搜到" in out
    assert "全部通道都不通" not in out


# ---------------------------------------------------------------------------
# 2. ReAct 时效约束注入
# ---------------------------------------------------------------------------

async def test_freshness_guidance_injected_after_web_tool():
    llm = _ScriptedLLM([
        {"content": "", "tool_calls": [_tool_call("web_search", '{"query":"x"}')]},
        {"content": "结论是甲。", "tool_calls": []},
    ])
    loop = _make_loop(llm)
    out = await loop.run([{"role": "system", "content": "s"},
                          {"role": "user", "content": "问"}])

    # 第二次结构化调用的消息里应带上时效约束
    second = llm.calls[1][1]
    assert any(_FRESHNESS_GUIDANCE == (m or "") for m in second)
    # 回答无日期 → 追加时效脚注
    assert out.startswith("结论是甲。")
    assert "时效" in out


async def test_freshness_guidance_injected_only_once():
    llm = _ScriptedLLM([
        {"content": "", "tool_calls": [_tool_call("web_search", "{}", "c1")]},
        {"content": "", "tool_calls": [_tool_call("web_search", "{}", "c2")]},
        {"content": "答案", "tool_calls": []},
    ])
    loop = _make_loop(llm)
    await loop.run([{"role": "user", "content": "q"}])

    third = llm.calls[2][1]
    assert sum(1 for m in third if (m or "") == _FRESHNESS_GUIDANCE) == 1


async def test_no_guidance_when_only_non_freshness_tools():
    llm = _ScriptedLLM([
        {"content": "", "tool_calls": [_tool_call("calculate", "{}")]},
        {"content": "等于 42", "tool_calls": []},
    ])
    loop = _make_loop(llm)
    out = await loop.run([{"role": "user", "content": "1+1"}])

    second = llm.calls[1][1]
    assert not any((m or "") == _FRESHNESS_GUIDANCE for m in second)
    # 未用时效工具 → 不加脚注
    assert out == "等于 42"


# ---------------------------------------------------------------------------
# 3. 时效脚注
# ---------------------------------------------------------------------------

async def test_footer_skipped_when_answer_has_date():
    llm = _ScriptedLLM([
        {"content": "", "tool_calls": [_tool_call("web_search", "{}")]},
        {"content": "据 2026-09-14 的报道，情况是甲。", "tool_calls": []},
    ])
    loop = _make_loop(llm)
    out = await loop.run([{"role": "user", "content": "q"}])
    assert out == "据 2026-09-14 的报道，情况是甲。"


async def test_footer_can_be_disabled_by_switch():
    llm = _ScriptedLLM([
        {"content": "", "tool_calls": [_tool_call("web_search", "{}")]},
        {"content": "结论", "tool_calls": []},
    ])
    loop = _make_loop(llm, freshness_footer=False)
    out = await loop.run([{"role": "user", "content": "q"}])
    assert out == "结论"


async def test_footer_added_on_max_round_finalize_path():
    """触顶收尾路径同样要有时效披露（此前只有该路径有约束、现在两条都有脚注）。"""
    llm = _ScriptedLLM([
        {"content": "", "tool_calls": [_tool_call("web_search", '{"q":"1"}', "c1")]},
        {"content": "", "tool_calls": [_tool_call("web_search", '{"q":"2"}', "c2")]},
        {"content": "", "tool_calls": [_tool_call("web_search", '{"q":"3"}', "c3")]},
        {"content": "整理后的结论", "tool_calls": []},
    ])
    loop = _make_loop(llm)
    out = await loop.run([{"role": "user", "content": "q"}])
    assert loop._hit_max_rounds is True
    assert "整理后的结论" in out
    assert "时效" in out


def test_footer_text_mentions_fetch_time():
    assert "{date}" in _FRESHNESS_FOOTER


# ---------------------------------------------------------------------------
# 4. 配置接线
# ---------------------------------------------------------------------------

def test_react_config_defaults():
    from anima.core.config import AnimaConfig

    cfg = AnimaConfig()
    assert cfg.react.freshness_footer is True
    assert cfg.react.memory_first is True


def test_generation_helper_reads_config():
    from anima.pipeline.stages.generation import _freshness_footer_enabled

    on = type("C", (), {"config": type("Cfg", (), {
        "react": type("R", (), {"freshness_footer": True})()})()})()
    off = type("C", (), {"config": type("Cfg", (), {
        "react": type("R", (), {"freshness_footer": False})()})()})()
    missing = type("C", (), {"config": type("Cfg", (), {})()})()

    assert _freshness_footer_enabled(on) is True
    assert _freshness_footer_enabled(off) is False
    assert _freshness_footer_enabled(missing) is True   # fail-open，最坏多一句披露
