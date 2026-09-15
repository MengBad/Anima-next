"""P3-2 测试：受控仓库只读访问 + 有依据的改进提案。

本文档锁死**边界**而不是「功能能用」——因为这个能力的价值全在边界上：

- 只读（模块内无写路径）+ 根目录约束（`..` / 绝对路径 / 软链跳出都拦）
- 拒绝名单真的拦住密钥、生产配置（`config.yaml` 含 WebUI token）、生产 `data/`、VCS/依赖
- 有界（字节/行/条数/命中上限）且**截断要如实说**
- 每次读都有 `repo_read` 审计事件
- 工具：OWNER + 私聊专属 + 配置可一键停用；提案只登记、绝不写盘/部署
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from anima.agent.repo_read import (
    DEFAULT_DENY_PATTERNS,
    RepoPathDenied,
    RepoReader,
    build_repo_reader,
    repo_read_enabled,
)
from anima.tools import LEVEL_OWNER, LEVEL_READONLY, ToolRegistry, _CALL_CONTEXT
from anima.tools.builtin.repo import register_repo_tools


# ---------------------------------------------------------------------------
# 替身 / 夹具
# ---------------------------------------------------------------------------

class _Audit:
    def __init__(self):
        self.events: list[dict] = []

    def emit(self, event, **kw):
        self.events.append({"event": event, **kw})


class _Core:
    def __init__(self, data_dir, *, enabled=True, audit=None, **over):
        cfg = type("R", (), {
            "enabled": enabled, "max_bytes": over.get("max_bytes", 20_000),
            "max_lines": over.get("max_lines", 400), "max_files": over.get("max_files", 40),
            "max_hits": over.get("max_hits", 40), "deny_extra": over.get("deny_extra", []),
        })()
        self.config = type("C", (), {
            "repo_read": cfg,
            "persistence": type("P", (), {"data_dir": str(data_dir)})(),
        })()
        self.audit = audit


@pytest.fixture
def repo(tmp_path):
    """一个迷你仓库：含可读源码 + 各类应被拒绝的敏感文件。"""
    root = tmp_path / "repo"
    (root / "anima" / "core").mkdir(parents=True)
    (root / "data").mkdir()
    (root / ".git").mkdir()
    (root / "anima" / "__pycache__").mkdir()
    (root / "anima" / "app.py").write_text(
        "\n".join(f"line{i}" for i in range(1, 51)) + "\nslow(a,b)", encoding="utf-8")
    (root / "anima" / "core" / "security.py").write_text("SECRET_FENCE = True\n", encoding="utf-8")
    (root / "config.yaml").write_text("webui:\n  token: super-secret-token\n", encoding="utf-8")
    (root / ".secrets").write_text("ANIMA_LLM_API_KEY=sk-real\n", encoding="utf-8")
    (root / ".env").write_text("TOKEN=abc\n", encoding="utf-8")
    (root / "data" / "chat_memory.json").write_text('{"private":"用户隐私"}', encoding="utf-8")
    (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (root / "anima" / "__pycache__" / "app.pyc").write_bytes(b"\x00\x01\x02")
    (root / "anima" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00binary")
    return root


def _reader(repo, **kw) -> RepoReader:
    return RepoReader(repo, **kw)


# ---------------------------------------------------------------------------
# 1. 拒绝名单 / 根约束
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rel", [
    "config.yaml", ".secrets", ".env", "data/chat_memory.json",
    ".git/config", "anima/__pycache__/app.pyc", "anima/logo.png",
])
def test_denied_paths_are_refused(repo, rel):
    out = _reader(repo).read_text(rel)
    assert out["ok"] is False
    assert out["content"] == ""
    assert "拒绝名单" in out["reason"] or "二进制" in out["reason"]


@pytest.mark.parametrize("rel", [
    "../outside.txt", "anima/../../etc/passwd", "/etc/passwd", "~/secret",
])
def test_path_escape_is_refused(repo, rel):
    with pytest.raises(RepoPathDenied):
        _reader(repo).resolve(rel)


def test_symlink_escape_is_refused(repo, tmp_path):
    """软链必须按 resolve 后的真实路径判定，否则是现成的越狱通道。"""
    outside = tmp_path / "outside.txt"
    outside.write_text("机密", encoding="utf-8")
    link = repo / "link.txt"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不支持创建软链")
    with pytest.raises(RepoPathDenied):
        _reader(repo).resolve("link.txt")


def test_deny_extra_is_honoured(repo):
    (repo / "notes.private").write_text("x", encoding="utf-8")
    assert _reader(repo).read_text("notes.private")["ok"] is True
    r = _reader(repo, deny_patterns=DEFAULT_DENY_PATTERNS + ("*.private",))
    assert r.read_text("notes.private")["ok"] is False


def test_normal_source_is_readable(repo):
    out = _reader(repo).read_text("anima/app.py")
    assert out["ok"] is True
    assert "line1" in out["content"]
    assert out["total_lines"] == 51


def test_missing_file_is_honest(repo):
    out = _reader(repo).read_text("anima/nope.py")
    assert out["ok"] is False
    assert "不存在" in out["reason"]


def test_binary_refused_not_silently_empty(repo):
    out = _reader(repo).read_text("anima/logo.png")
    assert out["ok"] is False
    assert "二进制" in out["reason"]


# ---------------------------------------------------------------------------
# 2. 有界 + 截断诚实
# ---------------------------------------------------------------------------

def test_line_window_and_limits(repo):
    r = _reader(repo, max_lines=10)
    out = r.read_text("anima/app.py")
    assert out["ok"] is True
    assert out["truncated"] is True
    assert "行数超过上限" in "；".join(out["notes"])
    assert len(out["content"].splitlines()) == 10


def test_byte_limit_is_disclosed(repo):
    r = _reader(repo, max_bytes=200)
    out = r.read_text("anima/app.py")
    assert out["ok"] is True
    assert out["truncated"] is True
    assert "字节数超过上限" in "；".join(out["notes"])


def test_start_end_line_window(repo):
    out = _reader(repo).read_text("anima/app.py", start_line=5, end_line=7)
    assert out["range"] == [5, 7]
    assert out["content"].splitlines() == ["line5", "line6", "line7"]


def test_list_dir_is_bounded_and_hides_denied(repo):
    (repo / "many").mkdir()
    for i in range(60):
        (repo / "many" / f"f{i:02d}.py").write_text("x", encoding="utf-8")
    out = _reader(repo, max_files=5).list_dir("many")
    assert out["truncated"] is True
    assert len(out["entries"]) <= 5

    root_out = _reader(repo).list_dir("", depth=2)
    joined = "\n".join(root_out["entries"])
    for hidden in ("config.yaml", ".secrets", ".env", "data/", ".git/", "__pycache__"):
        assert hidden not in joined, hidden


def test_grep_is_bounded_and_skips_denied(repo):
    out = _reader(repo).grep("line", glob="**/*")
    assert out["ok"] is True
    assert out["files_skipped"] >= 1        # 至少跳过 .pyc / 被拒文件
    hits = _reader(repo, max_hits=3).grep("line", glob="**/*.py")
    assert len(hits["hits"]) <= 3
    assert hits["truncated"] is True


def test_grep_bad_regex_is_honest(repo):
    out = _reader(repo).grep("([", glob="**/*.py")
    assert out["ok"] is False
    assert "正则非法" in out["reason"]


# ---------------------------------------------------------------------------
# 3. 审计
# ---------------------------------------------------------------------------

def test_reads_emit_audit_events(repo):
    audit = _Audit()
    r = _reader(repo, audit=audit)
    r.read_text("anima/app.py")
    r.list_dir("anima")
    r.grep("line", glob="**/*.py")
    actions = [e["action"] for e in audit.events]
    assert actions == ["read", "list", "grep"]
    assert all(e["event"] == "repo_read" for e in audit.events)
    assert audit.events[0]["path"].endswith("anima/app.py")


def test_audit_failure_does_not_break_read(repo):
    class _BadAudit:
        def emit(self, *a, **k):
            raise RuntimeError("audit down")

    out = _reader(repo, audit=_BadAudit()).read_text("anima/app.py")
    assert out["ok"] is True


# ---------------------------------------------------------------------------
# 4. 模块本身不含写路径
# ---------------------------------------------------------------------------

def test_module_has_no_write_apis():
    import inspect

    import anima.agent.repo_read as mod

    src = inspect.getsource(mod)
    for bad in ("write_text(", "write_bytes(", "unlink(", "rmtree(", "rename(",
                "open(..., \"w\"", "shutil.move"):
        assert bad not in src, f"只读模块不应出现 {bad}"
    # 唯一允许的写路径是审计 sink（在 _emit 内，通过注入的 audit 对象）
    assert "self._audit.emit" in src


# ---------------------------------------------------------------------------
# 5. 工具注册与边界
# ---------------------------------------------------------------------------

def test_tools_registered_only_when_enabled(repo, tmp_path):
    on = _Core(tmp_path, enabled=True)
    off = _Core(tmp_path, enabled=False)
    r_on, r_off = ToolRegistry(core=on), ToolRegistry(core=off)
    register_repo_tools(r_on, on)
    register_repo_tools(r_off, off)
    assert {"repo_list", "repo_read", "repo_grep", "propose_repo_change"} <= set(r_on.tools)
    assert not (set(r_off.tools) & {"repo_list", "repo_read", "repo_grep", "propose_repo_change"})


def test_tools_are_owner_only_and_private_only(repo, tmp_path):
    core = _Core(tmp_path)
    reg = ToolRegistry(core=core)
    register_repo_tools(reg, core)
    for name in ("repo_list", "repo_read", "repo_grep", "propose_repo_change"):
        tool = reg.tools[name]
        assert tool.min_level == LEVEL_OWNER, name
        assert tool.private_only is True, name


def test_repo_tools_hidden_in_group_chat(repo, tmp_path):
    core = _Core(tmp_path)
    reg = ToolRegistry(core=core)
    register_repo_tools(reg, core)

    token = _CALL_CONTEXT.set({"detail_type": "group", "group_id": 727631526})
    try:
        names_group = {s["function"]["name"] for s in reg.get_schemas(LEVEL_OWNER)}
    finally:
        _CALL_CONTEXT.reset(token)
    names_private = {s["function"]["name"] for s in reg.get_schemas(LEVEL_OWNER)}

    assert "repo_read" not in names_group, "源码不得从群聊被读出去"
    assert "repo_read" in names_private


def test_repo_tools_invisible_below_owner(repo, tmp_path):
    core = _Core(tmp_path)
    reg = ToolRegistry(core=core)
    register_repo_tools(reg, core)
    names = {s["function"]["name"] for s in reg.get_schemas(LEVEL_READONLY)}
    assert not (names & {"repo_list", "repo_read", "repo_grep", "propose_repo_change"})


def _tools(repo, tmp_path, **kw):
    """注册工具并把读取根注入成测试夹具仓库（生产用仓库自身位置）。"""
    core = _Core(tmp_path, **kw)
    reg = ToolRegistry(core=core)
    register_repo_tools(reg, core, reader_factory=lambda: _reader(repo, audit=core.audit))
    return core, reg


async def test_repo_read_tool_returns_real_content(repo, tmp_path):
    _, reg = _tools(repo, tmp_path)
    out = await reg.tools["repo_read"].handler(path="anima/app.py")
    assert "line1" in out and "共 51 行" in out


async def test_repo_read_tool_reports_denied_honestly(repo, tmp_path):
    _, reg = _tools(repo, tmp_path)
    out = await reg.tools["repo_read"].handler(path="config.yaml")
    assert "读取失败" in out and "拒绝名单" in out


async def test_repo_grep_tool_reports_hits(repo, tmp_path):
    _, reg = _tools(repo, tmp_path)
    out = await reg.tools["repo_grep"].handler(pattern="slow", glob="**/*.py")
    assert "anima/app.py" in out


async def test_repo_list_tool_hides_denied(repo, tmp_path):
    _, reg = _tools(repo, tmp_path)
    out = await reg.tools["repo_list"].handler(path="", depth=1)
    assert "anima/" in out and ".secrets" not in out


# ---------------------------------------------------------------------------
# 6. 提案：只登记、不落地
# ---------------------------------------------------------------------------

async def test_propose_repo_change_registers_pending_owner_with_real_grounding(repo, tmp_path):
    from anima.agent.self_evolution import get_evolution_gate

    _, reg = _tools(repo, tmp_path)

    out = await reg.tools["propose_repo_change"].handler(
        targets="anima/app.py", request="把 app.py 里的 slow 改成异步")

    assert "提案已登记" in out
    assert "等待 Owner 审批" in out
    assert "代码依据：1 个文件已读" in out
    # 真的读到了真实代码作为依据
    gate = get_evolution_gate()
    pid = out.split("：")[1].split("\n")[0].strip()
    proposal = gate.get(pid)
    assert proposal is not None
    assert "line1" in proposal.evidence
    assert proposal.status == "proposed"


async def test_propose_repo_change_never_writes_or_deploys(repo, tmp_path):
    """碰过的文件必须一字不改（提案是提案，不是补丁）。"""
    _, reg = _tools(repo, tmp_path)
    before = (repo / "anima" / "app.py").read_text(encoding="utf-8")
    out = await reg.tools["propose_repo_change"].handler(
        targets="anima/app.py", request="随便改点什么")
    assert "不部署" in out
    assert (repo / "anima" / "app.py").read_text(encoding="utf-8") == before


async def test_propose_repo_change_core_protected_is_rejected(repo, tmp_path):
    # 夹具里放一份"受保护核心文件"的等价物：用真实 CORE_PROTECTED 路径名，
    # 并确保该路径在夹具仓库里可读，从而验证围栏（而非"读不到"）在起作用。
    protected = "anima/core/security.py"
    assert protected in __import__(
        "anima.agent.self_evolution", fromlist=["CORE_PROTECTED"]).CORE_PROTECTED
    _, reg = _tools(repo, tmp_path)
    out = await reg.tools["propose_repo_change"].handler(
        targets=protected, request="把权限检查去掉")
    assert "安全围栏拒绝自动落地" in out
    assert "人工 PR" in out


async def test_propose_repo_change_requires_request(repo, tmp_path):
    _, reg = _tools(repo, tmp_path)
    out = await reg.tools["propose_repo_change"].handler(targets="anima/app.py", request="  ")
    assert "不能为空" in out


async def test_propose_repo_change_denied_target_is_disclosed(repo, tmp_path):
    """依据读不到就必须说出来，不能让 Owner 以为提案有代码依据。"""
    _, reg = _tools(repo, tmp_path)
    out = await reg.tools["propose_repo_change"].handler(
        targets="config.yaml", request="改配置")
    assert "未取到依据" in out and "拒绝名单" in out


# ---------------------------------------------------------------------------
# 7. 配置
# ---------------------------------------------------------------------------

def test_config_defaults():
    from anima.core.config import AnimaConfig

    cfg = AnimaConfig()
    assert cfg.repo_read.enabled is True
    assert cfg.repo_read.max_bytes == 20_000
    assert cfg.repo_read.max_lines == 400
    assert cfg.repo_read.deny_extra == []


def test_repo_read_enabled_fails_closed_without_config():
    class _NoCfg:
        config = type("C", (), {})()
    assert repo_read_enabled(_NoCfg()) is False
    assert repo_read_enabled(type("C", (), {"config": None})()) is False


def test_build_repo_reader_uses_config(tmp_path):
    core = _Core(tmp_path, max_bytes=1234, max_lines=9, deny_extra=["*.zzz"])
    r = build_repo_reader(core)
    assert r.max_bytes == 1234 and r.max_lines == 9
    assert "*.zzz" in r.deny_patterns
