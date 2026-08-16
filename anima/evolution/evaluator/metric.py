"""R10-02 演化评估引擎 —— 指标采集。

从既有可观测接口（健康历史 / 运行验收 / 用量账本）抽取**可量化指标**，
供 Before/After 对比与改进证据生成。只读、不修改生产。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MetricSample:
    """一次指标快照：标签化、可比较。"""

    captured_at: float = field(default_factory=time.time)
    metrics: dict[str, float] = field(default_factory=dict)
    context: str = "snapshot"  # before | after | benchmark

    def to_dict(self) -> dict[str, Any]:
        return {
            "captured_at": self.captured_at,
            "context": self.context,
            "metrics": dict(self.metrics),
        }


def collect_metrics(core: Any, *, context: str = "snapshot") -> MetricSample:
    """从 core 既有接口抽取可量化指标。

    指标键（均为非负标量，便于对比）：
    - runtime_running: 0/1
    - channel_connected: 0/1
    - llm_request_count / llm_failure_count / llm_failure_rate
    - health_samples: 已积累健康样本数
    - automation_pending: 等待送达的自动化数
    - test_pass_rate: 若可取得最近测试通过率
    """
    metrics: dict[str, float] = {}

    running = bool(getattr(core, "_running", False))
    metrics["runtime_running"] = 1.0 if running else 0.0

    channel = getattr(getattr(core, "gateway", None), "channel", None)
    try:
        ch = channel.health() if channel and hasattr(channel, "health") else {}
    except Exception:
        ch = {}
    metrics["channel_connected"] = 1.0 if bool(ch.get("connected")) else 0.0

    llm = getattr(core, "llm", None)
    try:
        lh = llm.health() if llm and hasattr(llm, "health") else {}
    except Exception:
        lh = {}
    req = int(lh.get("request_count", 0) or 0)
    fail = int(lh.get("failure_count", 0) or 0)
    metrics["llm_request_count"] = float(req)
    metrics["llm_failure_count"] = float(fail)
    metrics["llm_failure_rate"] = (fail / req) if req else 0.0

    history = getattr(core, "health_history", None)
    meta = history.health() if history and hasattr(history, "health") else {}
    metrics["health_samples"] = float(int(meta.get("samples", 0) or 0))

    workflows = getattr(core, "workflows", None)
    summary = workflows.summary() if workflows and hasattr(workflows, "summary") else {}
    metrics["automation_pending"] = float(int(summary.get("active", 0) or 0))

    return MetricSample(metrics=metrics, context=context)
