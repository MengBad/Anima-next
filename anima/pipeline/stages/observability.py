# -*- coding: utf-8 -*-
"""阶段耗时可观测（只读观测，fail-soft）。

## 背景
2026-10-04 三批修复（r13 计量 / r14 cognition 移出关键路径 / r16+r17 护栏）部署后
**效果在真实流量上零证据** —— 群里闲，只能等人工审核窗口才取数。
本模块把 `[阶段耗时]` 已经打印出来的数喂进 `MetricsCollector`，
再由**既有的** `HealthHistory.record()`（已由 `autonomy._tick` 周期调用）落盘分位数。

## 为什么不新建范式
- 周期机制现成：`HealthHistory` 2880 样本留存 + JSON 落盘 ⇒ 只加字段，不加 timer/文件。
- 分位计算现成：`MetricsCollector.histogram` + `_HistogramBucket.percentile`。
- 数据源现成：`[阶段耗时]` 本来就在打日志 ⇒ **不新增埋点语句**。

## 🔴 两条口径纪律（都来自实测踩坑）
1. **子阶段单位是毫秒但无 `ms` 后缀**：`total=60947ms` 有后缀，
   `cognition_stage=165.4` 没有 ⇒ 要求后缀的正则会**全部落 0**（本日真踩过）。
2. **0 值必须照样进分布**：`prefilter_stage=0.1` 这类"快回合"是最快的一端，
   丢掉会让分位系统性偏高。
"""

from __future__ import annotations

import math
import re
from typing import Any

from anima.core.metrics import get_metrics

#: histogram 名（不带 tags 的裸名，便于 WebUI /api/metrics 直接读）
STAGE_LATENCY_METRIC = "stage_latency_ms"

#: `[阶段耗时] total=60947ms prefilter_stage=0.1 …`
#: 🔴 键名统一 `(?P<key>[A-Za-z_][A-Za-z0-9_]*)=`，**单位后缀可有可无**
#:（`total=60947ms` 有、`cognition_stage=165.4` 没有 —— 日志本身就不一致）。
#: 🔴 **不要写成 `ms?`**：那是"必须有 m + 可选 s"，会把**全部子阶段**判成不匹配
#: ⇒ 分位恒为空（本日真踩过，且踩了两次：探针一次、实现一次）。
_STAGE_KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([0-9]+(?:\.[0-9]+)?)(?:ms)?\b")
#: 该行必须是 `[阶段耗时]` 且带 `total=` 才算一个完整回合 —— 只出现
#: `entry_to_pipeline=1340ms` 的那行是**前置计时**，没有 total，不是回合。
_TURN_MARK_RE = re.compile(r"\[阶段耗时\].*\btotal=[0-9]")


def parse_stage_line(line: str) -> dict[str, float]:
    """从一行日志里解析出全部 `键=值`（毫秒）。

    解析不到就是空 dict —— **不抛异常**（观测不该打断任何东西）。
    """
    if not line:
        return {}
    out: dict[str, float] = {}
    for key, val in _STAGE_KV_RE.findall(line):
        try:
            out[key] = float(val)
        except ValueError:
            continue
    return out


def is_turn_line(line: str) -> bool:
    """该行是否为**一个完整回合**的阶段耗时（带 `total=`）。"""
    return bool(line) and bool(_TURN_MARK_RE.search(line))


def observe_stage_latency(stage: str, value_ms: float) -> None:
    """把一个阶段耗时喂进 histogram。

    fail-soft + 剔异常值：`None` / NaN / inf / 负数 一律丢弃（污染分位比不记更糟）。
    """
    if not stage or value_ms is None:
        return
    try:
        v = float(value_ms)
    except (TypeError, ValueError):
        return
    if not math.isfinite(v) or v < 0:
        return
    try:
        get_metrics().histogram(STAGE_LATENCY_METRIC, v, tags={"stage": stage})
    except Exception:  # noqa: BLE001 — 观测绝不阻断回复路径
        return


def observe_turn(stages: dict[str, Any]) -> None:
    """喂入一个完整回合的**全部**阶段耗时。"""
    if not isinstance(stages, dict):
        return
    for key, value in stages.items():
        observe_stage_latency(key, value)


def observe_line(line: str) -> bool:
    """直接吃一行 journal 文本。返回是否为完整回合。

    离线回测 / 补录历史样本时走同一入口，**不另写解析器**。
    """
    if not is_turn_line(line):
        return False
    observe_turn(parse_stage_line(line))
    return True


def latency_snapshot() -> dict[str, Any]:
    """导出各阶段分位（`count`/`p50`/`p99`/`avg`/`max`），供落盘用。

    🔴 分位字段**沿用 `_HistogramBucket.as_dict()` 的真实键名**（`p50`/`p99`，
    没有 `p90`）—— 不自造字段名，否则读的人要去猜对应关系。

    🔴 **无样本的阶段不出现在结果里** —— 调用方（`HealthHistory`）负责补零值占位，
    这样"没数据"与"字段缺失"在落盘产物里仍可区分。
    """
    try:
        hists = get_metrics().snapshot().get("histograms", {}) or {}
    except Exception:  # noqa: BLE001
        return {}

    prefix = STAGE_LATENCY_METRIC + "{"
    out: dict[str, Any] = {}
    for key, bucket in hists.items():
        if not key.startswith(prefix) or not key.endswith("}"):
            continue
        # key 形如 `stage_latency_ms{stage=generation_stage}`
        # （格式由 `MetricsCollector._make_key` 决定：`name{k=v}`，`=` 分隔）
        inner = key[len(prefix):-1]
        stage = inner.split("=", 1)[1] if "=" in inner else inner
        stage = stage.strip()
        if not stage:
            continue
        data = dict(bucket) if isinstance(bucket, dict) else {}
        out[stage] = {
            "count": int(data.get("count", 0) or 0),
            "p50": round(float(data.get("p50", 0.0) or 0.0), 1),
            "p99": round(float(data.get("p99", 0.0) or 0.0), 1),
            "avg": round(float(data.get("avg", 0.0) or 0.0), 1),
            "max": round(float(data.get("max", 0.0) or 0.0), 1),
        }
    return out
