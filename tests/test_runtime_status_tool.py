"""固定运行状态工具：不依赖模型拼接 shell，也不回显沙箱细节。"""

import pytest

from anima.tools import _tool_runtime_status, create_default_registry, LEVEL_ADMIN


@pytest.mark.asyncio
async def test_runtime_status_uses_fixed_read_only_commands(monkeypatch):
    import anima.tools as tools

    calls = []

    async def fake_run_command(argv):
        calls.append(argv)
        return {
            ("free", "-h"): "Mem: 1G",
            ("df", "-h", "/"): "Filesystem 10G",
            ("uptime",): "up 1 day",
        }[argv]

    monkeypatch.setattr(tools, "_run_runtime_status_command", fake_run_command)
    result = await _tool_runtime_status()

    assert calls == [("free", "-h"), ("df", "-h", "/"), ("uptime",)]
    assert "【内存】" in result
    assert "【磁盘】" in result


@pytest.mark.asyncio
async def test_runtime_status_hides_sandbox_error(monkeypatch):
    import anima.tools as tools

    async def fake_run_command(argv):
        return None

    monkeypatch.setattr(tools, "_run_runtime_status_command", fake_run_command)
    result = await _tool_runtime_status()

    assert result == "运行状态暂时不可用，本喵不想拿不准的数据糊弄你。"
    assert "沙箱" not in result


def test_runtime_status_tool_is_admin_only():
    tool = create_default_registry().tools["get_runtime_status"]
    assert tool.min_level == LEVEL_ADMIN
