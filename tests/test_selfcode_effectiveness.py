"""SelfCode 效果评估与建议卸载（不自动删除）。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from anima.cognitive.selfcode import SelfCodeManager
from anima.tools import ToolRegistry


def _write_tool(path: Path, name: str = "eff_demo") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "TOOL_META = {\n"
        f'    "name": "{name}",\n'
        '    "description": "eff demo",\n'
        '    "parameters": {"x": {"type": "string", "description": "x"}},\n'
        '    "required": ["x"],\n'
        '    "min_level": "user"\n'
        "}\n"
        "async def run(core, **params):\n"
        "    return str(params.get('x') or '')\n",
        encoding="utf-8",
    )
    return path


def _mgr(tmp_path: Path) -> SelfCodeManager:
    class Core:
        class C:
            class SC:
                enabled = True
                workspace_dir = ""
                selfcode_dir = ""
            self_code = SC()
            class P:
                data_dir = str(tmp_path)
            persistence = P()
        config = C()
        tools = ToolRegistry()
    m = SelfCodeManager(Core())
    m.data_dir = tmp_path
    m.selfcode_dir = tmp_path / "selfcode"
    m.workspace = tmp_path / "ws"
    m.audit_path = tmp_path / "audit.json"
    m.quota_path = tmp_path / "quota.json"
    m.stats_path = tmp_path / "stats.json"
    m._quota = {"date": "", "count": 0}
    m._stats = {"tools": {}}
    return m


@pytest.mark.asyncio
async def test_handler_records_calls(tmp_path):
    m = _mgr(tmp_path)
    path = _write_tool(m.selfcode_dir / "eff_demo.py")
    meta = {
        "name": "eff_demo",
        "description": "eff demo",
        "parameters": {"x": {"type": "string", "description": "x"}},
        "required": ["x"],
        "min_level": "user",
    }
    ok, _ = m._register_runtime_module(path, meta, count_install=True)
    assert ok
    tool = m.core.tools.tools["eff_demo"]
    out = await tool.handler(x="hi")
    assert out == "hi"
    st = m.get_tool_stats("eff_demo")
    assert st["calls"] == 1
    assert st["ok"] == 1


@pytest.mark.asyncio
async def test_handler_records_fail_prefix(tmp_path):
    m = _mgr(tmp_path)
    path = m.selfcode_dir / "fail_demo.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "TOOL_META = {\n"
        '    "name": "fail_demo",\n'
        '    "description": "f",\n'
        '    "parameters": {},\n'
        '    "required": [],\n'
        '    "min_level": "user"\n'
        "}\n"
        "async def run(core, **params):\n"
        '    return "错误: boom"\n',
        encoding="utf-8",
    )
    meta = {
        "name": "fail_demo", "description": "f", "parameters": {},
        "required": [], "min_level": "user",
    }
    m._register_runtime_module(path, meta, count_install=False)
    await m.core.tools.tools["fail_demo"].handler()
    st = m.get_tool_stats("fail_demo")
    assert st["fail"] == 1
    assert st["ok"] == 0


def test_evaluate_idle_never_used(tmp_path):
    m = _mgr(tmp_path)
    path = _write_tool(m.selfcode_dir / "idle_tool.py", "idle_tool")
    meta = {
        "name": "idle_tool", "description": "i",
        "parameters": {"x": {"type": "string", "description": "x"}},
        "required": ["x"], "min_level": "user",
    }
    m._register_runtime_module(path, meta, count_install=False)
    st = m._tool_stat("idle_tool")
    st["installed_at"] = time.time() - 72 * 3600
    m._save_stats()
    sug = m.evaluate_tools(min_age_hours=48.0)
    assert any(s["name"] == "idle_tool" for s in sug)
    items = m.list_installed()
    assert items[0]["stats"]["suggest_uninstall"] is True


def test_evaluate_high_fail_rate(tmp_path):
    m = _mgr(tmp_path)
    path = _write_tool(m.selfcode_dir / "bad_tool.py", "bad_tool")
    meta = {
        "name": "bad_tool", "description": "b",
        "parameters": {"x": {"type": "string", "description": "x"}},
        "required": ["x"], "min_level": "user",
    }
    m._register_runtime_module(path, meta, count_install=False)
    for _ in range(6):
        m.record_tool_call("bad_tool", ok=False, error="x")
    m.record_tool_call("bad_tool", ok=True)
    sug = m.evaluate_tools(min_calls_for_fail_rate=5, fail_rate_threshold=0.6)
    assert any(s["name"] == "bad_tool" and "high_fail_rate" in s["reason"] for s in sug)


def test_dismiss_and_delete_clears_stats(tmp_path):
    m = _mgr(tmp_path)
    path = _write_tool(m.selfcode_dir / "gone.py", "gone")
    meta = {
        "name": "gone", "description": "g",
        "parameters": {"x": {"type": "string", "description": "x"}},
        "required": ["x"], "min_level": "user",
    }
    m._register_runtime_module(path, meta, count_install=False)
    m.record_tool_call("gone", ok=True)
    m.evaluate_tools(min_age_hours=0.0)  # may or may not suggest
    m.dismiss_uninstall_suggestion("gone")
    assert m.get_tool_stats("gone").get("suggest_uninstall") is False
    m.delete_tool("gone")
    assert "gone" not in m._stats.get("tools", {})
