"""Phase 2 Agent 工具层测试：权限门控、路径穿越防护、读写往返、命令黑名单、HTTP URL 守卫。"""

import types

import pytest

from anima.agent.audit import AgentAuditor
from anima.tools import LEVEL_OWNER, LEVEL_USER, ToolRegistry
from anima.tools.builtin.agent_tools import (
    agent_exec,
    agent_http_request,
    agent_read_file,
    agent_write_file,
    register_agent_tools,
)


def _make_core(data_dir, auditor=None):
    cfg = types.SimpleNamespace(
        persistence=types.SimpleNamespace(data_dir=str(data_dir))
    )
    return types.SimpleNamespace(config=cfg, audit=None, agent_auditor=auditor)


@pytest.fixture
def core(tmp_path):
    auditor = AgentAuditor(str(tmp_path))
    return _make_core(tmp_path, auditor), tmp_path


# ---------------------------------------------------------------------------
# 权限门控（注册表层面）
# ---------------------------------------------------------------------------

def test_agent_tools_hidden_from_normal_user(core):
    c, _ = core
    reg = ToolRegistry(c)
    register_agent_tools(reg, c)
    schemas_user = reg.get_schemas(LEVEL_USER)
    names_user = {s["function"]["name"] for s in schemas_user}
    assert not any(n.startswith("agent_") for n in names_user)

    schemas_owner = reg.get_schemas(LEVEL_OWNER)
    names_owner = {s["function"]["name"] for s in schemas_owner}
    assert {"agent_http_request", "agent_read_file", "agent_write_file",
            "agent_exec"} <= names_owner


def test_agent_tools_not_registered_without_core():
    reg = ToolRegistry(None)
    register_agent_tools(reg, None)  # 应安全跳过，不抛错
    assert not any(n.startswith("agent_") for n in reg.tools)


# ---------------------------------------------------------------------------
# 路径穿越防护
# ---------------------------------------------------------------------------

async def test_read_rejects_traversal(core):
    c, _ = core
    out = await agent_read_file(c, "../../etc/passwd")
    assert "拒绝工作区外路径" in out


async def test_write_and_read_roundtrip(core):
    c, _ = core
    w = await agent_write_file(c, "result.txt", "hello-agent")
    assert "已写入" in w
    r = await agent_read_file(c, "result.txt")
    assert "hello-agent" in r


async def test_write_rejects_traversal(core):
    c, _ = core
    out = await agent_write_file(c, "../escape.txt", "x")
    assert "拒绝工作区外路径" in out


# ---------------------------------------------------------------------------
# 命令黑名单
# ---------------------------------------------------------------------------

async def test_exec_blocks_dangerous_command(core):
    c, _ = core
    out = await agent_exec(c, "rm -rf /")
    assert "安全护栏拒绝" in out


async def test_exec_runs_safe_command(core):
    c, _ = core
    out = await agent_exec(c, "echo hi-from-agent")
    assert "hi-from-agent" in out


# ---------------------------------------------------------------------------
# HTTP URL 守卫
# ---------------------------------------------------------------------------

async def test_http_rejects_non_http(monkeypatch, core):
    c, _ = core
    out = await agent_http_request(c, "GET", "file:///etc/passwd")
    assert "拒绝" in out


async def test_http_success(monkeypatch, core):
    c, _ = core
    import urllib.error
    import urllib.request

    class _FakeResp:
        status = 200

        def read(self, n=-1):
            return b"pong-body"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=30):
        return _FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    out = await agent_http_request(c, "GET", "https://example.com/x")
    assert "HTTP 200" in out
    assert "pong-body" in out
