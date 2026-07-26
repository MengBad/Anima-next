"""结构化运行指标收集器 —— 统一各模块零散统计。

设计：
- 进程内轻量实现，无外部依赖（不引入 Prometheus/DataDog）
- counter: 单调递增计数器（消息数、错误数、工具调用数）
- gauge: 瞬时值（内存、连接数、队列深度）
- histogram: 分布统计（延迟、token 数）—— 仅保留 count/sum/min/max/p50/p99
- snapshot(): 一次性导出所有指标，供 WebUI /api/metrics 使用

线程安全：使用 threading.Lock 保护写操作；读操作（snapshot）允许弱一致。
"""

from __future__ import annotations
import threading
import time
from collections import defaultdict
from typing import Any, Optional


class _HistogramBucket:
    """单个 histogram 指标的聚合状态。"""
    __slots__ = ("count", "total", "min_val", "max_val", "_values")

    def __init__(self):
        self.count = 0
        self.total = 0.0
        self.min_val = float("inf")
        self.max_val = float("-inf")
        # 保留最近 256 个样本用于 p50/p99 估算；超出后 FIFO 淘汰
        self._values: list[float] = []

    def observe(self, value: float) -> None:
        self.count += 1
        self.total += value
        if value < self.min_val:
            self.min_val = value
        if value > self.max_val:
            self.max_val = value
        self._values.append(value)
        if len(self._values) > 256:
            self._values = self._values[-256:]

    def percentile(self, p: float) -> float:
        if not self._values:
            return 0.0
        sorted_vals = sorted(self._values)
        idx = int(len(sorted_vals) * p / 100.0)
        idx = min(idx, len(sorted_vals) - 1)
        return sorted_vals[idx]

    def as_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "sum": round(self.total, 3),
            "min": round(self.min_val, 3) if self.count else 0,
            "max": round(self.max_val, 3) if self.count else 0,
            "avg": round(self.total / self.count, 3) if self.count else 0,
            "p50": round(self.percentile(50), 3),
            "p99": round(self.percentile(99), 3),
        }


class MetricsCollector:
    """进程内轻量指标收集器。

    用法::

        metrics = MetricsCollector()
        metrics.counter("messages.inbound")
        metrics.counter("messages.outbound", tags={"group": "12345"})
        metrics.gauge("llm.consecutive_failures", 3)
        metrics.histogram("llm.latency_ms", 1234.5)
        snapshot = metrics.snapshot()
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._counters: dict[str, int] = defaultdict(int)
        self._gauges: dict[str, tuple[float, float]] = {}  # name -> (value, timestamp)
        self._histograms: dict[str, _HistogramBucket] = {}
        self._created_at = time.time()

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def counter(self, name: str, value: int = 1, tags: Optional[dict[str, str]] = None) -> None:
        """递增计数器。tags 会拼接到 key 中（name{tag1=v1,tag2=v2}）。"""
        key = self._make_key(name, tags)
        with self._lock:
            self._counters[key] += value

    def gauge(self, name: str, value: float, tags: Optional[dict[str, str]] = None) -> None:
        """设置瞬时值。"""
        key = self._make_key(name, tags)
        with self._lock:
            self._gauges[key] = (value, time.time())

    def histogram(self, name: str, value: float, tags: Optional[dict[str, str]] = None) -> None:
        """记录一个分布样本。"""
        key = self._make_key(name, tags)
        with self._lock:
            if key not in self._histograms:
                self._histograms[key] = _HistogramBucket()
            self._histograms[key].observe(value)

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def get_counter(self, name: str, tags: Optional[dict[str, str]] = None) -> int:
        key = self._make_key(name, tags)
        return self._counters.get(key, 0)

    def get_gauge(self, name: str, tags: Optional[dict[str, str]] = None) -> Optional[float]:
        key = self._make_key(name, tags)
        entry = self._gauges.get(key)
        return entry[0] if entry else None

    def snapshot(self) -> dict[str, Any]:
        """一次性导出所有指标（WebUI /api/metrics 使用）。"""
        with self._lock:
            return {
                "uptime_sec": round(time.time() - self._created_at, 1),
                "counters": dict(self._counters),
                "gauges": {k: {"value": v[0], "ts": round(v[1], 1)} for k, v in self._gauges.items()},
                "histograms": {k: h.as_dict() for k, h in self._histograms.items()},
            }

    def reset(self) -> None:
        """清空所有指标（测试用）。"""
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()
            self._created_at = time.time()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _make_key(name: str, tags: Optional[dict[str, str]] = None) -> str:
        if not tags:
            return name
        tag_str = ",".join(f"{k}={v}" for k, v in sorted(tags.items()))
        return f"{name}{{{tag_str}}}"


# ---------------------------------------------------------------------------
# 进程级单例（可选使用；模块也可直接持有独立实例）
# ---------------------------------------------------------------------------

_global_metrics: Optional[MetricsCollector] = None
_metrics_lock = threading.Lock()


def get_metrics() -> MetricsCollector:
    """获取进程级 MetricsCollector 单例。"""
    global _global_metrics
    if _global_metrics is None:
        with _metrics_lock:
            if _global_metrics is None:
                _global_metrics = MetricsCollector()
    return _global_metrics
