"""双 agent 自纠错循环测试：Coder↔Critic 编排 + 网关同模型桥回退。"""

import asyncio
from pathlib import Path

import pytest

from anima.cognitive.selfcode import SelfCodeManager, _parse_verdict
from anima.core.persistence import PersistenceProvider
from anima.core.tasks import TaskStore


def _write_valid_module(workspace: Path, name="demo_tool.py") -> Path:
    workspace.mkdir(parents=True, exist_ok=True)
    p = workspace / name
    p.write_text(
        "TOOL_META = {\n"
        '    "name": "demo_tool",\n'
        '    "description": "demo tool",\n'
        '    "parameters": {}, "required": [], "min_level": "user"\n'
        "}\n"
        "async def run(core, **params):\n"
        "    return 'ok'\n",
        encoding="utf-8",
    )
    return p


def _make_cfg():
    class Cfg:
        enabled = True
        auto_install = False
        daily_quota = 0
        critic_enabled = True
        max_review_rounds = 2
        model = "gemini-3.5-flash-low"
        claude_executable = "claude"
        coder_backend = "claude"
        permission_mode = "acceptEdits"
        workspace_dir = ""
        selfcode_dir = ""
        max_turns = 12
        timeout_seconds = 180
        test_timeout = 30
        anthropic_api_key = ""
        api_base_url = ""
    return Cfg()


def _make_core():
    class Llm:
        endpoint = "http://look.aicaowo.com:3000/gemini-antigravity/v1"
        model = "gemini-3.5-flash-low"
        api_key = type("S", (), {"get_secret_value": lambda self: "gw-key"})()
    class Config:
        llm = Llm()
    class Core:
        config = Config()
        tools = None
    return Core()


def _make_mgr(tmp_path: Path) -> SelfCodeManager:
    m = SelfCodeManager(_make_core())
    m.cfg = _make_cfg()
    m.workspace = tmp_path / "ws"
    m.selfcode_dir = tmp_path / "sc"
    m.data_dir = tmp_path
    m.audit_path = tmp_path / "audit.json"
    m.quota_path = tmp_path / "quota.json"
    m._quota = {"date": "", "count": 0}
    m._last_out = ""
    m.cfg.workspace_dir = str(m.workspace)
    return m


# ----------------------------------------------------------------------
# _parse_verdict
# ----------------------------------------------------------------------

def test_parse_verdict_approve():
    v, issues = _parse_verdict('{"verdict":"APPROVE","issues":[]}')
    assert v == "APPROVE" and issues == []


def test_parse_verdict_revise():
    v, issues = _parse_verdict('前缀 {"verdict":"REVISE","issues":["缺异常","命名不规范"]} 后缀')
    assert v == "REVISE"
    assert "缺异常" in issues


def test_parse_verdict_fallback_approve():
    assert _parse_verdict("blah APPROVE blah")[0] == "APPROVE"


def test_parse_verdict_fallback_revise():
    assert _parse_verdict("no json")[0] == "REVISE"


# ----------------------------------------------------------------------
# 命令构建 / 环境回退
# ----------------------------------------------------------------------

def test_build_claude_cmd_readonly_has_model_and_readonly_tools():
    m = _make_mgr(Path("/tmp/_nope"))
    ro = m._build_claude_cmd("goal", read_only=True)
    assert "--model" in ro and "Read" in ro and "Bash" not in ro
    coder = m._build_claude_cmd("goal", read_only=False)
    assert "Bash,Edit,Read,Write" in coder


def test_claude_env_falls_back_to_llm_gateway():
    m = _make_mgr(Path("/tmp/_nope"))
    env = m._claude_env()
    assert env["ANTHROPIC_API_KEY"] == "gw-key"
    # llm.endpoint 去 /v1 → Anthropic base（CC 自动补 /v1/messages）
    assert env["ANTHROPIC_BASE_URL"] == "http://look.aicaowo.com:3000/gemini-antigravity"


# ----------------------------------------------------------------------
# 双 agent 循环编排
# ----------------------------------------------------------------------

def test_code_with_review_revises_then_installs(tmp_path):
    """Coder 写模块 + Critic 首轮 REVISE / 次轮 APPROVE：Coder 被调两次、Critic 两次、模块通过自测。"""
    import anima.cognitive.selfcode as sc_mod

    calls = {"coder": 0, "critic": 0}

    async def fake_exec_claude(self, cmd):
        is_read_only = ("Read" in cmd) and ("Bash" not in cmd)
        if is_read_only:
            calls["critic"] += 1
            if calls["critic"] == 1:
                return 0, '{"verdict":"REVISE","issues":["缺少异常处理"],"suggest":"加 try"}'
            return 0, '{"verdict":"APPROVE","issues":[]}'
        calls["coder"] += 1
        _write_valid_module(self.workspace)
        return 0, "wrote module"

    m = _make_mgr(tmp_path)
    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(sc_mod.SelfCodeManager, "_exec_claude", fake_exec_claude)
        result = asyncio.run(m.code_with_review("做一个 demo 工具"))

    assert calls["coder"] == 2, calls
    assert calls["critic"] == 2, calls
    cands = m._scan_candidates()
    assert any(p.name == "demo_tool.py" for p in cands)
    ok, _ = asyncio.run(m._test_module(cands[0]))
    assert ok is True
    assert "等待 Owner" in result


def test_code_with_review_never_auto_installs_even_with_legacy_flag(tmp_path):
    """旧 auto_install=true 不能绕过当前 Owner 审批模型。"""
    import anima.cognitive.selfcode as sc_mod

    calls = {"coder": 0, "critic": 0}

    async def fake_exec_claude(self, cmd):
        if ("Read" in cmd) and ("Bash" not in cmd):
            calls["critic"] += 1
            return 0, '{"verdict":"APPROVE","issues":[]}'
        calls["coder"] += 1
        _write_valid_module(self.workspace)
        return 0, "wrote"

    m = _make_mgr(tmp_path)
    m.cfg.auto_install = True
    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(sc_mod.SelfCodeManager, "_exec_claude", fake_exec_claude)
        result = asyncio.run(m.code_with_review("做一个 demo 工具"))

    assert not (m.selfcode_dir / "demo_tool.py").exists()
    assert "等待 Owner" in result


@pytest.mark.asyncio
async def test_engineering_task_requires_owner_approval_before_install(tmp_path, monkeypatch):
    """双 Agent 产物先入任务提案；点击批准才会调用安装逻辑。"""
    import anima.cognitive.selfcode as sc_mod

    async def fake_exec_claude(self, cmd):
        if ("Read" in cmd) and ("Bash" not in cmd):
            return 0, '{"verdict":"APPROVE","issues":[]}'
        _write_valid_module(self.workspace)
        return 0, "wrote"

    m = _make_mgr(tmp_path)
    m.core.tasks = TaskStore(PersistenceProvider(str(tmp_path)))
    installed = []

    def fake_install(self, path):
        installed.append(path.name)
        return True, "test-installed"

    monkeypatch.setattr(sc_mod.SelfCodeManager, "_exec_claude", fake_exec_claude)
    monkeypatch.setattr(sc_mod.SelfCodeManager, "_install_module", fake_install)
    task = await m.create_engineering_task("做一个 demo 工具", 42)

    assert task.status == "awaiting_approval"
    assert task.kind == "selfcode"
    assert task.proposal["effect"]
    assert task.proposal["candidates"][0]["tool"]["name"] == "demo_tool"
    assert installed == []

    done = await m.approve_engineering_task(task.id, 42)
    assert done.status == "completed"
    assert installed == ["demo_tool.py"]


@pytest.mark.asyncio
async def test_engineering_task_redo_replaces_old_pending_candidate(tmp_path, monkeypatch):
    """Owner 的“要求重做”必须废弃旧候选，并在新隔离任务中重新审查。"""
    import anima.cognitive.selfcode as sc_mod

    async def fake_exec_claude(self, cmd):
        if ("Read" in cmd) and ("Bash" not in cmd):
            return 0, '{"verdict":"APPROVE","issues":[]}'
        _write_valid_module(self.workspace)
        return 0, "wrote"

    m = _make_mgr(tmp_path)
    m.core.tasks = TaskStore(PersistenceProvider(str(tmp_path)))
    monkeypatch.setattr(sc_mod.SelfCodeManager, "_exec_claude", fake_exec_claude)
    original = await m.create_engineering_task("做一个可重做 demo 工具", 42)
    replacement = await m.redo_engineering_task(original.id, 42, "请补充异常处理")

    assert original.id != replacement.id
    assert m.core.tasks.get(original.id).status == "rejected"
    assert replacement.status == "awaiting_approval"


# ----------------------------------------------------------------------
# 原生代码 Agent 后端（coder_backend=native，直接用 bot 的 LLMClient）
# ----------------------------------------------------------------------

class _FakeLlm:
    """模拟 core.llm：首轮当 Coder 产模块，次轮当 Critic 给 APPROVE。"""

    def __init__(self):
        self.calls = []
        self.started = False

    async def start(self):
        self.started = True

    async def close(self):
        pass

    async def chat(self, messages, **kw):
        self.calls.append(messages)
        sys_c = messages[0]["content"]
        if "审查员" in sys_c:  # Critic 系统提示含「代码审查员」
            return '{"verdict":"APPROVE","issues":[]}'
        return (
            "```python\n"
            "TOOL_META = {\n"
            '    "name": "demo_tool",\n'
            '    "description": "demo tool",\n'
            '    "parameters": {}, "required": [], "min_level": "user"\n'
            "}\n"
            "async def run(core, **params):\n"
            "    return 'ok'\n"
            "```"
        )


@pytest.mark.asyncio
async def test_register_nested_json_schema_parameters_is_llm_callable(tmp_path):
    """LLM 常见的完整 JSON Schema parameters 必须被展平，且 handler 接受关键字参数。"""
    from anima.cognitive.selfcode import SelfCodeManager
    from anima.tools import ToolRegistry

    m = _make_mgr(tmp_path)
    m.selfcode_dir.mkdir(parents=True, exist_ok=True)
    path = m.selfcode_dir / "temp_tool.py"
    path.write_text(
        "TOOL_META = {\n"
        '    "name": "convert_celsius_to_fahrenheit",\n'
        '    "description": "c to f",\n'
        '    "parameters": {\n'
        '        "type": "object",\n'
        '        "properties": {\n'
        '            "celsius": {"type": "number", "description": "C"}\n'
        "        },\n"
        '        "required": ["celsius"]\n'
        "    },\n"
        '    "required": ["celsius"],\n'
        '    "min_level": "user"\n'
        "}\n"
        "async def run(core, **params):\n"
        "    c = float(params.get('celsius'))\n"
        "    return f'{c}C = {c * 9 / 5 + 32}F'\n",
        encoding="utf-8",
    )
    m.core.tools = ToolRegistry(m.core)
    ok, msg = m._register_runtime_module(
        path,
        {
            "name": "convert_celsius_to_fahrenheit",
            "description": "c to f",
            "parameters": {
                "type": "object",
                "properties": {"celsius": {"type": "number", "description": "C"}},
                "required": ["celsius"],
            },
            "required": ["celsius"],
            "min_level": "user",
        },
        count_install=False,
    )
    assert ok, msg
    tool = m.core.tools.tools["convert_celsius_to_fahrenheit"]
    schema = tool.to_openai_schema()
    assert "celsius" in schema["function"]["parameters"]["properties"]
    assert schema["function"]["parameters"]["properties"]["celsius"]["type"] == "number"
    result = await m.core.tools.call("convert_celsius_to_fahrenheit", {"celsius": 25}, permission_level=2)
    assert "77" in result


def test_native_backend_generates_pending_proposal(tmp_path):
    """原生后端：LLM 直接写模块 → Critic APPROVE → 形成待审批提案，绝不自动安装。"""
    import anima.cognitive.selfcode as sc_mod

    m = _make_mgr(tmp_path)
    m.cfg.coder_backend = "native"
    m.core.llm = _FakeLlm()

    result = asyncio.run(m.code_with_review("做一个 demo 工具"))

    # 两个 agent 都通过 bot 的 LLMClient 跑过
    assert len(m.core.llm.calls) == 2, m.core.llm.calls
    assert m.core.llm.started is True
    # 工作区确实产出了 .py 候选
    cands = m._scan_candidates()
    assert any(p.suffix == ".py" for p in cands)
    # 仍然只进提案、不安装
    assert not (m.selfcode_dir / "demo_tool.py").exists()
    assert "等待 Owner" in result


def test_native_backend_revises_then_approves(tmp_path):
    """原生后端 REVISE 回灌：Critic 首轮 REVISE、Coder 重写、次轮 APPROVE。"""
    import anima.cognitive.selfcode as sc_mod

    class ReviseLlm:
        def __init__(self):
            self.n = 0
            self.started = False

        async def start(self):
            self.started = True

        async def close(self):
            pass

        async def chat(self, messages, **kw):
            self.n += 1
            sys_c = messages[0]["content"]
            if "审查员" in sys_c:
                return ('{"verdict":"REVISE","issues":["缺异常处理"],"suggest":"加 try"}'
                        if self.n == 2 else '{"verdict":"APPROVE","issues":[]}')
            return "```python\nTOOL_META = {\"name\":\"demo_tool\",\"description\":\"d\",\"parameters\":{},\"required\":[],\"min_level\":\"user\"}\nasync def run(core, **params):\n    return 'ok'\n```"

    m = _make_mgr(tmp_path)
    m.cfg.coder_backend = "native"
    m.core.llm = ReviseLlm()

    result = asyncio.run(m.code_with_review("做一个 demo 工具"))

    # Coder 被调 2 次（首写 + REVISE 重写），Critic 2 次
    assert m.core.llm.n == 4, m.core.llm.n
    assert "等待 Owner" in result


# ----------------------------------------------------------------------
# 回滚点 / 影响字段（支撑 WebUI 审批中心 · 阶段⑦）
# ----------------------------------------------------------------------

def test_proposal_includes_impact_and_rollback(tmp_path):
    """提案必须携带影响说明、回滚说明与候选代码预览（供 WebUI 审批中心展示）。"""
    m = _make_mgr(tmp_path)
    _write_valid_module(m.workspace)
    proposal, summary = asyncio.run(m._collect_proposal("做一个 demo 工具"))
    assert proposal["impact"]
    assert proposal["rollback"]
    assert "selfcode_backup" in proposal["rollback"]
    assert proposal["candidates"]
    cand = proposal["candidates"][0]
    assert cand["preview"]


def test_install_module_records_backup_path(tmp_path):
    """安装前对运行时工具目录做时间戳备份，并记录到 _last_install_backup（回滚点来源）。"""
    m = _make_mgr(tmp_path)
    m.selfcode_dir.mkdir(parents=True, exist_ok=True)
    src = _write_valid_module(tmp_path / "src")
    ok, msg = m._install_module(src)
    assert m._last_install_backup
    assert Path(m._last_install_backup).exists()
    # core.tools=None 时安装逻辑最终不注册，但备份点已生成（回滚点已就绪）
    assert ok is False


@pytest.mark.asyncio
async def test_approve_records_rollback_backup(tmp_path, monkeypatch):
    """审批安装成功后，实际备份路径被写入 task.proposal['rollback_backup'] 供 WebUI 展示。"""
    import anima.cognitive.selfcode as sc_mod

    async def fake_exec_claude(self, cmd):
        if ("Read" in cmd) and ("Bash" not in cmd):
            return 0, '{"verdict":"APPROVE","issues":[]}'
        _write_valid_module(self.workspace)
        return 0, "wrote"

    m = _make_mgr(tmp_path)
    m.core.tasks = TaskStore(PersistenceProvider(str(tmp_path)))
    backup_path = str(tmp_path / "selfcode_backup_xxx")

    def fake_install_ok(self, path):
        self._last_install_backup = backup_path
        return True, "test-installed"

    monkeypatch.setattr(sc_mod.SelfCodeManager, "_exec_claude", fake_exec_claude)
    monkeypatch.setattr(sc_mod.SelfCodeManager, "_install_module", fake_install_ok)
    task = await m.create_engineering_task("做一个 demo 工具", 42)
    done = await m.approve_engineering_task(task.id, 42)
    assert done.status == "completed"
    assert done.proposal.get("rollback_backup") == backup_path
