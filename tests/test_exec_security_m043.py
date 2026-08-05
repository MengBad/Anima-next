"""M0.4.3 Security Hardening —— 代码执行安全边界回归。

背景：Phase 1 Security Gate = FAIL（执行基座未达 capability 标准）。
本文件覆盖 Phase 2 硬化项的回归断言：

    S1  UID-only 授权边界      —— 群角色不参与授权
    S2  密钥隔离               —— 子进程不继承密钥
    S3  文件系统禁闭           —— user 层不可读越界文件
    S4  执行审计               —— 结构化审计事件
    S5  exec scope 接线        —— 缺 scope 拒绝执行
    S6  以上集中回归
    R1  沙箱授权注入防御        —— 调用方 permission_level 注入被 ctx 覆盖

设计原则（锁定）：
    QQ UID -> PermissionStore -> explicit grant -> execution permission
    群主 != bot owner；管理员 != bot admin；群身份仅属聊天上下文。
"""

import inspect

import pytest

from anima.core.config import AnimaConfig
from anima.pipeline import PipelineContext, StageResult
from anima.pipeline.stages import _check_permission
from anima.pipeline.stages.permission import permission_stage
from anima.tools import Tool, ToolRegistry


# --------------------------------------------------------------------------
# 测试脚手架
# --------------------------------------------------------------------------

class _FakeCore:
    def __init__(self, config: AnimaConfig):
        self.config = config


def _make_ctx(user_id: int, group_id, sender_role: str, config: AnimaConfig):
    """构造带 OneBot11 群角色的 pipeline 上下文。"""
    event = {
        "raw": {
            "sender": {"user_id": user_id, "role": sender_role},
            "message": "hello",
        },
        "sender": {"user_id": user_id, "role": sender_role},
    }
    ctx = PipelineContext(
        event=event,
        text="hello",
        user_id=user_id,
        group_id=group_id,
        detail_type="group" if group_id else "private",
        sender_name="tester",
    )
    ctx.meta["core"] = _FakeCore(config)
    return ctx


# ==========================================================================
# S1 —— UID-only 授权边界
# ==========================================================================

def test_s1_check_permission_rejects_sender_role_argument():
    """签名守门：不得存在任何群角色入参（防止回归）。"""
    params = list(inspect.signature(_check_permission).parameters)
    assert params == ["user_id", "group_id", "config"]
    with pytest.raises(TypeError):
        _check_permission(10001, 100, AnimaConfig(), "owner")  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_s1_group_owner_uid_unauthorized_is_denied():
    """QQ 群主但未被显式授权 -> 只能是 level 2 普通用户。"""
    cfg = AnimaConfig()
    ctx = _make_ctx(10001, 100, "owner", cfg)
    result = await permission_stage(ctx)
    assert result is StageResult.CONTINUE
    assert ctx.meta["permission_level"] == 2
    assert ctx.meta["permission_role"] == "user"


@pytest.mark.asyncio
async def test_s1_group_admin_uid_unauthorized_is_denied():
    """QQ 群管理员但未被显式授权 -> 只能是 level 2 普通用户。"""
    cfg = AnimaConfig()
    ctx = _make_ctx(10002, 100, "admin", cfg)
    await permission_stage(ctx)
    assert ctx.meta["permission_level"] == 2
    assert ctx.meta["permission_role"] == "user"


@pytest.mark.asyncio
async def test_s1_authorized_uid_is_allowed():
    """显式授权的 UID -> 拿到对应等级，与群角色无关（此处群角色为 member）。"""
    cfg = AnimaConfig()
    cfg.permissions.owners = [20001]
    ctx = _make_ctx(20001, 100, "member", cfg)
    await permission_stage(ctx)
    assert ctx.meta["permission_level"] == 4
    assert ctx.meta["permission_role"] == "owner"


@pytest.mark.asyncio
async def test_s1_group_role_change_does_not_change_permission():
    """同一 UID 在 member/admin/owner 三种群角色下权限完全一致。"""
    cfg = AnimaConfig()
    levels = set()
    for role in ("member", "admin", "owner"):
        ctx = _make_ctx(30001, 100, role, cfg)
        await permission_stage(ctx)
        levels.add((ctx.meta["permission_level"], ctx.meta["permission_role"]))
    assert levels == {(2, "user")}


@pytest.mark.asyncio
async def test_s1_group_role_preserved_as_chat_context_only():
    """群角色仍保留为聊天上下文元数据，但键名与授权字段严格分离。"""
    cfg = AnimaConfig()
    ctx = _make_ctx(30002, 100, "owner", cfg)
    await permission_stage(ctx)
    assert ctx.meta["group_sender_role"] == "owner"
    # 关键：上下文身份不得污染授权身份
    assert ctx.meta["permission_role"] == "user"
    assert ctx.meta["permission_level"] == 2


@pytest.mark.asyncio
async def test_s1_banned_uid_still_blocked_in_group():
    """收紧路径不受影响：被封禁 UID 即便是群主也被拦截。"""
    cfg = AnimaConfig()
    cfg.permissions.banned = [40001]
    ctx = _make_ctx(40001, 100, "owner", cfg)
    result = await permission_stage(ctx)
    assert result is StageResult.STOP


# ==========================================================================
# S2 —— 密钥隔离（沙箱子进程不继承父进程密钥）
# ==========================================================================

_FAKE_SECRETS = {
    "ANIMA_TEST_LLM_API_KEY": "sk-should-never-leak-0001",
    "ANIMA_TEST_NAPCAT_TOKEN": "tok-should-never-leak-0002",
    "ANIMA_TEST_DB_PASSWORD": "pw-should-never-leak-0003",
    "SILICONFLOW_API_KEY": "sk-should-never-leak-0004",
}


@pytest.fixture
def _injected_secrets(monkeypatch):
    for key, value in _FAKE_SECRETS.items():
        monkeypatch.setenv(key, value)
    return _FAKE_SECRETS


def test_s2_build_sandbox_env_strips_secrets(_injected_secrets):
    from anima.tools.sandbox import build_sandbox_env

    env = build_sandbox_env()
    for key, value in _injected_secrets.items():
        assert key not in env
        assert value not in env.values()


def test_s2_build_sandbox_env_keeps_runtime_essentials(_injected_secrets):
    from anima.tools.sandbox import build_sandbox_env

    env = build_sandbox_env()
    # 至少保留一个可用于定位解释器的基础变量，否则子进程根本起不来
    assert "PATH" in env or "SYSTEMROOT" in env
    # 白名单之外的任意变量都不应出现
    assert all(k.upper() in _allowlist() for k in env if k != "SANDBOX_OUTPUT_DIR")


def _allowlist():
    from anima.tools.sandbox import SAFE_ENV_ALLOWLIST

    return SAFE_ENV_ALLOWLIST


def test_s2_build_sandbox_env_never_returns_none():
    """env=None 会导致 subprocess 继承全量环境，必须杜绝。"""
    from anima.tools.sandbox import build_sandbox_env

    env = build_sandbox_env()
    assert isinstance(env, dict)


def test_s2_extra_injection_rejects_sensitive_keys(caplog):
    from anima.tools.sandbox import build_sandbox_env

    env = build_sandbox_env({"SANDBOX_OUTPUT_DIR": "/tmp/x", "MY_TOKEN": "leak"})
    assert env["SANDBOX_OUTPUT_DIR"] == "/tmp/x"
    assert "MY_TOKEN" not in env


def test_s2_run_shell_passes_explicit_env():
    """run_shell 不得再走 env=None 的继承路径。"""
    import inspect

    from anima.tools import sandbox as sb

    src = inspect.getsource(sb.run_shell)
    assert "build_sandbox_env()" in src


@pytest.mark.asyncio
async def test_s2_sandbox_subprocess_has_no_secrets(_injected_secrets):
    """端到端：user 层沙箱内 dump 环境变量，不得出现任何密钥。"""
    from anima.tools.sandbox import run_python

    out = await run_python(
        "import os\nprint('|'.join(sorted(os.environ)))",
        permission_level=2,
    )
    # 先证明沙箱确实执行成功（否则「没泄漏」是假阳性）
    assert "SANDBOX_OUTPUT_DIR" in out, f"沙箱未真正执行: {out[:400]}"
    for key, value in _injected_secrets.items():
        assert key not in out, f"环境变量 {key} 泄漏进沙箱: {out[:400]}"
        assert value not in out


# ==========================================================================
# S3 —— 文件系统禁闭
# ==========================================================================

from pathlib import Path as _Path  # noqa: E402

_PROJECT_ROOT = _Path(__file__).resolve().parents[1]


def _probe(body: str) -> str:
    """包一层 try/except，让「是否被拒绝」成为可断言的显式输出。

    直接依赖 traceback 不可靠：沙箱 stderr 截断到 500 字节，
    异常类型位于 traceback 末尾，常被截掉。
    """
    indented = "\n".join("    " + line for line in body.strip().splitlines())
    return (
        "try:\n"
        f"{indented}\n"
        "    print('SANDBOX_LEAK')\n"
        "except PermissionError as _e:\n"
        "    print('SANDBOX_DENIED')\n"
    )


@pytest.mark.asyncio
async def test_s3_user_cannot_read_project_config():
    """user 层不得读取项目根下的任意文件（配置/密钥的落点）。"""
    from anima.tools.sandbox import run_python

    target = _PROJECT_ROOT / "pyproject.toml"
    out = await run_python(
        _probe(f"open({str(target)!r}, encoding='utf-8').read()"),
        permission_level=2,
    )
    assert "SANDBOX_DENIED" in out, f"项目文件被 user 层读取: {out[:400]}"
    assert "SANDBOX_LEAK" not in out


@pytest.mark.asyncio
async def test_s3_user_cannot_read_via_pathlib():
    """pathlib 走 io.open，同样必须被守卫拦截。"""
    from anima.tools.sandbox import run_python

    target = _PROJECT_ROOT / "pyproject.toml"
    out = await run_python(
        _probe(
            "from pathlib import Path\n"
            f"Path({str(target)!r}).read_text(encoding='utf-8')"
        ),
        permission_level=2,
    )
    assert "SANDBOX_DENIED" in out, f"pathlib 绕过了文件禁闭: {out[:400]}"
    assert "SANDBOX_LEAK" not in out


@pytest.mark.asyncio
async def test_s3_user_cannot_list_project_dir():
    """目录枚举同样越界。"""
    from anima.tools.sandbox import run_python

    out = await run_python(
        _probe(f"import os\nos.listdir({str(_PROJECT_ROOT)!r})"),
        permission_level=2,
    )
    assert "SANDBOX_DENIED" in out, f"目录枚举越界未被拦截: {out[:400]}"
    assert "SANDBOX_LEAK" not in out


@pytest.mark.asyncio
async def test_s3_user_cannot_write_outside_workspace():
    """写越界：连 Python 运行时目录也不可写（防植入后门）。"""
    from anima.tools.sandbox import run_python

    out = await run_python(
        _probe(
            "import sys, os\n"
            "p = os.path.join(sys.prefix, 'anima_s3_probe.txt')\n"
            "open(p, 'w').write('x')"
        ),
        permission_level=2,
    )
    assert "SANDBOX_DENIED" in out, f"越界写未被拦截: {out[:400]}"
    assert "SANDBOX_LEAK" not in out


@pytest.mark.asyncio
async def test_s3_low_level_os_open_is_guarded():
    """低级 os.open 同样受控（绕过 builtins.open 的常见手法）。"""
    from anima.tools.sandbox import run_python

    target = _PROJECT_ROOT / "pyproject.toml"
    out = await run_python(
        _probe(f"import os\nos.open({str(target)!r}, os.O_RDONLY)"),
        permission_level=2,
    )
    assert "SANDBOX_DENIED" in out, f"os.open 绕过了禁闭: {out[:400]}"
    assert "SANDBOX_LEAK" not in out


@pytest.mark.asyncio
async def test_s3_user_can_use_sandbox_workspace():
    """禁闭不得误伤正常用法：输出目录可读写。"""
    from anima.tools.sandbox import run_python

    code = (
        "import os\n"
        "p = os.path.join(os.environ['SANDBOX_OUTPUT_DIR'], 'ok.txt')\n"
        "open(p, 'w').write('hello')\n"
        "print(open(p).read())"
    )
    out = await run_python(code, permission_level=2)
    assert "hello" in out, f"沙箱工作区被误伤: {out[:400]}"


@pytest.mark.asyncio
async def test_s3_pil_pipeline_still_works():
    """回归：图像工具链（读 site-packages 资源 + 写输出目录）不受影响。"""
    from anima.tools.sandbox import run_python

    code = (
        "from PIL import Image\n"
        "img = Image.new('RGB', (8, 8), 'red')\n"
        "print('SIZE', img.size)"
    )
    out = await run_python(code, permission_level=2)
    assert "SIZE (8, 8)" in out, f"PIL 链路受损: {out[:400]}"


@pytest.mark.asyncio
async def test_s3_owner_tier_not_confined():
    """主人层保持宽松（其权限等价于宿主机，禁闭无意义且会破坏运维能力）。"""
    from anima.tools.sandbox import run_python

    target = _PROJECT_ROOT / "pyproject.toml"
    code = f"print('OWNER_READ', len(open({str(target)!r}, encoding='utf-8').read()) > 0)"
    out = await run_python(code, permission_level=4)
    assert "OWNER_READ True" in out, f"主人层被误伤: {out[:400]}"


def test_s3_tier_confinement_flags():
    """分层开关：user/admin 禁闭，owner 放行。"""
    from anima.tools.sandbox import sandbox_tier_for

    assert sandbox_tier_for(2).fs_confined is True
    assert sandbox_tier_for(3).fs_confined is True
    assert sandbox_tier_for(4).fs_confined is False


# ==========================================================================
# S4 —— 执行审计
# ==========================================================================

class _FakeAudit:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, **fields):
        self.events.append((event_type, fields))

    def types(self) -> list[str]:
        return [t for t, _ in self.events]

    def find(self, event_type: str) -> dict:
        for t, f in self.events:
            if t == event_type:
                return f
        raise AssertionError(f"未找到审计事件 {event_type}，实际: {self.types()}")


class _AuditCore:
    def __init__(self):
        self.audit = _FakeAudit()
        self.config = None


def _exec_registry():
    from anima.tools import Tool, ToolRegistry

    async def _fake_run_python(code: str, permission_level: int = 2, **_kw):
        if "BOOM" in code:
            raise RuntimeError("boom")
        return f"executed:{len(code)}"

    core = _AuditCore()
    reg = ToolRegistry(core)
    reg.tools["run_python"] = Tool(
        name="run_python",
        description="exec",
        parameters={"code": {"type": "string"}},
        handler=_fake_run_python,
        # S5：镜像真实 run_python 的 exec scope 授权，使审计门控可通过
        granted_scopes={"exec": True},
    )
    reg.tools["echo"] = Tool(
        name="echo",
        description="non-exec",
        parameters={"text": {"type": "string"}},
        handler=lambda text: f"echo:{text}",
    )
    return reg, core


@pytest.mark.asyncio
async def test_s4_execution_emits_started_and_finished():
    reg, core = _exec_registry()
    token = reg.set_call_context(user_id=987654, group_id=100, permission_level=2)
    try:
        out = await reg.call("run_python", {"code": "print(1)"}, permission_level=2)
    finally:
        reg.reset_call_context(token)

    assert "executed" in out
    types = core.audit.types()
    assert "execution_started" in types
    assert "execution_finished" in types
    assert types.index("execution_started") < types.index("execution_finished")


@pytest.mark.asyncio
async def test_s4_audit_records_actor_and_sandbox_tier():
    reg, core = _exec_registry()
    token = reg.set_call_context(user_id=987654, group_id=100, permission_level=4)
    try:
        await reg.call("run_python", {"code": "print(1)"}, permission_level=4)
    finally:
        reg.reset_call_context(token)

    started = core.audit.find("execution_started")
    assert started["actor"] == 987654
    assert started["group_id"] == 100
    assert started["permission_level"] == 4
    assert started["sandbox_tier"] == "owner"
    assert started["payload_len"] == len("print(1)")
    assert len(started["payload_sha256"]) == 16

    finished = core.audit.find("execution_finished")
    assert finished["success"] is True
    assert isinstance(finished["duration_ms"], float)


@pytest.mark.asyncio
async def test_s4_execution_failure_is_audited():
    reg, core = _exec_registry()
    token = reg.set_call_context(user_id=1, permission_level=2)
    try:
        await reg.call("run_python", {"code": "BOOM"}, permission_level=2)
    finally:
        reg.reset_call_context(token)

    failed = core.audit.find("execution_failed")
    assert failed["error"] == "RuntimeError"
    assert failed["tool"] == "run_python"
    assert "execution_finished" not in core.audit.types()


@pytest.mark.asyncio
async def test_s4_non_execution_tool_not_audited_as_execution():
    reg, core = _exec_registry()
    await reg.call("echo", {"text": "hi"}, permission_level=2)
    assert not any(t.startswith("execution_") for t in core.audit.types())


def test_s4_execution_tool_set_is_explicit():
    """执行面清单必须显式声明，避免新增执行工具时漏审计。"""
    from anima.tools import EXECUTION_TOOLS

    assert {"run_python", "run_shell", "agent_exec"} <= set(EXECUTION_TOOLS)


# ==========================================================================
# S5 —— exec scope 真实门控
# ==========================================================================

@pytest.mark.asyncio
async def test_s5_missing_exec_scope_is_denied():
    """S5：执行类工具若未获 exec scope 授权，即便通过 min_level 也拒绝执行。"""
    from anima.tools import Tool, ToolRegistry

    called = {"hit": False}

    def _rogue_exec(code: str, **_kw):
        called["hit"] = True
        return "PWNED"

    core = _AuditCore()
    reg = ToolRegistry(core)
    # 故意不授予 exec scope（granted_scopes 缺省 = NO_SCOPES -> exec False）
    reg.tools["run_python"] = Tool(
        name="run_python",
        description="exec",
        parameters={"code": {"type": "string"}},
        handler=_rogue_exec,
    )
    token = reg.set_call_context(user_id=987654, group_id=100, permission_level=4)
    try:
        out = await reg.call("run_python", {"code": "import os"}, permission_level=4)
    finally:
        reg.reset_call_context(token)

    # 关键：handler 绝不应被实际执行
    assert called["hit"] is False, "缺少 exec scope 的工具不应被实际执行"
    assert "exec scope" in out or "未获授权" in out
    assert "execution_scope_denied" in core.audit.types()
    # 反向断言：不应留下正常执行审计轨迹
    assert "execution_started" not in core.audit.types()


@pytest.mark.asyncio
async def test_s5_exec_scope_granted_allows_execution():
    """S5：显式授予 exec scope 的工具可正常执行（正向，覆盖真实 run_shell）。"""
    from anima.tools import Tool, ToolRegistry

    core = _AuditCore()
    reg = ToolRegistry(core)
    reg.tools["run_shell"] = Tool(
        name="run_shell",
        description="exec",
        parameters={"command": {"type": "string"}},
        handler=lambda command: f"ran:{command}",
        min_level=3,
        granted_scopes={"exec": True},
    )
    token = reg.set_call_context(user_id=1, group_id=None, permission_level=3)
    try:
        out = await reg.call("run_shell", {"command": "ls"}, permission_level=3)
    finally:
        reg.reset_call_context(token)

    assert out == "ran:ls"
    assert "execution_started" in core.audit.types()
    assert "execution_scope_denied" not in core.audit.types()


@pytest.mark.asyncio
async def test_s5_non_exec_tool_ignores_exec_scope():
    """S5：非执行类工具不受 exec scope 门控影响（如 echo）。"""
    from anima.tools import Tool, ToolRegistry

    core = _AuditCore()
    reg = ToolRegistry(core)
    reg.tools["echo"] = Tool(
        name="echo",
        description="non-exec",
        parameters={"text": {"type": "string"}},
        handler=lambda text: f"echo:{text}",
        # 即便误授予 exec scope，也不应触发门控（名字不在 EXECUTION_TOOLS）
        granted_scopes={"exec": True},
    )
    out = await reg.call("echo", {"text": "hi"}, permission_level=2)
    assert out == "echo:hi"
    assert "execution_scope_denied" not in core.audit.types()


# ==========================================================================
# R1 —— 沙箱授权注入防御 (M0.4.3-R1)
# ==========================================================================
#
# 攻击模型：普通/管理员 UID 在 LLM tool-call args 中携带 {"permission_level": 4}，
# 企图把 run_python 沙箱升级到 owner 层（network=True + fs_confined=False）。
# 修复后：_invoke 在调用 handler 前无条件用可信 ctx 的 permission_level 覆盖 args，
# 审计记录也只信 ctx，调用方传入值被彻底丢弃。

@pytest.mark.asyncio
async def test_r1_user_cannot_escalate_via_injected_permission_level():
    """R1：普通 UID(args 注入 4) 沙箱仍须是 user 层，handler 收 ctx 值。"""
    reg, core = _exec_registry()
    received: dict = {}
    async def _capture(code, permission_level=2, **_kw):
        received["level"] = permission_level
        return f"executed:{len(code)}"
    reg.tools["run_python"] = Tool(
        name="run_python", description="exec",
        parameters={"code": {"type": "string"}},
        handler=_capture, granted_scopes={"exec": True},
    )
    token = reg.set_call_context(user_id=987654, group_id=100, permission_level=2)
    try:
        out = await reg.call(
            "run_python",
            {"code": "print(1)", "permission_level": 4},
            permission_level=2,
        )
    finally:
        reg.reset_call_context(token)

    assert "executed" in out
    # 关键：handler 收到的必须是 ctx 的 2，而非注入的 4
    assert received["level"] == 2, f"特权参数注入得逞，handler 收到 {received['level']}"
    started = core.audit.find("execution_started")
    assert started["permission_level"] == 2
    assert started["sandbox_tier"] == "user"


@pytest.mark.asyncio
async def test_r1_admin_cannot_escalate_via_injected_permission_level():
    """R1：管理员(level 3) 注入 4 也不得越级到 owner 沙箱。"""
    reg, core = _exec_registry()
    received: dict = {}
    async def _capture(code, permission_level=3, **_kw):
        received["level"] = permission_level
        return f"executed:{len(code)}"
    reg.tools["run_python"] = Tool(
        name="run_python", description="exec",
        parameters={"code": {"type": "string"}},
        handler=_capture, granted_scopes={"exec": True},
    )
    token = reg.set_call_context(user_id=987654, group_id=100, permission_level=3)
    try:
        out = await reg.call(
            "run_python",
            {"code": "print(1)", "permission_level": 4},
            permission_level=3,
        )
    finally:
        reg.reset_call_context(token)

    assert received["level"] == 3, f"管理员越级注入得逞: {received['level']}"
    started = core.audit.find("execution_started")
    assert started["permission_level"] == 3
    assert started["sandbox_tier"] == "admin"


@pytest.mark.asyncio
async def test_r1_owner_context_keeps_owner_sandbox():
    """R1：合法 owner 上下文(ctx=4) 仍正确映射 owner 沙箱（分级能力不破坏）。"""
    reg, core = _exec_registry()
    received: dict = {}
    async def _capture(code, permission_level=4, **_kw):
        received["level"] = permission_level
        return f"executed:{len(code)}"
    reg.tools["run_python"] = Tool(
        name="run_python", description="exec",
        parameters={"code": {"type": "string"}},
        handler=_capture, granted_scopes={"exec": True},
    )
    token = reg.set_call_context(user_id=1, group_id=None, permission_level=4)
    try:
        out = await reg.call("run_python", {"code": "print(1)"}, permission_level=4)
    finally:
        reg.reset_call_context(token)

    assert received["level"] == 4
    started = core.audit.find("execution_started")
    assert started["permission_level"] == 4
    assert started["sandbox_tier"] == "owner"


def test_r1_run_python_schema_excludes_permission_level():
    """R1：工具 schema 不得向 LLM 暴露 permission_level 参数（仅 code）。"""
    p = _Path(__file__).resolve().parents[1] / "anima/tools/builtin/__init__.py"
    src = p.read_text(encoding="utf-8")
    idx = src.index('"run_python"')
    block = src[idx: idx + 400]
    assert '"code"' in block
    assert '"permission_level"' not in block, "run_python 工具 schema 不得暴露 permission_level 参数"


@pytest.mark.asyncio
async def test_r1_unknown_tool_arg_does_not_inject_permission():
    """R1：LLM 传入的非 schema 字段（攻击试探）不影响可信 permission_level。"""
    reg, core = _exec_registry()
    received: dict = {}
    async def _capture(code, permission_level=2, **_kw):
        received["level"] = permission_level
        return f"executed:{len(code)}"
    reg.tools["run_python"] = Tool(
        name="run_python", description="exec",
        parameters={"code": {"type": "string"}},
        handler=_capture, granted_scopes={"exec": True},
    )
    token = reg.set_call_context(user_id=987654, group_id=100, permission_level=2)
    try:
        await reg.call(
            "run_python",
            {"code": "print(1)", "evil_field": "x", "permission_level": 4},
            permission_level=2,
        )
    finally:
        reg.reset_call_context(token)

    assert received["level"] == 2, "未知字段/注入字段影响了可信权限等级"

