"""P0-1 Memory-First 回归测试（DESIGN_REACT_MEMORY_FIRST.md）。

覆盖四层：
1. generation 分流引导 —— 成员/事实类注入记忆优先；实时类仍注入实时引导（互斥，不打架）
2. Tool.priority —— 记忆类工具在 schema 中排在 web_search 之前；未标注工具行为不变
3. react.py —— 不再伪造 Thought 注入循环；触顶收尾不再讲工具优先级
4. 搜网结果 —— 仍写入独立 WebKnowledgeStore（接线不断）

原文件只有一个断言恒真的占位测试，已被本文件替换。
"""
from __future__ import annotations

import pytest

from anima.pipeline import PipelineContext, StageResult
from anima.pipeline.stages import generation_stage
from anima.pipeline.stages.generation import (
    _MEMORY_FIRST_RE,
    _MEMORY_FIRST_GUIDANCE,
    _REALTIME_TOOL_GUIDANCE,
    _IDENTITY_LOCK_HEADING,
)
from anima.tools import Tool, ToolRegistry, LEVEL_USER
from anima.agent.react import ReActLoop

_NL = chr(10)
_IDENTITY_BLOCK = f"{_IDENTITY_LOCK_HEADING}{_NL}你是哈基米，一只猫娘。"


class _FakeCore:
    """最小 core 桩：够 generation_stage 走到 ReAct 引导注入即可。"""

    def __init__(self, memory_first: bool = True):
        self.llm = type("_LLM", (), {"model": "fake-model"})()
        self.config = type("_Config", (), {
            "react": type("_React", (), {"memory_first": memory_first})(),
            "sticker": type("_Sticker", (), {"share_across_groups": True})(),
        })()
        self.vector_memory = None
        self.tools = type("_Tools", (), {
            "tools": {"web_search": object(), "get_member_facts": object()},
            "set_call_context": lambda self, **kw: object(),
            "get_call_context": lambda self: {"generated_image_paths": []},
            "reset_call_context": lambda self, token: None,
        })()


def _make_ctx(text: str, core) -> PipelineContext:
    ctx = PipelineContext(
        event={},
        text=text,
        user_id=200,
        group_id=727631526,
        detail_type="group",
        sender_name="小明",
        meta={"core": core},
    )
    ctx.messages = [{"role": "system", "content": f"系统设定{_NL}{_NL}{_IDENTITY_BLOCK}"}]
    return ctx


def _patch_react(monkeypatch):
    class _FakeReAct:
        def __init__(self, *a, **k):
            pass

        async def run(self, messages, **k):
            return "好的喵"

    import anima.agent.react as react_mod
    monkeypatch.setattr(react_mod, "ReActLoop", _FakeReAct)


# ---------------------------------------------------------------------------
# 1. 分流引导
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "亦萱是谁",
    "亦萱喜欢什么",
    "讲讲小明的背景",
    "你还记得我上次说过什么吗",
])
async def test_memory_guidance_injected_for_member_question(monkeypatch, text):
    _patch_react(monkeypatch)
    ctx = _make_ctx(text, _FakeCore())

    result = await generation_stage(ctx)
    assert result == StageResult.CONTINUE

    sys_text = next(m["content"] for m in ctx.messages if m.get("role") == "system")
    assert _MEMORY_FIRST_GUIDANCE in sys_text, f"成员/事实类问题应注入记忆优先引导: {text}"


@pytest.mark.asyncio
async def test_realtime_guidance_still_wins_for_news(monkeypatch):
    """实时类问题只能收到「先搜网」，不能同时收到「先查记忆」——两条指令会打架。"""
    text = "今天深圳天气怎么样"
    assert _MEMORY_FIRST_RE.search(text), "前置条件：该句同时命中记忆词表"

    _patch_react(monkeypatch)
    ctx = _make_ctx(text, _FakeCore())

    await generation_stage(ctx)

    sys_text = next(m["content"] for m in ctx.messages if m.get("role") == "system")
    assert _REALTIME_TOOL_GUIDANCE in sys_text, "实时类问题必须仍注入实时引导"
    assert _MEMORY_FIRST_GUIDANCE not in sys_text, "实时类问题不应再注入记忆优先引导"


@pytest.mark.asyncio
async def test_memory_first_toggle_off_disables_guidance(monkeypatch):
    """react.memory_first=false → 不注入记忆引导（一键回滚点）。"""
    _patch_react(monkeypatch)
    ctx = _make_ctx("亦萱是谁", _FakeCore(memory_first=False))

    await generation_stage(ctx)

    sys_text = next(m["content"] for m in ctx.messages if m.get("role") == "system")
    assert _MEMORY_FIRST_GUIDANCE not in sys_text


@pytest.mark.asyncio
async def test_toggle_off_disables_priority_sort_in_react(monkeypatch):
    """同一个开关同时管住引导注入与 schema 排序——回滚只改一处。"""
    captured: dict = {}

    class _FakeReAct:
        def __init__(self, *a, **k):
            captured.update(k)

        async def run(self, messages, **k):
            return "好的喵"

    import anima.agent.react as react_mod
    monkeypatch.setattr(react_mod, "ReActLoop", _FakeReAct)

    await generation_stage(_make_ctx("亦萱是谁", _FakeCore(memory_first=False)))
    assert captured.get("priority_sort") is False


# ---------------------------------------------------------------------------
# 2. Tool.priority / schema 排序
# ---------------------------------------------------------------------------
def _registry_with(core) -> ToolRegistry:
    reg = ToolRegistry(core=core)
    # 注册顺序刻意「web 在前」，验证 priority 能把记忆类顶到前面
    reg.register(Tool("web_search", "联网搜索", {"query": {"type": "string"}},
                      lambda **k: "", required=["query"], priority=3))
    reg.register(Tool("get_member_facts", "成员事实", {"name": {"type": "string"}},
                      lambda **k: "", required=["name"], priority=1))
    reg.register(Tool("get_user_relations", "成员关系", {"name": {"type": "string"}},
                      lambda **k: "", required=["name"], priority=1))
    reg.register(Tool("query_web_memory", "网页记忆", {"query": {"type": "string"}},
                      lambda **k: "", required=["query"], priority=1))
    reg.register(Tool("calculate", "计算", {"expression": {"type": "string"}},
                      lambda **k: ""))  # 未标注 → 默认 5
    return reg


def _names(schemas):
    return [s["function"]["name"] for s in schemas]


def test_schema_order_memory_before_web():
    """显式开启排序时，记忆类工具必须排在 web_search 之前。"""
    schemas = _registry_with(_FakeCore()).get_schemas(LEVEL_USER, sort_by_priority=True)
    order = _names(schemas)

    for mem_tool in ("get_member_facts", "get_user_relations", "query_web_memory"):
        assert order.index(mem_tool) < order.index("web_search"), f"{mem_tool} 应排在 web_search 之前"
    # 未标注工具（默认 5）排在显式标注之后
    assert order.index("web_search") < order.index("calculate")


def test_priority_default_backward_compat():
    """未标注 priority 的工具默认 5；同优先级保持注册顺序（sort 稳定）。"""
    reg = ToolRegistry(core=_FakeCore())
    for name in ("a", "b", "c"):
        reg.register(Tool(name, name, {}, lambda **k: ""))

    assert [t.priority for t in reg.tools.values()] == [5, 5, 5]
    assert _names(reg.get_schemas(LEVEL_USER)) == ["a", "b", "c"]
    assert _names(reg.get_schemas(LEVEL_USER, sort_by_priority=True)) == ["a", "b", "c"]


def test_priority_sort_off_by_default_keeps_frozen_contract():
    """默认**不**排序：legacy schema 返回顺序是 M0.3 冻结契约（test_capability_* 守着）。

    排序只对显式开启的主路径（ReAct）生效，既有调用方可见行为不变。
    """
    reg = _registry_with(_FakeCore())
    registration_order = ["web_search", "get_member_facts", "get_user_relations",
                          "query_web_memory", "calculate"]
    assert _names(reg.get_schemas(LEVEL_USER)) == registration_order
    assert _names(reg.get_schemas(LEVEL_USER, sort_by_priority=False)) == registration_order


def test_react_loop_requests_priority_sort():
    """ReAct 主路径必须显式打开排序，否则 P0-1 在 QQ 上根本不生效。"""
    seen: dict = {}

    class _FakeTools:
        def get_schemas(self, permission_level=None, **kw):
            seen.update(kw)
            return []

    class _FakeLLM:
        model = "fake"

        async def chat_structured(self, messages, schemas, temperature=0.8, max_tokens=512):
            return {"content": "本喵知道喵", "tool_calls": []}

        async def chat(self, messages, temperature=0.8, max_tokens=512):
            return "本喵知道喵"

    import asyncio
    asyncio.run(ReActLoop(_FakeLLM(), _FakeTools(), priority_sort=True).run(
        [{"role": "user", "content": "亦萱是谁"}]))
    assert seen.get("sort_by_priority") is True


# ---------------------------------------------------------------------------
# 3. ReAct 不再伪造 Thought
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_react_loop_injects_no_fake_thought():
    class _FakeLLM:
        model = "fake"

        def __init__(self):
            self.seen: list[list[dict]] = []

        async def chat_structured(self, messages, schemas, temperature=0.8, max_tokens=512):
            self.seen.append([dict(m) for m in messages])
            return {"content": "本喵知道喵", "tool_calls": []}

        async def chat(self, messages, temperature=0.8, max_tokens=512):
            return "本喵知道喵"

    class _FakeTools:
        def get_schemas(self, permission_level=None, **kw):
            return []

    llm = _FakeLLM()
    loop = ReActLoop(llm, _FakeTools(), max_rounds=3)
    out = await loop.run([{"role": "user", "content": "亦萱是谁"}])

    assert out == "本喵知道喵"
    assert llm.seen, "应至少调用一次 LLM"
    for batch in llm.seen:
        for msg in batch:
            body = str(msg.get("content") or "")
            assert not body.startswith("Thought: 用户问"), (
                "ReAct 不应再伪造 Thought 消息——记忆优先改由 system 引导承担"
            )


def test_react_source_has_no_tool_priority_farewell():
    """触顶收尾阶段已经不给工具了，此时讲工具优先级是无效指令。"""
    import inspect
    from anima.agent import react as react_mod

    assert "你在选择工具时，请遵循以下优先级" not in inspect.getsource(react_mod)


# ---------------------------------------------------------------------------
# 4. 搜网结果 → 独立 WebKnowledgeStore（接线仍在）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_web_search_result_persisted_to_web_knowledge(monkeypatch, tmp_path):
    import urllib.request
    import anima.memory.web_knowledge as wk
    from anima.tools.builtin.web import tool_web_search

    captured: list[wk.WebKnowledgeStore] = []

    class _ScopedStore(wk.WebKnowledgeStore):
        def __init__(self, data_dir=None, file_name="web_knowledge.json"):
            super().__init__(tmp_path, file_name)  # 强制写临时目录，不污染 data/
            captured.append(self)

    monkeypatch.setattr(wk, "WebKnowledgeStore", _ScopedStore)

    class _FakeHeaders:
        def get_content_charset(self):
            return "utf-8"

    class _FakeResp:
        def __init__(self, body: bytes):
            self._body = body
            self.headers = _FakeHeaders()

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    _BING_HTML = (
        '<html><body>'
        '<li class="b_algo"><h2><a href="https://example.com/a">示例标题A</a></h2>'
        '<p>示例摘要A</p></li>'
        '</body></html>'
    ).encode("utf-8")

    def _fake_urlopen(req, *a, **k):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "bing.com" in url:
            return _FakeResp(_BING_HTML)
        raise OSError("offline in test")  # Tavily / SearXNG 不可用 → 降级到 Bing

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    out = await tool_web_search("示例查询词条", num_results=3)

    assert "示例标题A" in out, f"搜索结果未返回: {out}"
    assert captured, "web_search 成功后应把摘要写入 WebKnowledgeStore"
    assert captured[0].count() == 1
    stored = captured[0].all()[0]
    assert "示例标题A" in stored.summary
    assert stored.query == "示例查询词条"
