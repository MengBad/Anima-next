"""Agent 安全护栏 —— 统一接口与规范化 Phase 5。

集中管理 Agent 路径（仅 Owner 可触发）的安全约束：
- 危险命令黑名单（rm -rf /、dd、mkfs、fork bomb、chmod 777 / 等）
- 资源配额（磁盘写入 / 网络请求数 / 总时长 / 单步输出）
- 路径穿越防护（限定工作区内）
- URL 协议白名单（仅 http/https）

这些约束只作用于 Agent 路径，不影响群聊普通工具调用。
"""

from __future__ import annotations

import re
import shlex
import time
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# 危险命令黑名单（Agent 路径专用）
# ---------------------------------------------------------------------------

AGENT_BLOCKED_PATTERNS: list[str] = [
    r"\brm\s+(-rf|-fr|--recursive)\s+/",          # rm -rf /
    r"\brm\s+-rf\s+/\b",                          # 变体
    r"\bdd\s+if=",                                # dd 直写设备
    r"\bmkfs",                                    # 格式化
    r":\(\)\s*\{\s*.*\}\s*;\s*:",                 # fork bomb
    r"\bchmod\s+777\s+/",                         # chmod 777 /
    r">\s*/dev/sd",                               # 直写块设备
    r"\b(shutdown|reboot|halt|poweroff)\b",       # 关机/重启
    r"\b(curl|wget)\s+.*\|\s*(sh|bash)\b",        # 下载即执行
]

# 路径穿越防护：任何以这些前缀开头的绝对路径都拒绝（除非显式白名单）
_AGENT_PATH_TRAVERSAL_RE = re.compile(r"(\.\./|\.\.\\|/etc/passwd|/etc/shadow|~/.ssh|id_rsa)", re.I)

ALLOWED_URL_SCHEMES = {"http", "https"}


def is_blocked_command(command: str) -> bool:
    """判断一条 shell 命令是否命中危险黑名单。

    解析失败（含无法拆词的注入）一律视为危险并拒绝。
    """
    if not command or not command.strip():
        return True
    try:
        tokens = shlex.split(command)
    except ValueError:
        return True
    text = " ".join(tokens)
    if _AGENT_PATH_TRAVERSAL_RE.search(text):
        return True
    for pat in AGENT_BLOCKED_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            return True
    return False


def check_path_within(base: str | Path, path: str | Path) -> bool:
    """路径穿越防护：path 解析后必须落在 base 目录内（含 base 自身）。

    同时拒绝任何包含 '..' 或以 base 之外的绝对路径形式出现的路径。
    """
    try:
        base_res = Path(base).resolve()
        target = Path(path)
        # 先拒绝明显的相对越界片段
        if any(part in ("..",) for part in target.parts):
            # 允许 "./x" 形式，但 ".." 直接拒绝
            if ".." in target.parts:
                return False
        target_res = target.resolve()
    except (OSError, RuntimeError):
        return False
    if target_res == base_res:
        return True
    return base_res in target_res.parents


def is_safe_url(url: str) -> bool:
    """仅允许 http/https 协议，拒绝 file://、ftp://、javascript: 等。"""
    if not url:
        return False
    lowered = url.strip().lower()
    if "://" not in lowered:
        # 含 ":" 但没有 "://"：形如 "javascript:..." 的伪协议，直接拒绝。
        if ":" in lowered:
            return False
        # 无协议头，按 http 处理（调用方负责拼接主机名）
        lowered = "http://" + lowered
    scheme = lowered.split("://", 1)[0]
    return scheme in ALLOWED_URL_SCHEMES


# ---------------------------------------------------------------------------
# 资源配额
# ---------------------------------------------------------------------------

AGENT_RESOURCE_LIMITS: dict[str, int | float] = {
    "max_disk_write_mb": 100,       # 单任务最大磁盘写入
    "max_network_requests": 50,     # 单任务最大网络请求数
    "max_total_duration_sec": 600,  # 单任务最大总时长
    "max_output_size_kb": 512,      # 单步最大输出
}


class ResourceBudget:
    """单任务资源计数与配额校验。

    由 AgentExecutor 在每一步调用 consume_*，超限即抛出 ResourceExceeded。
    """

    def __init__(self, limits: dict[str, int | float] | None = None,
                 start_time: float | None = None):
        self.limits = dict(AGENT_RESOURCE_LIMITS)
        if limits:
            self.limits.update(limits)
        self.start_time = start_time if start_time is not None else time.time()
        self.disk_write_mb = 0.0
        self.network_requests = 0
        self.output_size_kb = 0.0  # 最近一步的输出（非累计，用于单步校验）
        self._last_step_output_kb = 0.0

    def consume_disk_write(self, mb: float) -> None:
        self.disk_write_mb += mb
        self._check("disk_write_mb", self.disk_write_mb, "max_disk_write_mb")

    def consume_network_request(self) -> None:
        self.network_requests += 1
        self._check("network_requests", self.network_requests, "max_network_requests")

    def consume_output(self, kb: float) -> None:
        """记录单步输出大小（覆盖式，仅校验最近一步）。"""
        self.output_size_kb = kb
        self._check("output_size_kb", self.output_size_kb, "max_output_size_kb")

    def check_duration(self) -> None:
        elapsed = time.time() - self.start_time
        self._check("duration", elapsed, "max_total_duration_sec")

    def _check(self, name: str, value: float, limit_key: str) -> None:
        limit = self.limits.get(limit_key)
        if limit is not None and value > limit:
            from anima.agent.guardrails import ResourceExceeded
            raise ResourceExceeded(
                f"资源配额超限：{name}={value:.1f} > 上限 {limit}（{limit_key}）"
            )

    def elapsed(self) -> float:
        return time.time() - self.start_time

    def as_dict(self) -> dict[str, Any]:
        return {
            "disk_write_mb": round(self.disk_write_mb, 2),
            "network_requests": self.network_requests,
            "elapsed_sec": round(self.elapsed(), 1),
            "limits": self.limits,
        }


class ResourceExceeded(Exception):
    """资源配额超限，由执行器捕获并标记任务失败。"""
