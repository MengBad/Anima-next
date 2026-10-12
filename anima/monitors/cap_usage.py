"""CAP（CLIProxyAPI）侧用量与失败归因的**只读**探针。

## 为什么需要它

生产唯一的 LLM 出口就是 CPA（`llm.endpoint: http://54.168.242.65:8317/v1`）。
在此之前，Anima 只有一条路知道上游出事：**自己发请求、被打回来**。
代价是 2026-10-11 实测到的那一幕 —— CPA 在 13:25 就把
`gemini-3.8-flash-high` 判死并冷却 9.27h，而 Anima 侧直到累积
**89 次失败**才触发切换，期间每次请求白等 1.8s，用户看到兜底话术。

但 CPA 其实**一直知道**原因：它装的 `cap-token-usage-tracker` 插件把每次
调用（含 `failed` / `failure_status` / `reasoning_effort` / `alias`）
都写进了 `data/token-usage-tracker.db`，并通过
`/v0/resource/plugins/cap-token-usage-tracker/*` 暴露出来。
⇒ 本模块就是把那份**上游自述**读进来，让 Anima 不用靠猜。

## 口径与边界（重要）

- **只读**：仅 GET，不发任何写请求、不碰 CPA 的 config / 容器。
- **fail-closed**：CAP 不可达 ⇒ `reachable=False` + `error`，
  **绝不伪造"健康"**。读不到就是"未知"，不是"没事"。
- **不自行判定"模型已死"**：本模块只**呈现** CAP 的数字。是否据此切换
  主模型属于调用方的策略，且需要授权 —— 探针不做决定。
- 拉取失败可由调用方静默降级；此模块不抛异常。

数据结构对应 CAP 的 `stats/groups`：
每个 `item` 描述一个 (provider, model, alias, reasoning_effort, auth) 组合的
调用计数与失败计数，正是"哪个模型在什么档位上被打回来"的直接答案。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("anima.monitors.cap_usage")

#: CAP 插件的路由前缀（2026-10-11 实测确认，无需鉴权）。
CAP_PREFIX = "/v0/resource/plugins/cap-token-usage-tracker"

#: CAP 支持的时间窗（实测：`1h` 会返 `unsupported range`，别写错）。
CAP_RANGES = ("24h", "7d", "30d", "all")

_DEFAULT_RANGE = "24h"
_DEFAULT_TIMEOUT = 8.0


@dataclass(frozen=True)
class CapModelGroup:
    """一个 (provider, model, alias, effort) 组合的调用与失败计数。"""

    provider: str
    model: str
    alias: str
    reasoning_effort: str
    requests: int
    failed_requests: int
    failure_status: int
    average_latency_ns: int
    source: str = ""

    @property
    def success_rate(self) -> float:
        if self.requests <= 0:
            return 0.0
        return max(0.0, 1.0 - (self.failed_requests / self.requests))

    @property
    def all_failed(self) -> bool:
        """整组全失败 —— 这才是"这个组合已经废了"的硬判据。

        注意单看失败数会被大流量误导：724 次里 4 次失败是噪声，
        35 次里 35 次失败才是事故。
        """
        return self.requests > 0 and self.failed_requests >= self.requests

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "alias": self.alias,
            "reasoning_effort": self.reasoning_effort,
            "requests": self.requests,
            "failed_requests": self.failed_requests,
            "failure_status": self.failure_status,
            "success_rate": round(self.success_rate, 4),
            "all_failed": self.all_failed,
            "average_latency_ms": round(self.average_latency_ns / 1e6, 1)
            if self.average_latency_ns
            else 0.0,
        }


@dataclass
class CapUsageSnapshot:
    """一次拉取的完整结果。`reachable=False` 时代其余字段无意义。"""

    reachable: bool
    captured_at: float = field(default_factory=time.time)
    error: str = ""
    range: str = _DEFAULT_RANGE
    #: CAP 自己的"数据自 何时起保留"，用于识别「CAP 刚被重置」这种情形。
    retained_since: str = ""
    last_used: str = ""
    summary: dict[str, Any] = field(default_factory=dict)
    groups: list[CapModelGroup] = field(default_factory=list)
    latency_ms: float = 0.0

    # ---- 派生视图（供归因用，都是对 groups 的纯函数筛选） ----

    def broken_groups(self) -> list[CapModelGroup]:
        """全失败且样本量足够、且不是 4xx 客户端错误的组合。

        `failure_status` 在 400 这类**确定性错误**上才值得警惕；如果整组
        全 400，通常意味着参数/档位与模型不兼容（正是 2026-10-11 的
        `minimal` 事件形态），而不是上游抖动。
        """
        return [
            g
            for g in self.groups
            if g.all_failed and g.requests >= 3
        ]

    def failed_models(self) -> dict[str, int]:
        """model/alias → 失败数（仅含有失败记录的组合）。"""
        out: dict[str, int] = {}
        for g in self.groups:
            if g.failed_requests > 0:
                name = g.alias or g.model
                out[name] = out.get(name, 0) + g.failed_requests
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "reachable": self.reachable,
            "range": self.range,
            "captured_at": self.captured_at,
            "error": self.error,
            "retained_since": self.retained_since,
            "last_used": self.last_used,
            "latency_ms": round(self.latency_ms, 1),
            "summary": dict(self.summary),
            "groups": [g.to_dict() for g in self.groups],
            "broken_groups": [g.to_dict() for g in self.broken_groups()],
            "failed_models": self.failed_models(),
        }


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_group(item: dict[str, Any]) -> CapModelGroup:
    return CapModelGroup(
        provider=str(item.get("provider") or ""),
        model=str(item.get("model") or ""),
        alias=str(item.get("alias") or ""),
        reasoning_effort=str(item.get("reasoning_effort") or ""),
        requests=_to_int(item.get("requests")),
        failed_requests=_to_int(item.get("failed_requests")),
        failure_status=_to_int(item.get("failure_status")),
        average_latency_ns=_to_int(item.get("average_latency_ns")),
        source=str(item.get("source") or ""),
    )


class CapUsageProbe:
    """按需拉取 CAP 用量快照。无后台任务、无缓存 —— 调用方决定频率。

    刻意**不做**自动轮询：拉取频率应当是显式策略（避免把只读探针变成
    事实上的健康检查依赖）。需要定时就由调用方放进自己的心跳。
    """

    def __init__(
        self,
        endpoint: str,
        *,
        token: str = "",
        timeout: float = _DEFAULT_TIMEOUT,
        range: str = _DEFAULT_RANGE,
    ) -> None:
        #: 形如 `http://54.168.242.65:8317/v1` —— 去掉尾部的 `/v1` 得到根。
        self.endpoint = str(endpoint or "").rstrip("/")
        self.token = str(token or "")
        self.timeout = max(1.0, float(timeout))
        self.range = range if range in CAP_RANGES else _DEFAULT_RANGE

    @property
    def base_url(self) -> str:
        base = self.endpoint
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        return base.rstrip("/")

    async def fetch(self) -> CapUsageSnapshot:
        """拉一次快照。**不抛异常** —— 任何失败都转成 `reachable=False`。"""
        if not self.base_url:
            return CapUsageSnapshot(
                reachable=False, error="未配置 llm.endpoint", range=self.range
            )
        url = f"{self.base_url}{CAP_PREFIX}/stats/groups?range={self.range}&limit=200&sort=failed_requests&direction=desc"
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        started = time.monotonic()
        try:
            import aiohttp
        except ImportError:  # pragma: no cover - aiohttp 是运行期依赖
            return CapUsageSnapshot(
                reachable=False, error="aiohttp 不可用", range=self.range
            )
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers) as resp:
                    latency = (time.monotonic() - started) * 1000.0
                    if resp.status != 200:
                        return CapUsageSnapshot(
                            reachable=False,
                            error=f"HTTP {resp.status}",
                            range=self.range,
                            latency_ms=latency,
                        )
                    data = await resp.json(content_type=None)
        except asyncio.TimeoutError:
            return CapUsageSnapshot(
                reachable=False, error="timeout", range=self.range
            )
        except Exception as exc:  # noqa: BLE001 - 探针绝不因上游异常而炸
            return CapUsageSnapshot(
                reachable=False,
                error=f"{type(exc).__name__}: {exc}",
                range=self.range,
            )
        return self._parse(data, latency_ms=(time.monotonic() - started) * 1000.0)

    def _parse(self, data: Any, *, latency_ms: float) -> CapUsageSnapshot:
        if not isinstance(data, dict):
            return CapUsageSnapshot(
                reachable=False, error="返回体非对象", range=self.range
            )
        items = data.get("items")
        if not isinstance(items, list):
            # schema 变了 —— 明确报告，而不是当空列表（那会伪装成"一切正常"）。
            return CapUsageSnapshot(
                reachable=False,
                error="返回体缺少 items 列表（CAP schema 可能已变更）",
                range=self.range,
                latency_ms=latency_ms,
            )
        groups = [_parse_group(i) for i in items if isinstance(i, dict)]
        summary = data.get("summary")
        return CapUsageSnapshot(
            reachable=True,
            range=str(data.get("range") or self.range),
            retained_since=str(data.get("retained_since") or ""),
            last_used=str(data.get("last_used") or ""),
            summary=dict(summary) if isinstance(summary, dict) else {},
            groups=groups,
            latency_ms=latency_ms,
        )

    # ---- 同步路径（供已在事件循环里的调用方使用） ----

    @property
    def _status_url(self) -> str:
        return (
            f"{self.base_url}{CAP_PREFIX}/stats/groups"
            f"?range={self.range}&limit=200&sort=failed_requests&direction=desc"
        )

    def fetch_sync(self) -> CapUsageSnapshot:
        """同步拉一次快照。**不抛异常**，任何失败都转成 `reachable=False`。

        为什么需要一条同步路径：健康采样的调用方（心跳）**跑在事件循环里**，
        在那里 `asyncio.run()` 会直接抛错。用 `urllib` 走标准库，
        既绕开"循环冲突"，也不为此改变 HTTP 客户端栈（探针只发一个 GET）。
        """
        if not self.base_url:
            return CapUsageSnapshot(
                reachable=False, error="未配置 llm.endpoint", range=self.range
            )
        import urllib.error
        import urllib.request

        req = urllib.request.Request(self._status_url)
        req.add_header("Accept", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                latency = (time.monotonic() - started) * 1000.0
                status = getattr(resp, "status", 200)
                body = resp.read()
        except urllib.error.HTTPError as exc:
            return CapUsageSnapshot(
                reachable=False,
                error=f"HTTP {exc.code}",
                range=self.range,
                latency_ms=(time.monotonic() - started) * 1000.0,
            )
        except Exception as exc:  # noqa: BLE001 - 探针绝不因上游异常而炸
            return CapUsageSnapshot(
                reachable=False,
                error=f"{type(exc).__name__}: {exc}",
                range=self.range,
                latency_ms=(time.monotonic() - started) * 1000.0,
            )
        if status != 200:
            return CapUsageSnapshot(
                reachable=False, error=f"HTTP {status}", range=self.range,
                latency_ms=latency,
            )
        try:
            import json as _json

            data = _json.loads(body.decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001
            return CapUsageSnapshot(
                reachable=False, error=f"JSON 解析失败: {exc}", range=self.range,
                latency_ms=latency,
            )
        return self._parse(data, latency_ms=latency)


__all__ = ["CapUsageProbe", "CapUsageSnapshot", "CapModelGroup", "CAP_PREFIX", "CAP_RANGES"]
