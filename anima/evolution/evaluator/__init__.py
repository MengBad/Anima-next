"""R10-02 演化评估引擎：Before/After 度量与改进证据。

把「优化前指标 → 修改 → 优化后指标」固化为可验证的 ImprovementEvidence，
作为 R9 受控自我进化与 R8 闭环的「效果评估」环节证据。只读、不自我授权。
"""
from .metric import MetricSample, collect_metrics
from .comparison import (
    ImprovementEvidence,
    compare_metric,
    compare_samples,
    summarize_verdict,
)
from .benchmark import Benchmark

__all__ = [
    "MetricSample",
    "collect_metrics",
    "ImprovementEvidence",
    "compare_metric",
    "compare_samples",
    "summarize_verdict",
    "Benchmark",
]
