"""兜底出站遥测守卫（2026-09-19 · P0-a）。

背景
----
兜底出口此前只 `return <文案>`，**不留任何结构化痕迹**。09-19 把话术改成多变体后
（见 `test_fallback_pools.py` / `test_react_empty_fallback_wording.py`），连
`grep 文案` 这个老口径也失效了 —— 于是「人格完善到底有没有用」变成一个**无法回答**
的问题。本文件钉住补齐后的可观测面。

口径
----
`fallback_*` 计的**不是"回复不好"**，而是：这一条最终出站**不是模型正常生成的正文**，
而是护栏改写或降级话术替身。它与 `total_replies` 是**子集关系**，用来算"机器话术占比"。

三层断言
--------
1. 桶清单完整（`_empty()` 预声明）+ 文案子集口径正确（`fallback_total`）；
2. 未知 kind 不产生新键（进 `fallback_other`）—— 防调用方拼错导致计数器无界增长；
3. **源码级**：3 处埋点入口 + 13 个出口的调用都在场。这一条是必需的：本改动是
   纯可观测的，**删掉任何一处都不会让行为测试变红**，只有源码级断言能兜住。

   2026-09-20 复检补漏：出口实为 **13** 个 —— `generation._resolve_bullying_reply`
   的两条 `COLD_RESPONSES`（霸凌冷回 / 回避期冷回）此前**完全没埋点**，
   「11 个出口全接上」的口径不完整。
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from anima.agent.react import (
    ReActLoop,
    _NO_TOOL_FALLBACKS,
    _TOOL_FAILED_FALLBACKS,
    _TOOL_OK_FALLBACKS,
)
from anima.cognitive.reply_quality import ReplyQuality, _empty
from anima.contracts.access import DeprecatedCoreAccess, _TelemetryFacade
from anima.pipeline import PipelineContext
from anima.pipeline.stages.guardrail import _MECHANICAL_REWRITES, guardrail_stage
from anima.pipeline.stages.generation import COLD_RESPONSES, _resolve_bullying_reply

_ROOT = Path(__file__).resolve().parents[1]

#: kind → 期望落桶。**手工镜像**实现里的白名单，而不是 import 它 ——
#: 若 import，则"改坏映射"与"改坏断言"会同时发生而互不报警。
_KIND_TO_KEY = {
    "no_tool": "fallback_no_tool",
    "tool_failed": "fallback_tool_failed",
    "tool_ok": "fallback_tool_ok",
    "breaker": "fallback_breaker",
    "react_error": "fallback_react_error",
    "empty_reply": "fallback_empty_reply",
    "mechanical": "fallback_mechanical",
    "leak_blocked": "fallback_leak_blocked",
    "analysis_essay": "fallback_analysis_essay",
    "political": "fallback_political",
    "cold_bullying": "fallback_cold_bullying",
    "cold_avoidance": "fallback_cold_avoidance",
}

_ALL_BUCKETS = tuple(_KIND_TO_KEY.values()) + ("fallback_other",)


@pytest.fixture()
def rq(tmp_path) -> ReplyQuality:
    return ReplyQuality(str(tmp_path))


def _react(trace, quality) -> ReActLoop:
    """只验出口与计数，不需要完整构造（该方法只读 tool_trace / quality）。"""
    a = ReActLoop.__new__(ReActLoop)
    a.tool_trace = trace
    a.quality = quality
    return a


def _ctx(core, reply: str = "", text: str = "怎么说") -> PipelineContext:
    ctx = PipelineContext(
        event={}, text=text, user_id=888, group_id=222,
        detail_type="group", sender_name="Alice",
        meta={},  # 无 chat_id → 跳过记忆写入副作用
    )
    ctx.reply = reply
    # 生产路径：`ctx.core` 是 M0.1 Facade（handler 无条件注入），
    # 所以埋点走 `telemetry().record_fallback()` 这层链，而不是直接摸 core。
    ctx.core = DeprecatedCoreAccess(core)
    return ctx


# ── 1. 桶清单与口径 ──────────────────────────────────────────────────────

def test_every_bucket_is_predeclared():
    """所有 fallback_* 桶必须在 `_empty()` 里预声明。

    `_metrics()` 用 `setdefault` 补齐，`record_fallback` 也能凭空创建键 ——
    所以漏声明**不会崩**，只会让这张表悄悄不是权威清单。这条断言就是防"悄悄"。
    """
    declared = set(_empty())
    missing = [b for b in _ALL_BUCKETS if b not in declared]
    assert not missing, f"_empty() 未声明这些桶: {missing}"


def test_default_day_has_zero_total(rq):
    """干净的一天：没有任何兜底出站，且不产生噪音信号。"""
    rep = rq.report()
    assert rep["fallback_total"] == 0
    assert all(rep[b] == 0 for b in _ALL_BUCKETS)


@pytest.mark.parametrize("kind,key", sorted(_KIND_TO_KEY.items()))
def test_record_fallback_maps_kind_to_its_bucket(rq, kind, key):
    rq.record_fallback(kind)
    rep = rq.report()
    assert rep[key] == 1, f"kind={kind!r} 未落到 {key}"
    assert rep["fallback_total"] == 1
    others = [b for b in _ALL_BUCKETS if b != key]
    assert all(rep[b] == 0 for b in others), f"kind={kind!r} 串到了别的桶"


def test_unknown_kind_falls_into_other_and_creates_no_new_key(rq):
    """拼错的 kind 不得凭空造键（否则计数器会无界增长、报表字段漂移）。"""
    rq.record_fallback("totally_made_up_kind")
    rep = rq.report()
    assert rep["fallback_other"] == 1
    assert rep["fallback_total"] == 1
    assert {k for k in rep if k.startswith("fallback_")} == {*_ALL_BUCKETS, "fallback_total"}


def test_unknown_kind_covers_none_and_empty(rq):
    """`None` / 空串也算未知（调用方漏传时不能静默丢失）。"""
    rq.record_fallback(None)
    rq.record_fallback("")
    assert rq.report()["fallback_other"] == 2


def test_total_sums_every_bucket(rq):
    rq.record_fallback("no_tool")
    rq.record_fallback("mechanical")
    rq.record_fallback("mechanical")
    rq.record_fallback("bogus")
    rep = rq.report()
    assert rep["fallback_no_tool"] == 1
    assert rep["fallback_mechanical"] == 2
    assert rep["fallback_other"] == 1
    assert rep["fallback_total"] == 4
    assert rep["fallback_total"] == sum(rep[b] for b in _ALL_BUCKETS)


def test_normal_reply_is_not_counted_as_fallback(rq):
    """口径守卫：正常生成的正文**不算**兜底 —— 否则「机器话术占比」就失去意义。"""
    rq.record_reply("本喵今天心情不错喵。", detail_type="group", target_id=222)
    rep = rq.report()
    assert rep["total_replies"] == 1
    assert rep["fallback_total"] == 0


def test_format_report_mentions_fallback_total(rq):
    rq.record_fallback("react_error")
    assert "兜底出站 1" in rq.format_report()


# ── 2. react 出口 ────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "trace,pool,key",
    [
        ([], _NO_TOOL_FALLBACKS, "fallback_no_tool"),
        ([{"ok": False, "name": "x_search"}], _TOOL_FAILED_FALLBACKS, "fallback_tool_failed"),
        ([{"ok": True, "name": "x_search"}], _TOOL_OK_FALLBACKS, "fallback_tool_ok"),
    ],
)
def test_react_empty_fallback_records_and_keeps_wording(rq, trace, pool, key):
    """出口既计数、又仍然返回池内话术（零行为变更）。"""
    a = _react(trace, rq)
    for _ in range(20):
        assert a._empty_content_fallback() in pool
    rep = rq.report()
    assert rep[key] == 20
    assert rep["fallback_total"] == 20


def test_react_telemetry_is_fail_soft_when_quality_is_none():
    """遥测绝不能把兜底本身摔掉：quality=None 时仍要正常出话。"""
    a = _react([], None)
    assert a._empty_content_fallback() in _NO_TOOL_FALLBACKS


def test_react_telemetry_is_fail_soft_when_quality_lacks_method():
    """老/替身 quality 对象没有 record_fallback 时，不得抛异常。"""
    a = _react([], SimpleNamespace())
    assert a._empty_content_fallback() in _NO_TOOL_FALLBACKS


def test_react_helper_does_not_log_error_on_missing_quality():
    """fail-soft 的日志级别应是 debug —— 不该在生产刷 error/warning。"""
    src = (_ROOT / "anima" / "agent" / "react.py").read_text(encoding="utf-8-sig")
    body = src.split("def _record_fallback")[1].split("async def _finalize_content")[0]
    assert "logger.debug" in body
    assert "logger.warning" not in body and "logger.error" not in body


# ── 3. guardrail 出口（经真实 Facade 链） ────────────────────────────────

@pytest.mark.asyncio
async def test_guardrail_mechanical_rewrite_records_via_facade(tmp_path):
    rq = ReplyQuality(str(tmp_path))
    ctx = _ctx(SimpleNamespace(reply_quality=rq), reply="作为 AI，我不能回答这个问题。")
    await guardrail_stage(ctx)
    assert ctx.reply in _MECHANICAL_REWRITES, "机械话术未被改写（行为回归）"
    assert rq.report()["fallback_mechanical"] == 1


@pytest.mark.asyncio
async def test_guardrail_analysis_essay_records(tmp_path):
    rq = ReplyQuality(str(tmp_path))
    ctx = _ctx(
        SimpleNamespace(reply_quality=rq),
        reply=(
            "从政治社会学的角度看，这个话题涉及多重结构性因素。首先，需要区分实证科学与"
            "规范判断；其次，任何单一解释都难以覆盖全部变量。综上所述，应当采取审慎立场。"
        ),
    )
    await guardrail_stage(ctx)
    assert rq.report()["fallback_analysis_essay"] == 1


@pytest.mark.asyncio
async def test_guardrail_political_records(tmp_path):
    rq = ReplyQuality(str(tmp_path))
    ctx = _ctx(SimpleNamespace(reply_quality=rq), reply="有人说是颜色革命搞的。")
    await guardrail_stage(ctx)
    assert ctx.reply == "这个话题本喵不碰喵～聊点别的吧！"
    assert rq.report()["fallback_political"] == 1


@pytest.mark.asyncio
async def test_guardrail_telemetry_is_fail_soft_without_core():
    """core 为 None（探针/降级环境）时不得抛异常、回复照常。"""
    ctx = _ctx(None, reply="作为 AI，我不能回答这个问题。")
    await guardrail_stage(ctx)
    assert ctx.reply in _MECHANICAL_REWRITES


@pytest.mark.asyncio
async def test_guardrail_telemetry_is_fail_soft_with_broken_core():
    """`ctx.core` 是个**没有 telemetry()** 的对象（未迁移替身）时也不得抛。"""
    ctx = _ctx(None, reply="作为 AI，我不能回答这个问题。")
    ctx.core = SimpleNamespace()  # 无 telemetry()
    await guardrail_stage(ctx)
    assert ctx.reply in _MECHANICAL_REWRITES


@pytest.mark.asyncio
async def test_guardrail_normal_reply_records_nothing(tmp_path):
    """反向守卫：正常接话不得被计入兜底（否则"机器话术占比"失去意义）。"""
    rq = ReplyQuality(str(tmp_path))
    ctx = _ctx(SimpleNamespace(reply_quality=rq), reply="哈哈，这个本喵也觉得挺有意思的喵。")
    await guardrail_stage(ctx)
    assert ctx.reply == "哈哈，这个本喵也觉得挺有意思的喵。"
    assert rq.report()["fallback_total"] == 0


# ── 4. Facade 委托 ───────────────────────────────────────────────────────

def test_facade_delegates_record_fallback(tmp_path):
    rq = ReplyQuality(str(tmp_path))
    _TelemetryFacade(SimpleNamespace(reply_quality=rq)).record_fallback("breaker")
    assert rq.report()["fallback_breaker"] == 1


def test_facade_is_silent_without_reply_quality():
    """core 没有 reply_quality（探针/降级环境）时不抛。"""
    _TelemetryFacade(SimpleNamespace()).record_fallback("breaker")


def test_telemetry_protocol_declares_record_fallback(tmp_path):
    """协议面：`TelemetryProtocol` 必须声明该方法，且真实 Facade 满足 isinstance。

    2026-09-19 教训：往 `TelemetryProtocol` 加方法会**同时**打挂所有 fake 实现
    （`test_facade_protocols.FakeTelemetry` 因 runtime_checkable 的 isinstance 而红）——
    那是设计使然，不是误报；这里把"协议声明 + 真实实现合规"钉在同一条断言里。
    """
    from anima.contracts.facades import TelemetryProtocol

    assert hasattr(TelemetryProtocol, "record_fallback")
    facade = _TelemetryFacade(SimpleNamespace(reply_quality=ReplyQuality(str(tmp_path))))
    assert isinstance(facade, TelemetryProtocol)


# ── 5. 源码级：埋点不得被静默删除 ────────────────────────────────────────

def test_source_records_at_every_exit():
    """纯可观测改动 —— 删掉埋点不会让任何行为测试变红，只有这条能兜住。"""
    react = (_ROOT / "anima" / "agent" / "react.py").read_text(encoding="utf-8-sig")
    gen = (_ROOT / "anima" / "pipeline" / "stages" / "generation.py").read_text(encoding="utf-8-sig")
    gr = (_ROOT / "anima" / "pipeline" / "stages" / "guardrail.py").read_text(encoding="utf-8-sig")

    assert react.count("self._record_fallback(") == 3, "react 三处出口都应计数"
    assert gen.count("_record_fallback(core, ") == 4, (
        "generation 四处埋点：3 处降级 + `_resolve_bullying_reply` 内的 COLD_RESPONSES 冷回"
        "（该处用 `kind` 参数区分「霸凌冷回 / 回避期冷回」两个出口）"
    )
    assert gr.count("_record_fallback(ctx, ") == 5, "guardrail 五处出口都应计数"


def test_source_fallback_records_cover_every_pool_exit():
    """每个 `random.choice(池)` 出口的**紧邻前方**必须有计数调用。

    比数量断言更强：防止"三处计数都还在，但有两处挪到了别的分支"。
    """
    for rel, pool in (
        ("anima/pipeline/stages/generation.py", "_DEGRADE_FALLBACKS"),
        ("anima/pipeline/stages/generation.py", "COLD_RESPONSES"),
        ("anima/pipeline/stages/guardrail.py", "_MECHANICAL_REWRITES"),
    ):
        src = (_ROOT / rel).read_text(encoding="utf-8-sig")
        lines = [re.sub(r"#.*", "", ln) for ln in src.splitlines()]
        for idx, ln in enumerate(lines):
            if f"random.choice({pool})" not in ln:
                continue
            window = "\n".join(lines[max(0, idx - 4):idx])
            assert "_record_fallback(" in window, (
                f"{rel}:{idx + 1} 的 {pool} 出口前缺少计数埋点"
            )


def test_source_no_dead_quality_lookup_remains_in_guardrail():
    """`guardrail.py` 里那行**半途而废**的遥测（取了 quality 却从不使用）已清掉。

    它是本改动的前身：当初想记「政治回避」，只写了取变量的那一行就停了，
    留下一个死变量。改用 Facade 后不应再有 `meta.get("core")` 式取值。
    """
    src = (_ROOT / "anima" / "pipeline" / "stages" / "guardrail.py").read_text(encoding="utf-8-sig")
    assert 'getattr(ctx.meta.get("core")' not in src, "旧的 meta[core] 取值路径又回来了"


# ── 6. COLD_RESPONSES 出口（2026-09-20 补漏：第 12/13 个出口）─────────────

def test_cold_bullying_exit_records_fallback(tmp_path):
    """霸凌冷回出口此前**完全没有埋点** —— 它必须真的走到 telemetry。

    口径：`COLD_RESPONSES` 是固定话术池（非模型生成正文）⇒ 属于 `fallback_*`。
    """
    rq = ReplyQuality(str(tmp_path))
    core = SimpleNamespace(reply_quality=rq)

    text = _resolve_bullying_reply(1, 0, core=core)

    assert text in COLD_RESPONSES, "应当返回池里的句子"
    rep = rq.report()
    assert rep["fallback_cold_bullying"] == 1
    assert rep["fallback_total"] == 1


def test_cold_avoidance_uses_its_own_bucket(tmp_path):
    """回避期冷回与霸凌冷回**分桶**，便于区分"被攻击"与"回避期内简短回复"。"""
    rq = ReplyQuality(str(tmp_path))
    core = SimpleNamespace(reply_quality=rq)

    _resolve_bullying_reply(0, core=core, kind="cold_avoidance")

    rep = rq.report()
    assert rep["fallback_cold_avoidance"] == 1
    assert rep["fallback_cold_bullying"] == 0
    assert rep["fallback_total"] == 1


def test_silence_branch_does_not_count_as_fallback(tmp_path):
    """「完全沉默」（recent>=3 / severity>=3）**没有出站** ⇒ 不算兜底出站。

    若把计数放在函数开头，沉默也会被算成"机器话术出站"，占比分母就脏了。
    """
    rq = ReplyQuality(str(tmp_path))
    core = SimpleNamespace(reply_quality=rq)

    assert _resolve_bullying_reply(3, 0, core=core) is None
    assert _resolve_bullying_reply(0, 3, core=core) is None
    assert rq.report()["fallback_total"] == 0


def test_cold_exit_survives_missing_core(tmp_path):
    """core 缺省（老调用点 / 单测直接调）不得抛 —— fail-soft 是硬约束。"""
    assert _resolve_bullying_reply(1, 0) in COLD_RESPONSES
    assert _resolve_bullying_reply(1, 0, core=None) in COLD_RESPONSES


def test_cold_exit_survives_broken_quality(tmp_path):
    """遥测自身抛异常也不得影响回复路径。"""
    bad = SimpleNamespace(reply_quality=SimpleNamespace(
        record_fallback=lambda kind: (_ for _ in ()).throw(RuntimeError("boom"))
    ))
    assert _resolve_bullying_reply(1, 0, core=bad) in COLD_RESPONSES
