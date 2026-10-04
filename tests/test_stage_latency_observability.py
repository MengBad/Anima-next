# -*- coding: utf-8 -*-
"""阶段耗时可观测：喂进 MetricsCollector + 随 health_history 落盘分位数

## 为什么要这个
10-04 三批修复（r13 计量 / r14 cognition 移出关键路径 / r16+r17 护栏）全部部署后
**效果一个字都没在真实流量上观测到** —— 群里闲 ⇒ 只能等 owner 问才取数，
而"部署完零证据"的尴尬会反复出现。

## 纪律：不新建范式
- **周期机制现成**：`HealthHistory.record()` 已由 `autonomy._tick` 周期调用，
  2880 样本留存 + JSON 落盘 ⇒ 只需**加字段**，不加 timer、不加文件。
- **分位计算现成**：`MetricsCollector.histogram` + `_HistogramBucket.percentile`，
  `snapshot()` 已导出 histograms（只被 WebUI 实时读、**不落盘**）⇒ 接上落盘即可。
- 数据源是 `[阶段耗时]` 已经打印出来的数 ⇒ **不新增埋点语句**。

## 判据要点
1. 阶段耗时要真的进 histogram（**含 0 值的回合也必须进**，否则分布失真）。
2. 落盘快照里要有分位字段，且**样本数为 0 时字段仍存在**（否则"没数据"与"字段缺失"分不清）。
3. 只读、fail-soft：观测失败绝不能影响回复路径。
4. 🔴 不得改任何既有字段（`health_history` 是既有契约，WebUI 在读）。
"""

from __future__ import annotations

import json
import time

import pytest

from anima.core.health_history import HealthHistory
from anima.core.metrics import get_metrics


# ── 1. 阶段耗时 → histogram ──────────────────────────────────────────

def test_stage_latency_fed_into_histogram():
    """阶段耗时必须真的进 histogram（否则落盘的分位恒为空）。"""
    from anima.pipeline.stages.observability import observe_stage_latency

    m = get_metrics()
    m.reset()
    observe_stage_latency("generation", 5330.0)
    observe_stage_latency("generation", 1500.0)
    observe_stage_latency("generation", 800.0)

    h = m.snapshot()["histograms"]
    # 🔴 key 格式由 `MetricsCollector._make_key` 决定：`name{k=v}`（**不是**裸名）
    key = "stage_latency_ms{stage=generation}"
    assert key in h, f"histogram 未落名：{sorted(h)}"
    assert h[key]["count"] == 3, f"样本数不对：{h[key]}"
    assert h[key]["p50"] == 1500.0, f"p50 不对：{h[key]}"


def test_zero_latency_turn_still_recorded():
    """🔴 回合 <1ms 时阶段耗时是 0.0（如纯 prefilter 拦截）——**必须照样进分布**。

    丢掉 0 值会让分位系统性偏高（"快回合"那一端被抹掉），
    而 prefilter 拦截恰恰是最快的一类回合。
    """
    from anima.pipeline.stages.observability import observe_stage_latency

    m = get_metrics()
    m.reset()
    observe_stage_latency("prefilter_stage", 0.0)
    observe_stage_latency("prefilter_stage", 90.6)

    h = m.snapshot()["histograms"]["stage_latency_ms{stage=prefilter_stage}"]
    assert h["count"] == 2, f"0 值被丢弃（分布失真）：{h}"
    assert h["min"] == 0.0, f"0 值未进分布：{h}"
    # 🔴 `_HistogramBucket.percentile` 的定义是 `sorted[int(n*p/100)]`
    # （2 样本取索引 1），**不是**中位数插值 ⇒ 判据按实现口径写，不臆造。
    assert h["p50"] == 90.6, f"p50 与实现的分位定义不符：{h}"
    assert h["avg"] == 45.3, f"0 值应参与均值：{h}"


def test_negative_and_nonfinite_are_dropped():
    """异常值（负数/NaN/inf）不得污染分布 —— 但也不能抛异常打断回复。"""
    from anima.pipeline.stages.observability import observe_stage_latency

    m = get_metrics()
    m.reset()
    for bad in (-1.0, float("nan"), float("inf"), None):
        observe_stage_latency("generation", bad)
    observe_stage_latency("generation", 100.0)

    h = m.snapshot()["histograms"]["stage_latency_ms{stage=generation}"]
    assert h["count"] == 1, f"异常值未剔除：{h}"


# ── 2. 随 health_history 落盘分位 ────────────────────────────────────

class _FakeLLM:
    def health(self):
        return {"request_count": 3, "failure_count": 1}


class _FakeSylann:
    def health(self):
        return {"observations": 1}


class _FakeCore:
    def __init__(self, d):
        from types import SimpleNamespace
        self.config = SimpleNamespace(persistence=SimpleNamespace(data_dir=d))
        self.llm = _FakeLLM()
        self.sylann = _FakeSylann()
        self.llm_handler = None
        self.gateway = None
        self._running = True
        self.agenda = None
        self.approvals = None
        self.monitors = None
        self.heartbeat = None
        self.skills = None
        self.intent_ledger = None
        self.workflows = None
        self.episodes = None


def test_snapshot_carries_latency_percentiles(tmp_path):
    """落盘快照必须带分位字段 —— 这是"以后不必人工取数"的关键。"""
    from anima.pipeline.stages.observability import observe_stage_latency

    get_metrics().reset()
    for v in (100.0, 200.0, 300.0, 400.0, 500.0):
        observe_stage_latency("generation_stage", v)

    core = _FakeCore(str(tmp_path))
    hh = HealthHistory(core, str(tmp_path))
    sample = hh.record(force=True)
    assert sample is not None

    lat = sample["stage_latency_ms"]
    assert lat, "快照里没有 stage_latency_ms"
    # 🔴 stage 名带 `_stage` 后缀（与 `[阶段耗时]` 日志里的键名一致）
    assert lat["generation_stage"]["count"] == 5, f"generation 分位缺失：{lat}"
    assert lat["generation_stage"]["p50"] == 300.0, f"p50 不对：{lat}"


def test_latency_fields_present_even_with_no_samples(tmp_path):
    """🔴 样本为 0 时字段**仍须存在**（值 0）——
    否则"没数据"与"字段缺失"分不清，排查时会误判成"功能没实现"。"""
    get_metrics().reset()
    core = _FakeCore(str(tmp_path))
    hh = HealthHistory(core, str(tmp_path))
    sample = hh.record(force=True)

    assert "stage_latency_ms" in sample, "无样本时 stage_latency_ms 键整个消失"
    assert isinstance(sample["stage_latency_ms"], dict)


def test_existing_fields_untouched(tmp_path):
    """🔴 不得改/删任何既有字段 —— health_history 是既有契约，WebUI 在读。"""
    get_metrics().reset()
    core = _FakeCore(str(tmp_path))
    hh = HealthHistory(core, str(tmp_path))
    sample = hh.record(force=True)

    for key in ("ts", "running", "channel_connected", "heartbeat_running",
                "watchdog_running", "watchdog_lag_sec", "llm_requests",
                "llm_failures", "sylann_observations", "outbound_failures"):
        assert key in sample, f"既有字段被破坏：{key}"


def test_recorded_to_disk_and_queryable(tmp_path):
    """必须真的落到磁盘且能被 query 读回（否则等于没记）。"""
    from anima.pipeline.stages.observability import observe_stage_latency

    get_metrics().reset()
    observe_stage_latency("cognition_stage", 67.0)
    core = _FakeCore(str(tmp_path))
    hh = HealthHistory(core, str(tmp_path))
    hh.record(force=True)

    path = tmp_path / "health_history.json"
    assert path.exists(), "health_history.json 未落盘"
    rows = json.loads(path.read_text(encoding="utf-8"))["samples"]
    assert rows[-1]["stage_latency_ms"]["cognition_stage"]["p50"] == 67.0

    got = hh.query(limit=10)
    assert got and got[-1]["stage_latency_ms"]["cognition_stage"]["count"] == 1


# ── 3. 解析：从 journal 文本喂入（回测/离线也用同一入口）──────────────

def test_parse_stage_line_extracts_all_stages():
    """必须能吃 journal 原文（回测历史样本时用同一入口，不另写解析器）。"""
    from anima.pipeline.stages.observability import parse_stage_line

    line = ("[阶段耗时] total=60947ms prefilter_stage=0.1 attachment_text_stage=0.0 "
            "bullying_stage=0.0 permission_stage=0.0 memory_stage=2146.5 "
            "cognition_stage=165.4 generation_stage=57607.4 guardrail_stage=1027.3")
    got = parse_stage_line(line)
    assert got["total"] == 60947.0
    assert got["cognition_stage"] == 165.4
    assert got["generation_stage"] == 57607.4
    # 🔴 子阶段单位是**毫秒但无 ms 后缀** ⇒ 正则若要求 ms 会全落 0。
    #    自证式断言：真实键集合必须一个不少（数错个数是常见失误，故比对集合而非长度）。
    assert set(got) == {
        "total", "prefilter_stage", "attachment_text_stage", "bullying_stage",
        "permission_stage", "memory_stage", "cognition_stage",
        "generation_stage", "guardrail_stage",
    }, f"字段集合不对：{sorted(got)}"


def test_parse_stage_line_ignores_garbage():
    from anima.pipeline.stages.observability import parse_stage_line

    assert parse_stage_line("随便一句日志") == {}
    assert parse_stage_line("") == {}
    # 🔴 前置计时行（只有 entry_to_pipeline、没有 total）**照样能解析出值**，
    #    只是它不算"一个完整回合"（那是 `is_turn_line` 的职责）。
    assert parse_stage_line("[阶段耗时] entry_to_pipeline=1340ms") == {"entry_to_pipeline": 1340.0}


def test_is_turn_line_requires_total():
    """`is_turn_line` 区分「前置计时」与「完整回合」——落盘只收回合。"""
    from anima.pipeline.stages.observability import is_turn_line

    assert is_turn_line("[阶段耗时][trace=345e9604] entry_to_pipeline=1340ms") is False
    assert is_turn_line("[阶段耗时] total=5452ms memory_stage=458.6") is True
    assert is_turn_line("") is False


def test_observe_turn_feeds_every_stage():
    from anima.pipeline.stages.observability import observe_turn

    m = get_metrics()
    m.reset()
    observe_turn({"total": 5452.0, "cognition_stage": 171.2, "generation_stage": 3803.3})
    h = m.snapshot()["histograms"]
    assert len(h) == 3, f"整回合未逐阶段喂入：{h}"
    assert h["stage_latency_ms{stage=generation_stage}"]["count"] == 1


# ── 4. 接线：真实回合必须自动进分布 ────────────────────────────────

@pytest.mark.asyncio
async def test_pipeline_turn_feeds_observability():
    """🔴 端到端接线判据：跑一次真 pipeline，`generation_stage` 必须出现在 histogram 里。

    这是"以后不必人工取数"能成立的前提 —— 只测 helper 不测接线，
    等于假设有人记得去调它（实测：没人会记得）。
    """
    import asyncio

    from anima.core.metrics import get_metrics
    from anima.pipeline import Pipeline, PipelineContext, StageResult
    from anima.pipeline.stages.observability import latency_snapshot

    # 🔴 必须用**生产的阶段函数名**（`_stage_ms` 的键取自 `stage.__name__`），
    #    否则判据自造名字会掩盖"真实名字没进分布"这类问题。
    async def generation_stage(ctx):
        await asyncio.sleep(0.01)
        ctx.reply = "喵"
        return StageResult.CONTINUE

    get_metrics().reset()
    pipe = Pipeline().use(generation_stage)
    ctx = PipelineContext(event={"message_id": "1"}, text="在吗", user_id=1,
                          group_id=None, detail_type="private", sender_name="t")
    await pipe.run(ctx)

    snap = latency_snapshot()
    assert "generation_stage" in snap, f"真实回合未进分布：{sorted(snap)}"
    assert snap["generation_stage"]["count"] == 1, f"计数不对：{snap}"
    assert snap["generation_stage"]["max"] > 0, f"耗时未记录：{snap}"
    assert "total" in snap, f"total 未进分布：{sorted(snap)}"
