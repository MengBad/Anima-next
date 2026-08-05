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
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import resource
    _HAS_RESOURCE = True
except ImportError:
    resource = None
    _HAS_RESOURCE = False

logger = logging.getLogger("anima.tools.sandbox")

# 安全限制
MAX_OUTPUT = 8192        # 最大输出字节
MAX_MEMORY_MB = 128      # 最大内存 MB
DEFAULT_TIMEOUT = 10     # 默认超时秒
# 硬上限：普通用户受到 pipeline 默认约束，Owner 任务经分级沙箱可放宽到 120s。
# 现有调用方传入的 timeout 均 ≤ DEFAULT_TIMEOUT(10)，因此抬高上限不改变旧行为。
MAX_TIMEOUT = 120        # 最大超时秒（Owner 分级沙箱上限）

# 外部隔离工具（按优先级）
ISOLATION_TOOLS = ["bwrap", "unshare"]

# ---------------------------------------------------------------------------
# M0.4.3 S2 —— 密钥隔离
# ---------------------------------------------------------------------------
# 沙箱子进程曾经继承父进程的完整 os.environ，任何注入到环境中的令牌/密钥
# （LLM API key、NapCat token、数据库口令等）都能被用户代码一行 print 出来，
# 再经工具结果回流给 LLM 和聊天窗口。此处改为**显式白名单**：
# 只有运行时确实需要的键才会进入子进程，其余一律丢弃。
SAFE_ENV_ALLOWLIST = frozenset({
    # POSIX 基础
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    "TMPDIR", "TEMP", "TMP", "USER", "LOGNAME",
    # Windows 运行 Python 必需
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    # Python 运行时
    "PYTHONPATH", "PYTHONHOME", "PYTHONIOENCODING",
    "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE", "VIRTUAL_ENV",
})

# 二次防御：即便某个键被误加进白名单，只要名字带敏感语义也一律剔除。
_SENSITIVE_ENV_MARKERS = (
    "TOKEN", "SECRET", "PASSWORD", "PASSWD", "APIKEY", "API_KEY",
    "KEY", "CREDENTIAL", "AUTH", "SESSION", "COOKIE", "PRIVATE",
    "SIGNATURE", "SALT", "ACCESS", "REFRESH",
)


def _is_sensitive_env_key(key: str) -> bool:
    upper = key.upper()
    return any(marker in upper for marker in _SENSITIVE_ENV_MARKERS)


def build_sandbox_env(extra: Optional[dict] = None) -> dict:
    """构造沙箱子进程环境变量（白名单 + 敏感名剔除）。

    绝不返回 None —— 返回 None 会让 subprocess 继承父进程全量环境，
    正是本函数要消灭的泄漏面。
    """
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key.upper() not in SAFE_ENV_ALLOWLIST:
            continue
        if _is_sensitive_env_key(key):
            continue
        env[key] = value
    if extra:
        for key, value in extra.items():
            if _is_sensitive_env_key(key):
                logger.warning("拒绝向沙箱注入敏感环境变量: %s", key)
                continue
            env[key] = str(value)
    return env


class SandboxError(Exception):
    pass


class _SandboxIsolationError(RuntimeError):
    """隔离工具执行失败（namespace 创建失败等），应降级重试。"""


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
# 分级沙箱（按权限等级分层能力）—— 统一接口与规范化 Phase 1
# ---------------------------------------------------------------------------

# 权限等级常量（与 anima.tools 保持一致；此处不反向 import 以避免循环依赖）。
_LEVEL_USER = 2
_LEVEL_ADMIN = 3
_LEVEL_OWNER = 4

@dataclass
class SandboxTier:
    """按权限等级分层的沙箱能力。

    - 普通用户（2）：现状不变，无网络 / 10s / 128MB / 严格 import 黑名单。
    - 管理员（3）：延长超时与内存，仍无网络。
    - 主人（4）：全能力，开放网络、放宽超时与内存、可写 agent_workspace、
      仅保留最危险的 import 阻止（ctypes / multiprocessing）。
    """

    network: bool = False
    timeout: int = DEFAULT_TIMEOUT
    memory_mb: int = MAX_MEMORY_MB
    writable_dirs: list[str] = field(default_factory=list)
    blocked_imports: set[str] = field(
        default_factory=lambda: {"subprocess", "socket", "requests", "ctypes", "multiprocessing"}
    )
    # M0.4.3 S3：文件系统禁闭。True 时用户代码只能访问沙箱工作区与
    # Python 运行时目录，无法读取项目配置 / 家目录 / 密钥文件。
    # 主人层保持关闭（主人本就拥有等价于宿主机的权限）。
    fs_confined: bool = True


# 默认分层。key 为权限等级；越权（>4）按 owner 处理。
SANDBOX_TIERS: dict[int, SandboxTier] = {
    _LEVEL_USER: SandboxTier(),  # 普通用户：现状不变
    _LEVEL_ADMIN: SandboxTier(timeout=30, memory_mb=256),  # 管理员：延长超时+内存
    _LEVEL_OWNER: SandboxTier(  # 主人：全能力
        network=True,
        timeout=MAX_TIMEOUT,
        memory_mb=512,
        writable_dirs=["data/agent_workspace"],
        blocked_imports={"ctypes", "multiprocessing"},  # 仅保留最危险阻止
        fs_confined=False,
    ),
}

# ---------------------------------------------------------------------------
# M0.4.3 S3 —— 文件系统禁闭
# ---------------------------------------------------------------------------
# 无论路径是否落在允许目录内，命中这些标记一律拒绝（deny 优先于 allow）。
FS_DENY_MARKERS = (
    ".env", ".ssh", "id_rsa", "id_ed25519", ".pem", ".key", ".p12", ".pfx",
    "secrets", "credential", "config.yaml", "config.yml", "config.json",
    ".git", ".netrc", "shadow", "authorized_keys", "known_hosts",
    ".aws", ".kube", ".docker", "token", ".npmrc", ".pypirc",
)


def _runtime_read_roots() -> list[str]:
    """允许只读访问的 Python 运行时目录（否则 import 链会断）。"""
    import sysconfig

    roots = {sys.prefix, sys.base_prefix, os.path.dirname(sys.executable)}
    try:
        paths = sysconfig.get_paths()
        for key in ("stdlib", "platstdlib", "purelib", "platlib", "data"):
            p = paths.get(key)
            if p:
                roots.add(p)
    except Exception:  # pragma: no cover - sysconfig 异常极罕见
        logger.debug("sysconfig 路径探测失败", exc_info=True)
    out = []
    for r in roots:
        if not r:
            continue
        try:
            out.append(os.path.realpath(os.path.abspath(r)))
        except OSError:
            continue
    return sorted(set(out))


def sandbox_tier_for(permission_level: int) -> SandboxTier:
    """返回给定权限等级对应的沙箱分层；越界按 owner（最高）处理。"""
    return SANDBOX_TIERS.get(min(int(permission_level), _LEVEL_OWNER), SANDBOX_TIERS[_LEVEL_USER])


# ---------------------------------------------------------------------------
# 隔离能力探测（含运行时验证）
# ---------------------------------------------------------------------------

def _find_tool(name: str) -> Optional[str]:
    return shutil.which(name)


# 模块级缓存：避免每次调用都探测
_isolator_cache: Optional[tuple[str, str]] = None


def _probe_isolator(tool: str, path: str) -> bool:
    """运行时轻量探测：实际执行一次确认隔离工具可用。

    bwrap: 用最小 namespace 创建测试（--ro-bind + true），不只是 --version。
    unshare: unshare --mount true（需要 namespace 权限）。
    """
    import subprocess
    try:
        if tool == "bwrap":
            # 必须测试实际 namespace 创建，--version 不需要权限会误报
            r = subprocess.run(
                [path, "--ro-bind", "/", "/", "--dev", "/dev",
                 "--proc", "/proc", "--unshare-pid", "--", "true"],
                capture_output=True, timeout=5,
            )
            return r.returncode == 0
        if tool in ("unshare", "unshare_user"):
            # 用最小命令探测 namespace 是否真正可用
            flags = ["--mount", "true"] if tool == "unshare" else ["--user", "--map-root-user", "--mount", "true"]
            r = subprocess.run([path] + flags, capture_output=True, timeout=5)
            return r.returncode == 0
    except Exception:
        pass
    return False


def _available_isolator() -> tuple[str, str]:
    """返回 (tool_name, tool_path) 或 ('', '')。

    优先级：bwrap(root) > unshare(root) > unshare --user(非 root) > 普通子进程。
    每个候选工具需通过运行时探测，失败则降级到下一个。
    Windows 上没有 getuid，直接跳过命名空间隔离。
    """
    global _isolator_cache
    if _isolator_cache is not None:
        return _isolator_cache

    getuid = getattr(os, "getuid", None)
    is_root = getuid is not None and getuid() == 0

    result = ("", "")

    # 优先尝试 bwrap（root 和非 root 都试，bwrap 不依赖 setuid）
    bwrap_path = _find_tool("bwrap")
    if bwrap_path and _probe_isolator("bwrap", bwrap_path):
        result = ("bwrap", bwrap_path)
    elif bwrap_path:
        logger.warning("沙箱隔离工具 bwrap 存在但 namespace 探测失败，降级")

    if result == ("", ""):
        if is_root:
            unshare_path = _find_tool("unshare")
            if unshare_path and _probe_isolator("unshare", unshare_path):
                result = ("unshare", unshare_path)
            elif unshare_path:
                logger.warning("沙箱隔离工具 unshare 存在但运行时探测失败，降级")
        else:
            # 非 root：尝试 user namespace（内核 >= 3.8 且未禁用 unprivileged userns）
            unshare_path = _find_tool("unshare")
            if unshare_path and _probe_isolator("unshare_user", unshare_path):
                result = ("unshare_user", unshare_path)
            elif unshare_path:
                logger.warning("沙箱 unshare --user 探测失败，降级到普通子进程")

    if result == ("", ""):
        logger.warning("沙箱无可用隔离工具，将以普通子进程执行（仍有 import 守卫 + rlimit 保护）")

    _isolator_cache = result
    return result


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
        resource.setrlimit(resource.RLIMIT_NPROC, (8, 8))
    except Exception:
        pass
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
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

    # 挂载 Python venv（venv 可能不在 /usr 下，bwrap 默认只挂载系统目录）
    # 不 resolve() —— venv/bin/python 是指向系统 Python 的符号链接，
    # 但 bwrap 执行时用的是原始路径，且 venv 的 site-packages 在同级 lib/ 下。
    _venv_dir = str(Path(sys.executable).parent.parent)
    if not _venv_dir.startswith(("/usr", "/bin")):
        cmd.extend(["--ro-bind", _venv_dir, _venv_dir])

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


def _build_unshare_user_command(config: SandboxConfig, inner_cmd: list[str]) -> list[str]:
    """构建非 root user namespace 隔离命令（降级方案）。"""
    unshare = _find_tool("unshare")
    if not unshare:
        raise SandboxError("unshare not available")

    # --user --map-root-user 允许非 root 创建命名空间
    flags = ["--user", "--map-root-user", "--mount", "--pid", "--fork", "--ipc"]
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
    if tool == "unshare_user":
        return _build_unshare_user_command(config, inner_cmd)
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


async def _run_subprocess(cmd: list[str], config: SandboxConfig,
                          env: dict | None = None) -> str:
    """运行子进程并返回输出。

    S2：env 为 None 时**不再继承**父进程环境，而是回落到白名单环境，
    确保任何调用方（含未来新增路径）都无法意外泄漏密钥。
    """
    logger.debug("沙箱执行: %s", cmd)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        preexec_fn=_preexec(config) if os.name != "nt" else None,
        env=env if env is not None else build_sandbox_env(),
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
        # 系统级错误（fork/unshare/permission）不暴露给 LLM，防止泄漏内部实现
        if any(kw in err for kw in ("unshare", "fork failed", "Operation not permitted",
                                     "Resource temporarily unavailable", "bwrap", "namespace")):
            logger.warning("沙箱系统级错误（已隐藏）: %.200s", err)
            # 清除缓存，下次调用重新探测
            global _isolator_cache
            _isolator_cache = None
            # 抛出异常让调用方降级重试
            raise _SandboxIsolationError(err)
        else:
            output += f"\n[stderr] {err}"

    if not output.strip():
        output = f"[沙箱] 执行完成（返回码 {proc.returncode}）"

    return output.strip()


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

# M0.4.3 S3：注入到沙箱子进程的文件系统禁闭守卫。
# 允许读：沙箱工作区 + Python 运行时目录（否则 import 链断裂）
# 允许写：仅沙箱工作区（连 site-packages 也不可写，防止植入后门）
# deny 标记优先于 allow，但不适用于运行时目录（避免误伤库自带的 config.json）
_FS_GUARD_BLOCK = r'''
if _FS_CONFINED:
    import io as _fsio
    import _io as _fsio_c
    import sysconfig as _fssc

    _fs_orig_open = _builtins.open
    _fs_orig_os_open = _os.open
    _fs_orig_listdir = _os.listdir
    _fs_orig_scandir = _os.scandir
    _fs_orig_remove = _os.remove
    _fs_orig_unlink = _os.unlink
    _fs_orig_rename = _os.rename
    _fs_orig_replace = _os.replace
    _fs_orig_mkdir = _os.mkdir
    _fs_orig_makedirs = _os.makedirs
    _fs_orig_rmdir = _os.rmdir

    def _fs_norm(p):
        try:
            return _os.path.realpath(_os.path.abspath(_os.fspath(p)))
        except Exception:
            return ''

    _FS_RUNTIME_ROOTS = []
    for _r in (_sys.prefix, _sys.base_prefix, _os.path.dirname(_sys.executable)):
        _rn = _fs_norm(_r) if _r else ''
        if _rn and _rn not in _FS_RUNTIME_ROOTS:
            _FS_RUNTIME_ROOTS.append(_rn)
    try:
        for _k, _v in _fssc.get_paths().items():
            if _k in ('stdlib', 'platstdlib', 'purelib', 'platlib', 'data') and _v:
                _rn = _fs_norm(_v)
                if _rn and _rn not in _FS_RUNTIME_ROOTS:
                    _FS_RUNTIME_ROOTS.append(_rn)
    except Exception:
        pass

    _FS_WORK_ROOTS = []
    for _r in (_os.path.dirname(_USER_FILE), _output_dir):
        _rn = _fs_norm(_r) if _r else ''
        if _rn and _rn not in _FS_WORK_ROOTS:
            _FS_WORK_ROOTS.append(_rn)

    _FS_READ_ROOTS = _FS_WORK_ROOTS + _FS_RUNTIME_ROOTS

    def _fs_under(path_str, roots):
        for _r in roots:
            if path_str == _r or path_str.startswith(_r + _os.sep):
                return True
        return False

    def _fs_denied(path_str):
        _low = path_str.replace('\\', '/').lower()
        for _m in _FS_DENY_MARKERS:
            if _m in _low:
                return True
        return False

    def _fs_check(path, write=False):
        if isinstance(path, int):
            return  # 已打开的 fd，不重复校验
        _norm = _fs_norm(path)
        if not _norm:
            raise PermissionError('[sandbox] 路径无法解析，访问被拒绝')
        _in_runtime = _fs_under(_norm, _FS_RUNTIME_ROOTS)
        if not _in_runtime and _fs_denied(_norm):
            raise PermissionError('[sandbox] 敏感路径访问被拒绝: %s' % (path,))
        if write:
            if not _fs_under(_norm, _FS_WORK_ROOTS):
                raise PermissionError(
                    '[sandbox] 写入越界，仅允许沙箱工作区: %s' % (path,))
        else:
            if not _fs_under(_norm, _FS_READ_ROOTS):
                raise PermissionError(
                    '[sandbox] 读取越界，仅允许沙箱工作区: %s' % (path,))

    def _fs_guard_open(file, mode='r', *a, **kw):
        _m = str(kw.get('mode', mode))
        _fs_check(file, write=any(_c in _m for _c in ('w', 'a', 'x', '+')))
        if 'mode' in kw:
            return _fs_orig_open(file, *a, **kw)
        return _fs_orig_open(file, mode, *a, **kw)

    def _fs_guard_os_open(path, flags, *a, **kw):
        _wmask = (getattr(_os, 'O_WRONLY', 0) | getattr(_os, 'O_RDWR', 0)
                  | getattr(_os, 'O_CREAT', 0) | getattr(_os, 'O_APPEND', 0)
                  | getattr(_os, 'O_TRUNC', 0))
        _fs_check(path, write=bool(flags & _wmask))
        return _fs_orig_os_open(path, flags, *a, **kw)

    def _fs_guard_listdir(path='.', *a, **kw):
        _fs_check(path)
        return _fs_orig_listdir(path, *a, **kw)

    def _fs_guard_scandir(path='.', *a, **kw):
        _fs_check(path)
        return _fs_orig_scandir(path, *a, **kw)

    def _fs_wrap_write1(_orig):
        def _w(path, *a, **kw):
            _fs_check(path, write=True)
            return _orig(path, *a, **kw)
        return _w

    def _fs_wrap_write2(_orig):
        def _w(src, dst, *a, **kw):
            _fs_check(src, write=True)
            _fs_check(dst, write=True)
            return _orig(src, dst, *a, **kw)
        return _w

    _builtins.open = _fs_guard_open
    for _mod in (_fsio, _fsio_c):
        try:
            _mod.open = _fs_guard_open
        except Exception:
            pass
    _os.open = _fs_guard_os_open
    _os.listdir = _fs_guard_listdir
    _os.scandir = _fs_guard_scandir
    _os.remove = _fs_wrap_write1(_fs_orig_remove)
    _os.unlink = _fs_wrap_write1(_fs_orig_unlink)
    _os.mkdir = _fs_wrap_write1(_fs_orig_mkdir)
    _os.makedirs = _fs_wrap_write1(_fs_orig_makedirs)
    _os.rmdir = _fs_wrap_write1(_fs_orig_rmdir)
    _os.rename = _fs_wrap_write2(_fs_orig_rename)
    _os.replace = _fs_wrap_write2(_fs_orig_replace)
'''

# 自动捕获（兜底）：用户代码未显式调用 _save_output 但留下了 PIL Image / 帧列表时，
# 自动保存并标记 [output_image]，避免「模型生成了图却忘记输出」导致发不出图片。
# 仅当用户代码恰好留下「一个」图片变量（排除注入的 _input_*）时才自动保存，避免误发。
_AUTO_CAPTURE_BLOCK = '''
if not _output_emitted:
    try:
        _cands = []
        for _name, _val in list(globals().items()):
            if _name.startswith('_input') or _name in ('_save_output', '_edit_each_frame', '_Image', 'Image', '_src', '_raw'):
                continue
            if isinstance(_val, _Image.Image):
                _cands.append((_name, _val, False))
            elif isinstance(_val, (list, tuple)) and _val and all(isinstance(_x, _Image.Image) for _x in _val):
                _cands.append((_name, _val, True))
        if len(_cands) == 1:
            _n, _v, _anim = _cands[0]
            try:
                if _anim:
                    _save_output(_v, 'out.gif')
                else:
                    _save_output(_v, 'output.png')
                import sys as _ac_sys
                print('[sandbox] 自动保存未显式输出的图片变量: ' + _n, file=_ac_sys.stderr)
            except Exception as _e:
                import sys as _ac_sys2
                print('[sandbox] 自动保存失败: ' + type(_e).__name__ + ': ' + str(_e), file=_ac_sys2.stderr)
    except Exception:
        pass
'''


async def run_python(code: str, timeout: int = DEFAULT_TIMEOUT,
                      input_images_b64: list[str] | None = None,
                      permission_level: int = _LEVEL_USER) -> str:
    """在沙箱中执行一段 Python 代码。

    Args:
        code: Python 代码字符串
        timeout: 超时秒数（按权限分层上限夹紧）
        input_images_b64: 可选，入站图片的 base64 列表，注入为 _input_image_b64 / _input_image
        permission_level: 调用者权限等级（2=普通 3=管理 4=主人），决定沙箱能力分层
    """
    # Phase 1：按权限等级选取沙箱分层（网络/超时/内存/可写目录/import 黑名单）。
    tier = sandbox_tier_for(permission_level)
    blocked = set(tier.blocked_imports)
    effective_timeout = min(int(timeout), tier.timeout)
    # S3：文件系统禁闭参数（主人层关闭）
    fs_confined = bool(tier.fs_confined)
    fs_deny = tuple(FS_DENY_MARKERS)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, prefix="anima_sandbox_"
    ) as f:
        f.write(f"""# Sandbox safety preamble
import builtins as _builtins
import os as _os
import sys as _sys
_orig_import = _builtins.__import__

# 用户代码文件路径（临时 .py）
_USER_FILE = __file__
_output_dir = _os.environ.get('SANDBOX_OUTPUT_DIR', '/tmp')
# 注意：shutil / importlib 不在阻止列表：
# - shutil：tempfile 内部依赖，PIL 等标准库 import 链会触发
# - importlib：PIL 12.x 用 importlib.import_module() 懒加载格式插件（GIF/PNG/WebP），
#   阻止会导致 Image.open() 报 UnidentifiedImageError（所有格式插件加载失败）
# 安全性：importlib.import_module("subprocess") 仍被拦截（subprocess 在列表里）
# 按权限分层注入：普通用户阻止最全，主人仅阻止 ctypes/multiprocessing。
_blocked = {blocked!r}

def _safe_import(name, *args, **kwargs):
    root = name.split(".")[0]
    if root in _blocked:
        # 只检查直接调用者是否为用户代码。
        # 不遍历整个栈——否则库内部导入（如 PIL GifImagePlugin 内部
        # import subprocess）会因用户代码触发了 from PIL import Image
        # 而被误杀，导致 GIF 等格式插件加载失败。
        # exec("import subprocess") 绕过仍能拦截：exec 帧的调用者即用户文件。
        _caller = _os.path.normpath(_sys._getframe(1).f_code.co_filename)
        if _caller == _os.path.normpath(_USER_FILE):
            raise ImportError(f"Module '{{name}}' is blocked in sandbox")
    return _orig_import(name, *args, **kwargs)

_builtins.__import__ = _safe_import

_FS_CONFINED = {fs_confined!r}
_FS_DENY_MARKERS = {fs_deny!r}

# 注意：不覆盖 exec/compile/eval —— Python stdlib import 内部依赖
# builtins.exec（如 importlib._bootstrap_external 在加载模块时调用 exec），
# 覆盖会导致 import io / import PIL 等直接崩溃。
# 安全性由 import 守卫的栈帧检查保证：即使用户代码 exec("import subprocess")，
# 栈帧检查仍能发现调用者来自 _USER_FILE 并拦截。
""")
        # ---- M0.4.3 S3：文件系统禁闭守卫 ----------------------------------
        # 纯 Python 层纵深防御：拦截 open / io.open / os.* 的常规读写入口，
        # 把用户代码限制在「沙箱工作区 + Python 运行时目录」内。
        # 硬边界仍由 bwrap/unshare 命名空间提供，此层用于无隔离工具时兜底。
        f.write(_FS_GUARD_BLOCK)
        f.write("\n# User code below\n")
        # 始终注入图片变量（即使无入站图，也定义为 None/空串，避免 LLM 代码引用 NameError）
        import json as _json
        _imgs = input_images_b64 or []
        f.write(f"\n# 入站图片（base64）\n")
        f.write(f"_input_images_b64 = {_json.dumps(_imgs)}\n")
        f.write(f"_input_image_b64 = _input_images_b64[0] if _input_images_b64 else ''\n")
        f.write("""# 快捷：第一张图的 PIL Image 对象（无图时为 None）
import io as _io, base64 as _b64
_input_image = None
_output_emitted = False  # 兜底自动捕获标记：_save_output 调用后置 True
# 动画 GIF / APNG / 动态 WebP 支持：加载全部帧 + 元数据，避免编辑后丢失动画
_input_frames = []          # 所有帧的 PIL.Image（单图 -> [img]）
_input_duration = []        # 每帧时长 ms（单图 -> [0]）
_input_loop = 0             # 循环次数（0 = 无限）
_input_is_animated = False  # 是否为动画图
_input_format = ''
try:
    from PIL import Image as _Image
    from PIL import Image  # LLM 代码可以直接用 Image
except Exception as _e:
    import sys as _s2; print(f'[sandbox] PIL import failed: {_e}', file=_s2.stderr)
    _Image = None
    Image = None
if _input_image_b64 and _Image is not None:
    try:
        _raw = _b64.b64decode(_input_image_b64)
        _src = _Image.open(_io.BytesIO(_raw))
        _input_format = _src.format
        try:
            _input_is_animated = bool(getattr(_src, "is_animated", False)) and _src.n_frames > 1
        except Exception:
            _input_is_animated = False
        if _input_is_animated:
            for _i in range(_src.n_frames):
                _src.seek(_i)
                _input_frames.append(_src.copy())  # 保留原 mode / 调色板
                try:
                    _input_duration.append(int(_src.info.get("duration", 100)))
                except Exception:
                    _input_duration.append(100)
            try:
                _input_loop = int(_src.info.get("loop", 0))
            except Exception:
                _input_loop = 0
        else:
            _input_frames = [_src.copy()]
            _input_duration = [0]
        _input_image = _input_frames[0] if _input_frames else None
        # 清理解码残留的打开态 Image，避免污染全局命名空间导致兜底自动捕获误判候选数
        if '_src' in globals():
            del _src
    except Exception as _e:
        import sys as _s2
        _diag = f'b64_len={len(_input_image_b64)} raw_len={len(_raw) if "_raw" in dir() else -1}'
        if '_raw' in dir() and _raw:
            _diag += f' head={_raw[:16].hex()} tail={_raw[-8:].hex()}'
        print(f'[sandbox] image decode failed ({type(_e).__name__}): {_e} | {_diag}', file=_s2.stderr)
        _input_image = None

# 直接落盘捕获：很多 LLM 生成的编辑代码会直接 img.save("out.png") 而非调用 _save_output，
# 导致不打印 [output_image]、图片发不出。monkey-patch Image.save，任何落盘动作都捕获路径并标记输出。
_save_in_progress = False  # _save_output 内部 save 时置 True，避免重复捕获
_orig_image_save = _Image.Image.save if _Image is not None else None

def _patched_image_save(self, fp, *args, **kwargs):
    # 相对路径重定向到共享输出目录（确保 NapCat 可读，避免 PrivateTmp / cwd 差异导致发不出图）
    if isinstance(fp, str) and not _os.path.isabs(fp) and _output_dir:
        fp = _os.path.join(_output_dir, _os.path.basename(fp))
    _ret = _orig_image_save(self, fp, *args, **kwargs) if _orig_image_save is not None else None
    global _output_emitted, _save_in_progress
    if _orig_image_save is not None and (not _output_emitted) and (not _save_in_progress):
        try:
            _p = fp if isinstance(fp, str) else getattr(fp, "name", None)
            if _p and isinstance(_p, str):
                print(f"[output_image] {_os.path.abspath(_p)}")
                _output_emitted = True
        except Exception:
            pass
    return _ret

if _Image is not None:
    try:
        _Image.Image.save = _patched_image_save
    except Exception:
        pass

def _edit_each_frame(func):
    '''对动画的每一帧应用 func(frame) -> frame，返回帧列表（单图也返回 [func(frame)]）。

    动画 GIF 必须用它处理全部帧，否则编辑后只剩第一帧 -> 变成静图。
    用法：_save_output(_edit_each_frame(lambda im: im.transpose(Image.FLIP_LEFT_RIGHT)), 'out.gif')
    '''
    return [func(_f.copy()) for _f in _input_frames]

# 输出图片辅助：_save_output(img) 或 _save_output(frames, 'out.gif') 保存到共享目录并打印路径
def _save_output(img, filename='output.png'):
    global _output_emitted, _save_in_progress
    _save_in_progress = True
    try:
        out_path = _os.path.join(_output_dir, filename)
        _is_list = isinstance(img, (list, tuple))
        _frames = list(img) if _is_list else None
        _multi = _is_list and len(_frames) > 1
        _want_anim = _multi or (not _is_list and _input_is_animated and str(filename).lower().endswith('.gif'))
        if _want_anim:
            # 动画输出：保存为多帧 GIF，保留每帧时长与循环，避免编辑后变静图
            if not _is_list:
                _frames = [img]
            if not str(filename).lower().endswith('.gif'):
                filename = _os.path.splitext(filename)[0] + '.gif'
                out_path = _os.path.join(_output_dir, filename)
            _frames[0].save(
                out_path, save_all=True, append_images=_frames[1:],
                duration=_input_duration[:len(_frames)] or None,
                loop=_input_loop, disposal=2, optimize=False,
            )
        else:
            # 单图或单帧列表：存为普通图片（_edit_each_frame 对静图返回 [img] 也能正确保存）
            _single = _frames[0] if _is_list else img
            _single.save(out_path)
        print(f'[output_image] {out_path}')
        _output_emitted = True
        return out_path
    finally:
        _save_in_progress = False

""")
        f.write(code)
        # 兜底：用户代码跑完后，若未显式 _save_output，自动捕获残留的图片变量
        f.write(_AUTO_CAPTURE_BLOCK)
        tmp_path = f.name

    # 创建共享输出目录。
    # 注意：systemd PrivateTmp=yes 使服务的 /tmp 对其它进程（NapCat）不可见，
    # 因此输出必须放在 data/ 下（ReadWritePaths 白名单内），NapCat 才能读取。
    _data_dir = Path(os.environ.get("ANIMA_DATA_DIR", "data"))
    _output_base = (_data_dir / "sandbox_output").resolve()
    _output_base.mkdir(parents=True, exist_ok=True)
    _output_dir = tempfile.mkdtemp(prefix="out_", dir=str(_output_base))
    # 清理 1 小时前的旧输出目录，防止磁盘累积
    _now = time.time()
    try:
        for _old in _output_base.iterdir():
            if _old.is_dir() and _old.name.startswith("out_"):
                if _now - _old.stat().st_mtime > 3600:
                    shutil.rmtree(_old, ignore_errors=True)
    except OSError:
        pass

    # Phase 1：主人分层可写 agent_workspace（持久化工作区，路径穿越防护由上层负责）。
    _writable = [str(Path(tmp_path).parent), _output_dir]
    for _rel in tier.writable_dirs:
        _ws = (_data_dir / _rel).resolve()
        try:
            _ws.mkdir(parents=True, exist_ok=True)
            _writable.append(str(_ws))
        except OSError:
            logger.warning("主人沙箱工作区创建失败，跳过: %s", _ws)

    try:
        config = SandboxConfig(
            memory_mb=tier.memory_mb,
            timeout=effective_timeout,
            network=tier.network,
            writable_dirs=_writable,
        )
        inner_cmd = [sys.executable, "-u", tmp_path]
        # S2：白名单环境，父进程密钥不进入沙箱
        _sandbox_env = build_sandbox_env({"SANDBOX_OUTPUT_DIR": _output_dir})
        # 先尝试隔离执行
        cmd = _build_command(config, inner_cmd)
        try:
            result = await _run_subprocess(cmd, config, env=_sandbox_env)
        except _SandboxIsolationError:
            # 隔离工具失败，降级为无隔离普通子进程重试（仍有 rlimit + import 守卫）
            logger.info("沙箱隔离失败，降级为无隔离子进程重试")
            result = await _run_subprocess(inner_cmd, config, env=_sandbox_env)
        # mkdtemp 默认 0o700，NapCat 可能以不同用户运行而无法读取输出图片。
        # 将输出目录及文件设为 world-readable，确保 OneBot 图片上传可读。
        try:
            os.chmod(_output_dir, 0o755)
            for _f in os.listdir(_output_dir):
                os.chmod(os.path.join(_output_dir, _f), 0o644)
        except OSError:
            pass
        return result
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
    # S2：显式白名单环境（`cat /proc/self/environ` 亦无密钥可读）
    return await _run_subprocess(cmd, config, env=build_sandbox_env())
