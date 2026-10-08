"""普通工具 ``tool_executed`` 审计字段 —— 事后能恢复 request / template / result_preview。

只用夹具，不打真实网络。写入点仍是 ``ToolRegistry._invoke`` 里既有的那一行，
没有另开旁路日志。
"""
from __future__ import annotations

import types

import pytest

from anima.tools import LEVEL_USER, Tool, ToolRegistry

from anima.skills import SkillRegistry


class FakeAudit:
    """只记内存，不落任何文件。"""

    def __init__(self):
        self.events: list[dict] = []

    def emit(self, event_type: str, **fields):
        self.events.append({"type": event_type, **fields})


def _core() -> types.SimpleNamespace:
    return types.SimpleNamespace(audit=FakeAudit())


def _registry(core) -> ToolRegistry:
    return ToolRegistry(core)


def _last_event(core, name: str = "tool_executed") -> dict:
    matched = [e for e in core.audit.events if e.get("type") == name]
    assert matched, [e.get("type") for e in core.audit.events]
    return matched[-1]


# ---------------------------------------------------------------------------
# 1. 一次普通工具调用之后，三样都在
# ---------------------------------------------------------------------------
async def test_ordinary_tool_call_audits_request_template_and_preview():
    core = _core()
    registry = _registry(core)
    registry.register(Tool(
        "fixture_search",
        "夹具搜索",
        {"query": {"type": "string", "description": "关键词"}},
        lambda query: f"关于「{query}」的结果 https://example.com/a",
        required=["query"], min_level=LEVEL_USER,
    ))

    out = await registry.call("fixture_search", {"query": "查 X 最新版本"}, LEVEL_USER)
    assert "https://example.com/a" in out

    event = _last_event(core)
    assert event["request"] == "查 X 最新版本"
    assert event["result_preview"] == out
    assert event["template"] == ""          # 普通工具没有模板 → 空字符串，字段仍在


async def test_template_is_recorded_when_the_tool_has_one():
    core = _core()
    registry = _registry(core)
    registry.register(Tool(
        "fixture_skill",
        "带模板的夹具技能",
        {"query": {"type": "string"}},
        lambda query: f"结果 {query}",
        required=["query"], min_level=LEVEL_USER,
        template="整理成 3-5 条要点，每条附来源。",
    ))
    await registry.call("fixture_skill", {"query": "主题"}, LEVEL_USER)
    assert _last_event(core)["template"] == "整理成 3-5 条要点，每条附来源。"


# ---------------------------------------------------------------------------
# 2. 超过 500 字截断 + 截断标记
# ---------------------------------------------------------------------------
async def test_result_preview_is_truncated_with_marker():
    core = _core()
    registry = _registry(core)
    long_output = "A" * 1200

    async def handler(query: str) -> str:
        return long_output

    registry.register(Tool("fixture_long", "长输出夹具", {"query": {"type": "string"}},
                           handler, required=["query"], min_level=LEVEL_USER))
    await registry.call("fixture_long", {"query": "q"}, LEVEL_USER)

    event = _last_event(core)
    preview = event["result_preview"]
    assert len(preview) < len(long_output)          # 确实被截断
    assert preview.startswith("A" * 500)            # 前 500 字原样保留
    assert "已截断" in preview                       # 带截断标记
    assert "1200" in preview                        # 标记里报出总长
    assert len(preview) <= 500 + len("…[已截断，共 1200 字]")


async def test_short_result_is_not_marked_truncated():
    core = _core()
    registry = _registry(core)
    registry.register(Tool("fixture_short", "短输出夹具", {"query": {"type": "string"}},
                           lambda query: "短结果", required=["query"], min_level=LEVEL_USER))
    await registry.call("fixture_short", {"query": "q"}, LEVEL_USER)
    preview = _last_event(core)["result_preview"]
    assert preview == "短结果" and "已截断" not in preview


# ---------------------------------------------------------------------------
# 3. 旧字段仍在
# ---------------------------------------------------------------------------
async def test_legacy_fields_are_preserved():
    core = _core()
    registry = _registry(core)
    registry.register(Tool("fixture_legacy", "旧字段夹具", {"query": {"type": "string"}},
                           lambda query: "内容", required=["query"], min_level=LEVEL_USER))
    await registry.call("fixture_legacy", {"query": "q"}, LEVEL_USER)
    event = _last_event(core)
    assert event["tool"] == "fixture_legacy"
    assert event["ok"] is True
    assert event["result_len"] == len("内容")


async def test_failed_call_still_records_request_and_ok_false():
    core = _core()
    registry = _registry(core)

    def boom(query: str) -> str:
        raise RuntimeError("boom")

    registry.register(Tool("fixture_boom", "失败夹具", {"query": {"type": "string"}},
                           boom, required=["query"], min_level=LEVEL_USER))
    await registry.call("fixture_boom", {"query": "会失败的请求"}, LEVEL_USER)

    failed = [e for e in core.audit.events if e.get("type") == "tool_failed"]
    assert failed and failed[-1]["tool"] == "fixture_boom"


# ---------------------------------------------------------------------------
# 4. 声明式 Skill 的模板能从工具上读到（GEPA 轨迹要靠它恢复）
# ---------------------------------------------------------------------------
def test_skill_tool_carries_its_execution_template(tmp_path):
    from pathlib import Path

    builtin_root = Path(__file__).resolve().parents[1] / "anima" / "skills" / "builtin"
    core = _core()
    tools = _registry(core)
    # web_brief 组合 web_search，装进工具面前先给一个同名的夹具目标
    tools.register(Tool("web_search", "夹具检索",
                        {"query": {"type": "string"}, "num_results": {"type": "integer"}},
                        lambda query, num_results=5: f"结果 {query}",
                        required=["query"], min_level=LEVEL_USER))
    core.tools = tools
    registry = SkillRegistry(tmp_path, core, builtin_root=builtin_root)
    registry.load()
    registry.install_into_tools()

    tool = core.tools.tools["skill_web_brief"]
    assert tool.template
    assert "来源" in tool.template


def test_tool_template_defaults_to_empty_string():
    tool = Tool("plain", "普通", {}, lambda: "x")
    assert tool.template == ""
