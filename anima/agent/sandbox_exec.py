"""P3 · 受限执行环境（对齐 Memoh 的容器隔离思路）。

## 为什么这么做

`self_code` 当初的死结：它要生成联网代码，而沙箱 `--unshare-net` 禁止一切网络，
静态审查又禁止 `aiohttp`/`urllib`/`httpx` —— 两堵墙夹死。生产 5 次调用 0 成功。

## Memoh 的做法（2026-10-31 读源码确认）

`internal/container/containerd/network.go` 用 CNI 给每个容器分配独立 IP；
`internal/acl/` 是**消息触发 ACL**（chat.trigger），与网络无关。
⇒ **Memoh 也不做域名白名单**。它的隔离靠容器边界：独立文件系统 + 独立进程空间。

本模块对齐这一点：**网络放开，但让它看不到不该看的东西**。

## 三道硬边界

1. **文件系统**：`--ro-bind / /` 只读，且用 `--tmpfs` 把敏感路径替换成空目录
   （`.secrets` / `data` / `config.yaml` 全部不可见 —— 实测 `ls` 为空）
2. **进程空间**：`--unshare-pid --die-with-parent`，超时能杀干净
3. **资源**：子进程 `RLIMIT_AS` 限内存 + 总超时，防拖垮 3.9G 小主机

## 与 Multitenant 的边界

刻意**不做**审批流、配额、租户隔离 —— 这是 owner 单人的服务器。
保留的只是资源/密钥/进程三条底线。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("anima.agent.sandbox_exec")

#: 每次执行的内存上限（MB）。生产 3.9G，主进程 + NapCat 已占约 1G。
DEFAULT_MEMORY_MB = 512
#: 单次执行超时（秒）。装包类任务 300s 够用。
DEFAULT_TIMEOUT_SEC = 300.0
#: 输出截断，防撑爆上下文。
MAX_OUTPUT_CHARS = 6000
#: 工作区保留时长（秒），过期由清理函数回收。
WORKSPACE_TTL_SEC = 86400.0

#: 必须对沙箱隐藏的路径（相对项目根）。命中任意一条即 mask。
HIDDEN_PATHS = (
    ".secrets",
    "config.yaml",
    "data",
    "venv/.env",
)


class SandboxError(RuntimeError):
    """沙箱不可用或执行失败。"""


@dataclass
class ExecResult:
    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    duration_sec: float
    workspace: str
    truncated: bool = False

    def render(self) -> str:
        """给人看的结果摘要。失败串必须命中工具失败前缀。"""
        if self.ok:
            body = (self.stdout or "").strip() or "（无输出）"
            return f"执行成功（{self.duration_sec:.1f}s）\n{body}"
        detail = ((self.stderr or "").strip() or (self.stdout or "").strip())[:800]
        return f"错误: 执行失败（exit={self.exit_code}）\n{detail}"


def workspace_root(project_root: str | Path) -> Path:
    return Path(project_root) / "data" / "workspace"


def new_workspace(project_root: str | Path, task_id: str = "") -> Path:
    """为一次执行创建独立工作区。

    2026-10-31: 建好后 chmod 0777。沙箱内以 `--ro-bind / /` 挂载时
    目录属主仍是宿主机用户（anima），而 bwrap 内进程的 uid 映射可能不同，
    不加权限会 `OSError: [Errno 30] Read-only file system`（生产验收实测）。
    """
    tid = (task_id or uuid.uuid4().hex[:10]).strip()
    ws = workspace_root(project_root) / tid
    ws.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(ws, 0o777)
    except Exception:
        logger.debug("工作区 chmod 失败（已隔离）: %s", ws, exc_info=True)
    return ws


def _bwrap_available() -> bool:
    return shutil.which("bwrap") is not None


def build_command(
    project_root: str | Path,
    workspace: str | Path,
    inner: list[str],
) -> list[str]:
    """构造 bwrap 命令。纯函数，可单测。

    关键：`--tmpfs` 把敏感路径替换成空目录 —— 沙箱内 `ls` 得到空，
    但宿主机上原文件毫发无损（tmpfs 是覆盖挂载，不删除）。
    """
    root = Path(project_root).resolve()
    ws = Path(workspace).resolve()

    cmd: list[str] = [
        "bwrap",
        "--ro-bind", "/", "/",          # 全盘只读
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        # 2026-10-31: **不要 mask /run**。
        # /etc/resolv.conf 是 -> /run/systemd/resolve/stub-resolv.conf 的软链，
        # 把 /run 换成空 tmpfs ⇒ 软链悬空 ⇒ 沙箱内 DNS 全废：
        #   URLError: [Errno -3] Temporary failure in name resolution
        # 表现为「联网功能存在但什么都连不上」，比直接禁网更难排查。
        "--unshare-pid",                # 独立 PID 命名空间
        "--die-with-parent",            # 父进程死则子进程死，防泄漏
        "--new-session",
    ]

    # mask 敏感路径：文件用 /dev/null，目录用空 tmpfs。
    #
    # 2026-10-31: **顺序很重要** —— workspace 位于 data 之下，
    # 所以 `--bind ws` 必须放在 `--tmpfs data` **之后**，否则被覆盖成空，
    # 沙箱里连自己的 main.py 都打不开（生产验收实测 exit=2）。
    for rel in HIDDEN_PATHS:
        target = root / rel
        if target.is_dir():
            cmd.extend(["--tmpfs", str(target)])
        elif target.exists():
            cmd.extend(["--ro-bind", "/dev/null", str(target)])

    # 工作区最后挂回 —— 覆盖在 masked 的 data 之上，只有它可写
    cmd.extend(["--bind", str(ws), str(ws)])

    # pip 用官方源：清华源对本机 403（2026-10-31 实测），必须覆盖
    cmd.extend([
        "--setenv", "PIP_INDEX_URL", "https://pypi.org/simple/",
        "--setenv", "PIP_NO_CACHE_DIR", "1",
        "--setenv", "HOME", str(ws),
        "--setenv", "TMPDIR", "/tmp",
    ])

    cmd.append("--")
    cmd.extend(inner)
    return cmd


async def execute(
    project_root: str | Path,
    inner: list[str],
    *,
    task_id: str = "",
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    memory_mb: int = DEFAULT_MEMORY_MB,
) -> ExecResult:
    """在 bwrap 沙箱里执行命令。"""
    if not _bwrap_available():
        raise SandboxError("bwrap 不可用")

    ws = new_workspace(project_root, task_id)
    cmd = build_command(project_root, ws, inner)
    budget = max(1.0, min(float(timeout_sec), 900.0))
    mem_bytes = max(64, int(memory_mb)) * 1024 * 1024

    def _limit_memory():
        """子进程内存硬上限。RLIMIT_AS 超限时 malloc 直接失败。"""
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        except Exception:
            pass

    t0 = time.time()
    # 2026-10-31: **必须设 cwd=ws**。不设的话子进程继承宿主机当前目录
    # （/opt/anima），而它在沙箱内是 --ro-bind 只读 ⇒ 任何相对路径写入都报
    #   OSError: [Errno 30] Read-only file system
    # 实测：cwd 不设 exit=1，设 cwd=ws exit=0。
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(ws),
        preexec_fn=_limit_memory if os.name != "nt" else None,
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=budget)
        code = proc.returncode or 0
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        await proc.wait()
        return ExecResult(
            ok=False, exit_code=-9, stdout="",
            stderr=f"执行超过 {int(budget)}s 被终止",
            duration_sec=time.time() - t0, workspace=str(ws),
        )

    out = (out_b or b"").decode("utf-8", "replace")
    err = (err_b or b"").decode("utf-8", "replace")
    truncated = False
    if len(out) > MAX_OUTPUT_CHARS:
        out, truncated = out[:MAX_OUTPUT_CHARS], True
    if len(err) > MAX_OUTPUT_CHARS:
        err, truncated = err[:MAX_OUTPUT_CHARS], True

    return ExecResult(
        ok=(code == 0), exit_code=code, stdout=out, stderr=err,
        duration_sec=time.time() - t0, workspace=str(ws), truncated=truncated,
    )


async def run_python(
    project_root: str | Path,
    code: str,
    *,
    task_id: str = "",
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    memory_mb: int = DEFAULT_MEMORY_MB,
    python_bin: str = "",
) -> ExecResult:
    """在工作区里跑一段 Python。"""
    ws = new_workspace(project_root, task_id)
    script = ws / "main.py"
    script.write_text(code, encoding="utf-8")
    py = python_bin or os.environ.get("ANIMA_SANDBOX_PYTHON", "") or "python3"
    return await execute(
        project_root, [py, str(script)],
        task_id=path_name(ws), timeout_sec=timeout_sec, memory_mb=memory_mb,
    )


def path_name(p: Path) -> str:
    return Path(p).name


def prune_workspaces(project_root: str | Path, ttl_sec: float = WORKSPACE_TTL_SEC) -> int:
    """清理过期工作区，返回删除数量。"""
    root = workspace_root(project_root)
    if not root.exists():
        return 0
    now = time.time()
    removed = 0
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            if now - child.stat().st_mtime > ttl_sec:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        except Exception:
            continue
    return removed
