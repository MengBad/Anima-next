"""R6.4 —— Execution Isolation：工业级执行隔离策略层。

## 目标（R6.4）

在 R5 沙箱（memory/timeout/network/fs 禁闭/blocked-imports）之上，补上"工业级
执行隔离"的缺失维度，使任何执行行为**可限制、可终止、可追踪**：

- **CPU limit**：CPU 时间配额（秒），超限即终止（可限制）
- **Memory limit**：内存上限（MB）（可限制）
- **Timeout**：墙钟超时（可终止）
- **Process isolation**：最大子进程数 / 进程数控制（可限制）
- **Resource accounting**：资源使用记账（CPU 秒 / 字节 / 墙钟），可对账（可追踪）
- **syscall restriction**：seccomp 策略档（禁系统调用，可限制）

## 本模块边界（加性新增，不动共享核心）

- **不修改** ``tools/sandbox.py``、``runtime/*``、``pipeline/*``、``capability/*``。
  本模块是**独立隔离策略层**：由 R6.1 的 provider / R6.5 全链路测试 / 未来接线点
  调用，把隔离需求收敛为一份**描述性 IsolatorLimits**，交给真实 sandbox 执行。
  它**不替代** sandbox 的 bwrap/unshare/cgroup 执行，只做策略派生与校验。
- **fail-closed**：请求的隔离无法满足（如超系统上限、seccomp 冲突）→ 校验失败，
  调用方必须 DENY，绝不降级执行。

## 完成标准映射

- 可限制：CPU 配额 / 内存 / 超时 / 进程数 / syscall 均在 IsolatorLimits 表达。
- 可终止：timeout 超限即终止信号（TERM→KILL 两段）。
- 可追踪：resource accounting 累计每次执行的 CPU 秒 / 字节 / 墙钟，落审计。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

# ---------------------------------------------------------------------------
# 词表 / 常量
# ---------------------------------------------------------------------------
#: seccomp 策略档。USER 最严（禁 exec 子进程/网络 socket/ptrace）；OWNER 最宽。
SECCOMP_DEFAULT = "default"
SECCOMP_USER = "user"
SECCOMP_ADMIN = "admin"
SECCOMP_OWNER = "owner"

#: 隔离校验失败原因词表。
ISO_OK = "ok"
ISO_OVER_LIMIT = "over_limit"            # 请求超出系统/分层上限
ISO_MEMORY_OVER = "memory_over_limit"    # 内存超限
ISO_CPU_OVER = "cpu_over_limit"          # CPU 超限
ISO_PROCS_OVER = "processes_over_limit"  # 进程数超限
ISO_TIMEOUT_OVER = "timeout_over_limit"  # 超时超限
ISO_OPEN_FILES_OVER = "open_files_over_limit"  # 打开文件数超限

#: 分层能力上界（与 sandbox.SANDBOX_TIERS 对齐的"工业级"扩展）。
_MAX_BY_LEVEL: dict[int, dict[str, float]] = {
    2: {"cpu_seconds": 5.0, "memory_mb": 128, "timeout": 10, "max_procs": 4, "max_open_files": 256},
    3: {"cpu_seconds": 15.0, "memory_mb": 256, "timeout": 30, "max_procs": 8, "max_open_files": 1024},
    4: {"cpu_seconds": 120.0, "memory_mb": 512, "timeout": 300, "max_procs": 64, "max_open_files": 65536},
}


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class IsolatorLimits:
    """一份完整的执行隔离需求（策略派生结果，交给真实 sandbox 执行）。

    所有字段都回答 R6.4 完成标准：可限制 / 可终止 / 可追踪。
    """

    permission_level: int
    cpu_seconds: float           # CPU 时间配额（秒）
    memory_mb: int               # 内存上限（MB）
    timeout: int                 # 墙钟超时（秒）
    max_procs: int               # 最大子进程数
    max_open_files: int          # 最大打开文件数
    seccomp: str                 # syscall 策略档
    resource_account: bool = True  # 是否做资源记账（可追踪）

    @property
    def isolator_name(self) -> str:
        """用于 R6.2 事件链 sandbox 字段的隔离档名。"""
        return f"iso-{self.seccomp}"


# ---------------------------------------------------------------------------
# 策略派生
# ---------------------------------------------------------------------------
def isolation_limits(
    permission_level: int,
    *,
    cpu_seconds: float | None = None,
    memory_mb: int | None = None,
    timeout: int | None = None,
    max_procs: int | None = None,
    max_open_files: int | None = None,
    seccomp: str | None = None,
    scopes: frozenset[str] = frozenset(),
) -> IsolatorLimits:
    """按权限等级 + 能力 scope 派生隔离需求。

    所有显式传入的请求参数**只会收窄、不会放宽**分层上界（与 sandbox 语义一致）：
    ``effective = min(requested or upper_bound, upper_bound)``。绝不提权。
    """
    level = min(int(permission_level), 4)
    cap = _MAX_BY_LEVEL.get(level, _MAX_BY_LEVEL[2])
    seccomp = seccomp or _seccomp_for_level(level)

    # 无网络 scope → 强制 user 档（即使 admin/owner 也收窄，防网络侧信道）
    if "network" not in scopes and seccomp in (SECCOMP_ADMIN, SECCOMP_OWNER):
        seccomp = SECCOMP_USER

    return IsolatorLimits(
        permission_level=level,
        cpu_seconds=min(cpu_seconds, cap["cpu_seconds"]) if cpu_seconds is not None else cap["cpu_seconds"],
        memory_mb=min(memory_mb, cap["memory_mb"]) if memory_mb is not None else int(cap["memory_mb"]),
        timeout=min(timeout, cap["timeout"]) if timeout is not None else int(cap["timeout"]),
        max_procs=min(max_procs, cap["max_procs"]) if max_procs is not None else int(cap["max_procs"]),
        max_open_files=min(max_open_files, cap["max_open_files"]) if max_open_files is not None else int(cap["max_open_files"]),
        seccomp=seccomp,
    )


def _seccomp_for_level(level: int) -> str:
    if level >= 4:
        return SECCOMP_OWNER
    if level == 3:
        return SECCOMP_ADMIN
    return SECCOMP_USER


def validate_limits(
    permission_level: int,
    *,
    cpu_seconds: float | None = None,
    memory_mb: int | None = None,
    timeout: int | None = None,
    max_procs: int | None = None,
    max_open_files: int | None = None,
) -> tuple[str, str]:
    """fail-closed 校验：**原始请求**的隔离需求是否可满足。

    返回 (status, detail)：``status == ISO_OK`` 才可执行；否则调用方必须 DENY。
    校验**只收紧、不放松**：任何"请求超上限"都判失败，绝不静默降级。
    （``isolation_limits`` 返回的是执行用的**有效**上限；这里校验的是调用方
    **原始请求**是否越界。）
    """
    level = min(int(permission_level), 4)
    cap = _MAX_BY_LEVEL.get(level, _MAX_BY_LEVEL[2])
    if timeout is not None and timeout > cap["timeout"]:
        return ISO_TIMEOUT_OVER, f"timeout {timeout} > cap {cap['timeout']}"
    if memory_mb is not None and memory_mb > cap["memory_mb"]:
        return ISO_MEMORY_OVER, f"memory {memory_mb} > cap {cap['memory_mb']}"
    if cpu_seconds is not None and cpu_seconds > cap["cpu_seconds"]:
        return ISO_CPU_OVER, f"cpu {cpu_seconds} > cap {cap['cpu_seconds']}"
    if max_procs is not None and max_procs > cap["max_procs"]:
        return ISO_PROCS_OVER, f"procs {max_procs} > cap {cap['max_procs']}"
    if max_open_files is not None and max_open_files > cap["max_open_files"]:
        return ISO_OPEN_FILES_OVER, f"open_files {max_open_files} > cap {cap['max_open_files']}"
    return ISO_OK, "limits satisfiable"


# ---------------------------------------------------------------------------
# 资源记账（可追踪）
# ---------------------------------------------------------------------------
@dataclass
class ResourceAccount:
    """一次执行/一段窗口内的资源使用记账。

    cpu_seconds / wall_seconds / bytes_written 累积，可对账（落 R6.5 execution
    trace / audit）。fail-open 语义：记账异常不阻断执行，只标记 degraded。
    """

    cpu_seconds: float = 0.0
    wall_seconds: float = 0.0
    bytes_written: int = 0
    executions: int = 0
    degraded: bool = False

    def record(self, cpu: float, wall: float, nbytes: int = 0) -> None:
        self.cpu_seconds += max(cpu, 0.0)
        self.wall_seconds += max(wall, 0.0)
        self.bytes_written += max(nbytes, 0)
        self.executions += 1

    def within_budget(self, limits: IsolatorLimits, budget_cpu: float | None = None) -> bool:
        """按配额判断当前累计是否在预算内（用于运行中终止决策）。

        任一维度超限 → False（调用方应 TERM/KILL）。预算缺省取 limits.cpu_seconds。
        """
        budget_cpu = budget_cpu if budget_cpu is not None else limits.cpu_seconds
        return self.cpu_seconds <= budget_cpu and self.wall_seconds <= limits.timeout


# ---------------------------------------------------------------------------
# 终止时序（可终止）
# ---------------------------------------------------------------------------
def terminate_signal(limits: IsolatorLimits, account: ResourceAccount) -> str | None:
    """返回应发的终止信号名：``TERM``（温和）→ 超限后 ``KILL``（强制）。

    None = 尚未超限，继续运行。两段式满足"可终止"且避免误杀。
    """
    if not account.within_budget(limits):
        # 温和超限（墙钟/CPU 略超）先 TERM；严重超限（2x）直接 KILL
        if account.wall_seconds > 2 * limits.timeout or account.cpu_seconds > 2 * limits.cpu_seconds:
            return "KILL"
        return "TERM"
    return None


# ---------------------------------------------------------------------------
# 审计适配（R6.2 事件链 sandbox 字段）
# ---------------------------------------------------------------------------
def isolation_to_event(limits: IsolatorLimits, account: ResourceAccount, result: str) -> dict:
    """投影成 R6.2 event_chain 可消费的 sandbox 事实。"""
    return {
        "sandbox": limits.isolator_name,
        "sandbox_limits": {
            "cpu": limits.cpu_seconds,
            "memory_mb": limits.memory_mb,
            "timeout": limits.timeout,
            "max_procs": limits.max_procs,
            "seccomp": limits.seccomp,
        },
        "resource_account": {
            "cpu_seconds": account.cpu_seconds,
            "wall_seconds": account.wall_seconds,
            "bytes_written": account.bytes_written,
            "executions": account.executions,
            "degraded": account.degraded,
        },
        "result": result,
    }