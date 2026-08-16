"""R10-02 演化评估引擎 —— 基准与改进证据编排。

用法（由 R8/R9 闭环在「部署前/部署后」各采一次指标）：
    bench = Benchmark()
    bench.capture_before(core)        # 部署前
    ... 部署 / 演化 ...
    report = bench.capture_after(core)  # 部署后，产出 ImprovementEvidence
"""
from __future__ import annotations

from typing import Any

from .comparison import compare_samples, summarize_verdict
from .metric import MetricSample, collect_metrics


class Benchmark:
    """Before/After 基准：把两次指标快照转为可验证的改进证据。"""

    def __init__(self) -> None:
        self._before: MetricSample | None = None

    def capture_before(self, core: Any, *, context: str = "before") -> MetricSample:
        self._before = collect_metrics(core, context=context)
        return self._before

    def capture_after(self, core: Any, *, context: str = "after") -> dict[str, Any]:
        """采集 After 快照并与 Before 对比，产出改进证据报告。"""
        after = collect_metrics(core, context=context)
        if self._before is None:
            return {
                "ok": False,
                "reason": "no_before_snapshot",
                "after": after.to_dict(),
                "evidence": [],
                "summary": {},
            }
        evidence = compare_samples(self._before.metrics, after.metrics)
        summary = summarize_verdict(evidence)
        return {
            "ok": True,
            "before": self._before.to_dict(),
            "after": after.to_dict(),
            "evidence": [e.to_dict() for e in evidence],
            "summary": summary,
        }

    # 允许手工注入快照（测试 / 离线评估场景）
    def set_before_metrics(self, metrics: dict[str, float]) -> None:
        self._before = MetricSample(metrics=metrics, context="before")

    def evaluate_pair(self, before: dict[str, float], after: dict[str, float]) -> dict[str, Any]:
        evidence = compare_samples(before, after)
        return {
            "ok": True,
            "before": {"metrics": before},
            "after": {"metrics": after},
            "evidence": [e.to_dict() for e in evidence],
            "summary": summarize_verdict(evidence),
        }
