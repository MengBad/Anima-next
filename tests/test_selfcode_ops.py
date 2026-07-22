"""SelfCode Owner 运维：列表 / 禁用 / 删除 / 回滚。"""
from pathlib import Path

import pytest

from anima.cognitive.selfcode import SelfCodeManager
from anima.tools import ToolRegistry


def _write_tool(path: Path, name: str = "ops_demo_tool") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "TOOL_META = {\n"
        f'    "name": "{name}",\n'
        '    "description": "ops demo",\n'
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
    m._quota = {"date": "", "count": 0}
    return m


def test_list_disable_enable_delete_and_restore(tmp_path):
    m = _mgr(tmp_path)
    path = _write_tool(m.selfcode_dir / "ops_demo_tool.py")
    meta = {
        "name": "ops_demo_tool",
        "description": "ops demo",
        "parameters": {"x": {"type": "string", "description": "x"}},
        "required": ["x"],
        "min_level": "user",
    }
    ok, _ = m._register_runtime_module(path, meta, count_install=False)
    assert ok
    items = m.list_installed()
    assert items and items[0]["name"] == "ops_demo_tool" and items[0]["registered"] is True

    disabled = m.set_tool_enabled("ops_demo_tool", False)
    assert disabled["enabled"] is False
    assert "ops_demo_tool" not in m.core.tools.tools
    assert m.list_installed()[0]["enabled"] is False

    enabled = m.set_tool_enabled("ops_demo_tool", True)
    assert enabled["enabled"] is True
    assert "ops_demo_tool" in m.core.tools.tools

    deleted = m.delete_tool("ops_demo_tool")
    assert deleted["deleted"] is True
    assert not path.exists()
    assert deleted.get("backup")
    assert m.list_installed() == []

    backups = m.list_backups()
    assert backups
    restored = m.restore_backup(backups[0]["id"])
    assert restored["loaded"] >= 1
    assert any(item["name"] == "ops_demo_tool" for item in m.list_installed())
