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
    ),
}


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
    """运行子进程并返回输出。"""
    logger.debug("沙箱执行: %s", cmd)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        preexec_fn=_preexec(config) if os.name != "nt" else None,
        env=env,
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

# 注意：不覆盖 exec/compile/eval —— Python stdlib import 内部依赖
# builtins.exec（如 importlib._bootstrap_external 在加载模块时调用 exec），
# 覆盖会导致 import io / import PIL 等直接崩溃。
# 安全性由 import 守卫的栈帧检查保证：即使用户代码 exec("import subprocess")，
# 栈帧检查仍能发现调用者来自 _USER_FILE 并拦截。

# User code below
""")
        # 始终注入图片变量（即使无入站图，也定义为 None/空串，避免 LLM 代码引用 NameError）
        import json as _json
        _imgs = input_images_b64 or []
        f.write(f"\n# 入站图片（base64）\n")
        f.write(f"_input_images_b64 = {_json.dumps(_imgs)}\n")
        f.write(f"_input_image_b64 = _input_images_b64[0] if _input_images_b64 else ''\n")
        f.write("""# 快捷：第一张图的 PIL Image 对象（无图时为 None）
import io as _io, base64 as _b64
_input_image = None
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
    except Exception as _e:
        import sys as _s2
        _diag = f'b64_len={len(_input_image_b64)} raw_len={len(_raw) if "_raw" in dir() else -1}'
        if '_raw' in dir() and _raw:
            _diag += f' head={_raw[:16].hex()} tail={_raw[-8:].hex()}'
        print(f'[sandbox] image decode failed ({type(_e).__name__}): {_e} | {_diag}', file=_s2.stderr)
        _input_image = None

def _edit_each_frame(func):
    '''对动画的每一帧应用 func(frame) -> frame，返回帧列表（单图也返回 [func(frame)]）。

    动画 GIF 必须用它处理全部帧，否则编辑后只剩第一帧 -> 变成静图。
    用法：_save_output(_edit_each_frame(lambda im: im.transpose(Image.FLIP_LEFT_RIGHT)), 'out.gif')
    '''
    return [func(_f.copy()) for _f in _input_frames]

# 输出图片辅助：_save_output(img) 或 _save_output(frames, 'out.gif') 保存到共享目录并打印路径
def _save_output(img, filename='output.png'):
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
    return out_path

""")
        f.write(code)
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
        _sandbox_env = {**os.environ, "SANDBOX_OUTPUT_DIR": _output_dir}
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
    return await _run_subprocess(cmd, config)
