"""工具自生长（P0 工具调用自学习 + P1 能力自生长系统）测试。

全部使用隔离 tmp_path，不触碰生产 data/，不触发真实 LLM。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from anima.agent.react import ReActLoop
from anima.tools import ToolRegistry, Tool, LEVEL_USER, register_capability_as_tool
from anima.cognitive.tool_learning import ToolLearning
from anima.cognitive.capabilities import CapabilityManager
from anima.cognitive.capability_dedup import find_similar_capability


# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------

class FakeLLM:
    """可脚本化的假 LLM：chat_structured / chat 均按队列返回。"""

    def __init__(self, structured_responses=None, chat_responses=None):
        self._structured = list(structured_responses or [])
        self._chat = list(chat_responses or [])
        self.model = "fake"

    async def chat_structured(self, messages, tools=None, temperature=0.8, max_tokens=512):
        if self._structured:
            return self._structured.pop(0)
        return {"tool_calls": [], "content": "（默认回复）"}

    async def chat(self, messages, temperature=0.8, max_tokens=512):
        if self._chat:
            return self._chat.pop(0)
        return "（默认回复）"


def _fake_core(tools, llm):
    """构造最小 core 桩（含 tools/llm/config.persistence.data_dir）。"""
    class _Cfg:
        class P:
            data_dir = "data"
        persistence = P()
    core = type("Core", (), {})()
    core.tools = tools
    core.llm = llm
    core.config = _Cfg()
    return core


# ---------------------------------------------------------------------------
# P0.1 ReActLoop.tool_trace
# ---------------------------------------------------------------------------

def test_react_tool_trace_collected():
    registry = ToolRegistry()
    registry.register(Tool(
        "echo", "回声", {"x": {"type": "string"}},
        lambda x: "RESULT", LEVEL_USER,
    ))
    llm = FakeLLM(structured_responses=[
        {"tool_calls": [{"function": {"name": "echo", "arguments": json.dumps({"x": "hi"})}}], "content": ""},
        {"tool_calls": [], "content": "final"},
    ])
    react = ReActLoop(llm, registry, max_rounds=5, permission_level=LEVEL_USER)
    reply = asyncio.run(react.run([{"role": "user", "content": "go"}]))
    assert reply == "final"
    assert len(react.tool_trace) == 1
    t = react.tool_trace[0]
    assert t["name"] == "echo"
    assert t["ok"] is True
    assert t["round"] == 1


def test_react_tool_trace_failure_marked():
    registry = ToolRegistry()
    def _boom():
        raise RuntimeError("boom")
    registry.register(Tool("boom", "爆炸", {}, _boom, LEVEL_USER))
    llm = FakeLLM(structured_responses=[
        {"tool_calls": [{"function": {"name": "boom", "arguments": "{}"}}], "content": ""},
        {"tool_calls": [], "content": "final"},
    ])
    react = ReActLoop(llm, registry, max_rounds=5, permission_level=LEVEL_USER)
    asyncio.run(react.run([{"role": "user", "content": "go"}]))
    assert react.tool_trace and react.tool_trace[0]["ok"] is False


# ---------------------------------------------------------------------------
# P0.2 ToolLearning
# ---------------------------------------------------------------------------

def test_tool_learning_record_and_prefs(tmp_path):
    tl = ToolLearning(str(tmp_path))
    trace = [{"name": "echo", "args": {}, "result": "r", "ok": True, "round": 1}]
    tl.record("user", trace, "hello")
    assert len(tl._data["records"]) == 1
    assert tl._data["preferences"]["echo"]["success_count"] == 1
    # 环形上限
    for _ in range(250):
        tl.record("user", [{"name": "echo", "args": {}, "result": "r", "ok": True, "round": 1}])
    assert len(tl._data["records"]) <= tl.records_max


def test_tool_learning_rules_fragment_empty_when_no_rules(tmp_path):
    tl = ToolLearning(str(tmp_path))
    assert tl.rules_fragment() == ""


def test_tool_learning_summarize_and_fragment(tmp_path):
    tl = ToolLearning(str(tmp_path))
    # 制造 3 条成功记录
    for _ in range(3):
        tl.record("user", [{"name": "get_weather", "args": {}, "result": "r", "ok": True, "round": 1}])
    llm = FakeLLM(chat_responses=['{"rule": "下雨优先问天气"}'])
    assert "get_weather" in tl.summarize_due_tools()
    out = asyncio.run(tl.summarize_rules(llm, "get_weather"))
    assert out == "下雨优先问天气"
    frag = tl.rules_fragment()
    assert "get_weather" in frag
    assert "下雨优先问天气" in frag


# ---------------------------------------------------------------------------
# P1.1 capability_dedup
# ---------------------------------------------------------------------------

def test_dedup_core_slots_merge():
    # 核心语义槽位（_self_ + _anchor_）共享 ≥2 → 合并（next-gen 去重核心）
    caps = [{"name": "自我锚定", "description": "锚定自我身份"}]
    idx = find_similar_capability("锚定本我", "锚定自我认知", caps, 0.6)
    assert idx == 0


def test_dedup_text_fallback_merge():
    # 高相似中文长名 → 文本 2-gram Jaccard ≥ 0.6 兜底合并
    caps = [{"name": "每日情绪回顾术", "description": "x"}]
    idx = find_similar_capability("每日情绪回顾", "x", caps, 0.6)
    assert idx == 0


def test_dedup_no_false_merge():
    caps = [{"name": "情绪回顾", "description": "梳理每日心情"}]
    idx = find_similar_capability("计算质数", "用筛法求素数", caps, 0.6)
    assert idx == -1


# ---------------------------------------------------------------------------
# P1.2 CapabilityManager
# ---------------------------------------------------------------------------

def test_capability_create_baseline_confidence(tmp_path):
    mgr = CapabilityManager(str(tmp_path))
    name = mgr.create_or_update({
        "name": "情绪回顾", "description": "梳理每日情绪",
        "how_to_use": "每晚回顾今天的心情", "register_as_independent_tool": True,
    })
    assert name == "情绪回顾"
    caps = mgr._data["capabilities"]
    assert len(caps) == 1
    assert caps[0]["confidence"] == 0.3  # 脱钩 LLM 自评


def test_capability_exact_name_dedup(tmp_path):
    # 第一道去重：同名精确匹配 → 合并为 1 条
    mgr = CapabilityManager(str(tmp_path))
    mgr.create_or_update({"name": "情绪回顾", "description": "梳理每日情绪",
                          "how_to_use": "x", "register_as_independent_tool": True})
    mgr.create_or_update({"name": "情绪回顾", "description": "更新版",
                          "how_to_use": "x", "register_as_independent_tool": True})
    assert len(mgr._data["capabilities"]) == 1


def test_capability_feedback(tmp_path):
    mgr = CapabilityManager(str(tmp_path))
    mgr.create_or_update({"name": "情绪回顾", "description": "梳理每日情绪",
                          "how_to_use": "x", "register_as_independent_tool": True})
    assert mgr.apply_feedback("情绪回顾", True) is True
    cap = mgr._data["capabilities"][0]
    assert cap["confidence"] == pytest.approx(0.38)
    assert cap["usage_count"] == 1
    mgr.apply_feedback("情绪回顾", False, "效果一般")
    cap = mgr._data["capabilities"][0]
    assert cap["confidence"] == pytest.approx(0.23)
    assert len(cap["corrections"]) == 1


def test_capability_maintain_health_drops_unused_old(tmp_path):
    mgr = CapabilityManager(str(tmp_path))
    mgr.create_or_update({"name": "旧能力", "description": "很久没用",
                          "how_to_use": "x", "register_as_independent_tool": True})
    mgr._data["capabilities"][0]["last_updated"] = "2000-01-01T00:00:00"
    mgr._data["capabilities"][0]["usage_count"] = 0
    mgr.maintain_health()
    assert len(mgr._data["capabilities"]) == 0  # 超 30 天未用 → 淘汰


def test_capability_build_fragment(tmp_path):
    mgr = CapabilityManager(str(tmp_path))
    mgr.create_or_update({"name": "情绪回顾", "description": "梳理每日情绪",
                          "how_to_use": "每晚回顾", "register_as_independent_tool": True})
    frag = mgr.build_fragment()
    assert "情绪回顾" in frag
    assert "我的个人技能与方法论" in frag


# ---------------------------------------------------------------------------
# P1.3 能力工具工厂 + 动态注册
# ---------------------------------------------------------------------------

def test_make_capability_tool_registered_visible_and_callable(tmp_path):
    registry = ToolRegistry()
    llm = FakeLLM(chat_responses=["EXEC_RESULT"])
    core = _fake_core(registry, llm)
    mgr = CapabilityManager(str(tmp_path), dynamic_daily_quota=5)
    cap = {
        "id": "cap_test123", "name": "情绪回顾", "description": "梳理情绪",
        "how_to_use": "回顾今日心情", "register_as_independent_tool": True,
    }
    ok = register_capability_as_tool(core, cap, mgr)
    assert ok is True
    # 对 LLM 可见
    schemas = registry.get_schemas(LEVEL_USER)
    names = [s["function"]["name"] for s in schemas]
    assert "my_cap_test123" in names
    # 调用走 executor（LLM 提示词方法，无代码 eval）
    result = asyncio.run(registry.call("my_cap_test123", {"query_or_args": "今天好丧"}, LEVEL_USER))
    assert "EXEC_RESULT" in result
    assert mgr._daily_register["count"] == 1


def test_register_same_name_skipped(tmp_path):
    registry = ToolRegistry()
    llm = FakeLLM(chat_responses=["EXEC"])
    core = _fake_core(registry, llm)
    mgr = CapabilityManager(str(tmp_path), dynamic_daily_quota=5)
    cap = {"id": "cap_a", "name": "能力A", "description": "x",
           "how_to_use": "x", "register_as_independent_tool": True}
    assert register_capability_as_tool(core, cap, mgr) is True
    assert register_capability_as_tool(core, cap, mgr) is False  # 同名跳过


def test_register_daily_quota(tmp_path):
    registry = ToolRegistry()
    llm = FakeLLM(chat_responses=["EXEC"])
    core = _fake_core(registry, llm)
    mgr = CapabilityManager(str(tmp_path), dynamic_daily_quota=1)
    cap1 = {"id": "cap_a", "name": "能力A", "description": "x",
            "how_to_use": "x", "register_as_independent_tool": True}
    cap2 = {"id": "cap_b", "name": "能力B", "description": "x",
            "how_to_use": "x", "register_as_independent_tool": True}
    assert register_capability_as_tool(core, cap1, mgr) is True
    assert register_capability_as_tool(core, cap2, mgr) is False  # 配额耗尽


def test_register_respects_independent_flag(tmp_path):
    registry = ToolRegistry()
    llm = FakeLLM(chat_responses=["EXEC"])
    core = _fake_core(registry, llm)
    mgr = CapabilityManager(str(tmp_path), dynamic_daily_quota=5)
    cap = {"id": "cap_c", "name": "能力C", "description": "x",
           "how_to_use": "x", "register_as_independent_tool": False}
    assert register_capability_as_tool(core, cap, mgr) is False  # 不愿被独立调用
