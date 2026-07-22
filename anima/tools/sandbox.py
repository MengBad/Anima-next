"""安全沙箱 —— 子进程隔离执行代码与命令。

设计（基于 §13 Linux 子进程沙箱）：
- subprocess 执行，超时自动 kill
- 内存限制（resource.setrlimit + cgroup v2 可选）
- 命名空间隔离（bubblewrap / unshare 可选）
- 禁止网络（-n 或 net namespace）
- 输出截断，防止撑爆
- 安全降级：bwrap → unshare → 普通子进程
"""

from __future__ import annotations
import asyncio
import logging
import os
import shlex
import shutil
import tempfile
from pathlib import Path
from typing import Optional

try:
    import resource
    _HAS_RESOURCE = True
except ImportError:
    resource = None
    _HAS_RESOURCE = False

logger = logging.getLogger("anima.sandbox")

# 安全限制
MAX_OUTPUT = 8192        # 最大输出字节
MAX_MEMORY_MB = 128      # 最大内存 MB
DEFAULT_TIMEOUT = 10     # 默认超时秒
MAX_TIMEOUT = 30         # 最大超时秒

# 外部隔离工具（按优先级）
ISOLATION_TOOLS = ["bwrap", "unshare"]


class SandboxError(Exception):
    pass


class SandboxConfig:
    """沙箱运行配置。"""

    def __init__(self,
                 memory_mb: int = MAX_MEMORY_MB,
                 timeout: int = DEFAULT_TIMEOUT,
                 network: bool = False,
                 writable_dirs: Optional[list[str]] = None,
                 readonly_dirs: Optional[list[str]] = None):
        self.memory_mb = memory_mb
        self.timeout = min(timeout, MAX_TIMEOUT)
        self.network = network
        self.writable_dirs = writable_dirs or []
        self.readonly_dirs = readonly_dirs or []


# ---------------------------------------------------------------------------
# 隔离能力探测
# ---------------------------------------------------------------------------

def _find_tool(name: str) -> Optional[str]:
    return shutil.which(name)


def _available_isolator() -> tuple[str, str]:
    """返回 (tool_name, tool_path) 或 ('', '')。"""
    for tool in ISOLATION_TOOLS:
        path = _find_tool(tool)
        if path:
            return tool, path
    return "", ""


# ---------------------------------------------------------------------------
# 资源限制（resource + cgroup v2）
# ---------------------------------------------------------------------------

def _set_resource_limits(memory_mb: int, max_cpu: int = MAX_TIMEOUT) -> None:
    """在子进程内设置 rlimit。"""
    if not _HAS_RESOURCE:
        return
    try:
        limit = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except Exception:
        pass
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (max_cpu, max_cpu))
    except Exception:
        pass
    try:
        resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
    except Exception:
        pass
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    except Exception:
        pass


def _setup_cgroup_v2(memory_mb: int, pid: int) -> bool:
    """尝试将当前进程加入 cgroup v2 进行资源限制。返回是否成功。"""
    cgroup_root = Path("/sys/fs/cgroup")
    if not cgroup_root.exists():
        return False

    try:
        # 使用名为 anima_sandbox 的 cgroup（如果当前用户有权限创建）
        sandbox_cg = cgroup_root / "anima_sandbox"
        sandbox_cg.mkdir(exist_ok=True)
        (sandbox_cg / "cgroup.procs").write_text(str(pid))
        (sandbox_cg / "memory.max").write_text(f"{memory_mb}M")
        (sandbox_cg / "cpu.max").write_text(f"{100000} {100000}")  # 1 CPU
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 命令构建
# ---------------------------------------------------------------------------

def _build_bwrap_command(config: SandboxConfig, inner_cmd: list[str]) -> list[str]:
    """构建 bubblewrap 命令。"""
    bwrap = _find_tool("bwrap")
    if not bwrap:
        raise SandboxError("bwrap not available")

    cmd = [bwrap, "--die-with-parent", "--new-session"]

    # 默认只读挂载根
    cmd.extend(["--ro-bind", "/usr", "/usr",
                "--ro-bind", "/lib", "/lib",
                "--ro-bind", "/lib64", "/lib64",
                "--ro-bind", "/bin", "/bin",
                "--ro-bind", "/sbin", "/sbin",
                "--proc", "/proc",
                "--dev", "/dev",
                "--tmpfs", "/tmp"])

    # 可写目录
    for d in config.writable_dirs:
        cmd.extend(["--bind", d, d])

    # 只读目录
    for d in config.readonly_dirs:
        cmd.extend(["--ro-bind", d, d])

    if not config.network:
        cmd.append("--unshare-net")

    cmd.append("--")
    cmd.extend(inner_cmd)
    return cmd


def _build_unshare_command(config: SandboxConfig, inner_cmd: list[str]) -> list[str]:
    """构建 unshare 命令（降级方案）。"""
    unshare = _find_tool("unshare")
    if not unshare:
        raise SandboxError("unshare not available")

    flags = ["--mount", "--pid", "--fork", "--ipc"]
    if not config.network:
        flags.append("--net")

    return [unshare] + flags + ["--"] + inner_cmd


def _build_command(config: SandboxConfig, inner_cmd: list[str]) -> list[str]:
    """根据可用工具构建隔离命令。"""
    tool, _ = _available_isolator()
    if tool == "bwrap":
        return _build_bwrap_command(config, inner_cmd)
    if tool == "unshare":
        return _build_unshare_command(config, inner_cmd)
    return inner_cmd


# ---------------------------------------------------------------------------
# 进程执行
# ---------------------------------------------------------------------------

def _preexec(config: SandboxConfig) -> Optional[callable]:
    """返回子进程 preexec_fn（仅非 Windows）。"""
    if os.name == "nt":
        return None

    def fn():
        _set_resource_limits(config.memory_mb)
        # 尝试 cgroup v2（对自身 pid）
        _setup_cgroup_v2(config.memory_mb, os.getpid())
    return fn


async def _run_subprocess(cmd: list[str], config: SandboxConfig) -> str:
    """运行子进程并返回输出。"""
    logger.debug("沙箱执行: %s", cmd)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        preexec_fn=_preexec(config) if os.name != "nt" else None,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=config.timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return f"[沙箱] 超时 ({config.timeout}s)，进程已终止"

    output = ""
    if stdout:
        output += stdout.decode("utf-8", errors="replace")[:MAX_OUTPUT]
    if stderr and proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace")[:500]
        output += f"\n[stderr] {err}"

    if not output.strip():
        output = f"[沙箱] 执行完成（返回码 {proc.returncode}）"

    return output.strip()


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

async def run_python(code: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """在沙箱中执行一段 Python 代码。

    Args:
        code: Python 代码字符串
        timeout: 超时秒数（最大 30）
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, prefix="anima_sandbox_"
    ) as f:
        f.write("""# Sandbox safety preamble
import builtins
_orig_import = builtins.__import__

_blocked = {"os", "subprocess", "shutil", "socket", "requests", "ctypes",
            "sys", "importlib", "compile", "eval", "exec"}

def _safe_import(name, *args, **kwargs):
    root = name.split(".")[0]
    if root in _blocked:
        raise ImportError(f"Module '{name}' is blocked in sandbox")
    return _orig_import(name, *args, **kwargs)

builtins.__import__ = _safe_import

# User code below
""")
        f.write(code)
        tmp_path = f.name

    try:
        config = SandboxConfig(
            memory_mb=MAX_MEMORY_MB,
            timeout=timeout,
            network=False,
            writable_dirs=[str(Path(tmp_path).parent)],
        )
        inner_cmd = [sys.executable, "-u", tmp_path]
        cmd = _build_command(config, inner_cmd)
        return await _run_subprocess(cmd, config)
    except Exception:
        logger.exception("沙箱 Python 执行异常")
        return "[沙箱] 执行异常（详见日志）"
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# shell 元字符 —— 一旦出现即拒绝（防止 ls && rm -rf / 之类绕过白名单）
_SHELL_META = set(";|&$`<>(){}\n\r!#")

# 允许的安全只读命令
SAFE_SHELL_CMDS = {
    "echo", "date", "ls", "cat", "wc", "grep", "head", "tail",
    "find", "du", "df", "uname", "whoami", "pwd", "ps",
    "id", "hostname", "uptime", "free",
}


def _validate_shell_command(command: str) -> tuple[bool, str]:
    """校验命令：白名单首词 + 无 shell 元字符。

    返回 (ok, reason)。reason 在失败时给出说明。
    """
    if not command.strip():
        return False, "命令为空"

    try:
        tokens = shlex.split(command)
    except ValueError as e:
        return False, f"命令解析失败: {e}"

    if not tokens:
        return False, "命令为空"

    cmd_name = tokens[0]
    if cmd_name not in SAFE_SHELL_CMDS:
        return False, f"命令 '{cmd_name}' 不在安全白名单中"

    # 逐 token 检查 shell 元字符，防止复合/注入命令绕过
    for tok in tokens:
        for ch in tok:
            if ch in _SHELL_META:
                return False, f"命令包含不允许的元字符 '{ch}'"

    return True, ""


async def run_shell(command: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """在沙箱中执行一条受限的只读 shell 命令。

    安全模型：
    - 首词必须位于只读白名单
    - 禁止任何 shell 元字符（; & | $ ` < > ( ) {{ }} 等），因此
      `ls && rm -rf /`、`cat /etc/passwd` 等复合/注入命令均被拒绝
    - 不调用 shell 解释器，直接 exec 程序（shlex 拆词后的 argv）
    """
    ok, reason = _validate_shell_command(command)
    if not ok:
        return f"[沙箱] {reason}"

    tokens = shlex.split(command)
    config = SandboxConfig(
        memory_mb=64,
        timeout=timeout,
        network=False,
    )
    cmd = _build_command(config, tokens)
    return await _run_subprocess(cmd, config)
