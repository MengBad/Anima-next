"""上游静默的**归因**与**挽救**（2026-09-23）。

背景
----
09-23 实测：同一条「本喵这边刚打了个嗝喵」在 24h 内出站两次，病因却**不是同一个**：

| 时刻 | 触发 | 病因 | 遥测桶 |
|---|---|---|---|
| 16:31:17 | 「无职转生里鸡妈妈的cv是谁」 | 三候选 `HTTP 200 + 空 content` ×2 轮 | `upstream_empty` ✅ |
| 14:55:02 | 用户质问 bot 发的图 | `ClientOSError`/`ServerDisconnectedError`，连续失败 8/9/10 次熔断 | `no_tool` ❌ |

两个缺陷因此暴露：

1. **归因不完整**：`_upstream_went_empty()` 只看 `empty_completion_count` 增量。但熔断打开时
   `llm/__init__.py:563-567` **早退、不增这个计数** ⇒ 纯传输故障被洗成 `fallback_no_tool`
   （"模型自己无话可说"）。审计据此读结论就会读错。
2. **用户侧无挽救**：`_finalize_content` 只做**一次**纯对话重试；它一旦也空，立刻出罐头话术。
   而 16:31 那次网关抖动只持续约 20 秒 —— 等一会儿再要一次，大概率能救回正文。

本文件钉住这两件事。判据边界（都不许悄悄放宽）：
- 归因只认**进程内一手计数的增量**，不认话术、不认 `tool_trace`；
- 挽救**只在上游静默时**发生（模型真的自己无话可说时不加延迟），**只等一次**、
  且**只在本来就要出罐头话术的路径上**付出延迟；
- 熔断剩余时长超过预算时**直接放弃等**（等不到头，白等）。
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

import pytest

from anima.agent.react import (
    ReActLoop,
    _LLM_FAULT_COUNTERS,
    _NO_TOOL_FALLBACKS,
)
from anima.cognitive.reply_quality import ReplyQuality

_ROOT = Path(__file__).resolve().parents[1]

_SAVED_REPLY = "救回来的回答喵"


# ── 桩件 ─────────────────────────────────────────────────────────────────

class _StubLLM:
    """最小 llm 替身：按脚本吐 content，吐空时（可选）自增「200-空」计数。

    `bump=False` 用来模拟「上游没坏、模型自己就是没话说」—— 这条路径**不得**被归因成
    上游故障，也不得为它付出额外延迟。
    """

    def __init__(self, script, *, bump=True):
        self.script = list(script)
        self.bump = bump
        self.empty_completion_count = 0
        self.failure_count = 0
        self.circuit_open_count = 0
        # 出站专用计数（**判据面**）—— 本替身代表的就是「出站正文调用」，故同步自增。
        self.outbound_empty_completion_count = 0
        self.outbound_failure_count = 0
        self.outbound_circuit_open_count = 0
        self.circuit_open_until = 0.0
        self.chat_calls = 0

    def _next(self) -> str:
        value = self.script.pop(0) if self.script else ""
        if self.bump and not str(value).strip():
            self.empty_completion_count += 1
            self.outbound_empty_completion_count += 1
        return value

    async def chat_structured(self, messages, tools, temperature=0.8, max_tokens=512):
        return {"content": self._next(), "tool_calls": []}

    async def chat(self, messages, temperature=0.8, max_tokens=512):
        self.chat_calls += 1
        return self._next()


class _StubTools:
    def get_schemas(self, permission_level, sort_by_priority=True):
        return []


@pytest.fixture()
def rq(tmp_path) -> ReplyQuality:
    return ReplyQuality(str(tmp_path))


@pytest.fixture()
def slept(monkeypatch) -> list:
    """把等待截获下来 —— 既避免测试真的睡，也让「等了多久、等了几次」可断言。"""
    record: list[float] = []

    async def _fake_sleep(seconds):
        record.append(float(seconds))

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    return record


def _loop(llm, quality, *, patience, circuit_until=0.0) -> ReActLoop:
    llm.circuit_open_until = circuit_until
    return ReActLoop(llm, _StubTools(), quality=quality,
                     upstream_patience_sec=patience)


def _agent(trace, quality, *, baseline, **counters) -> ReActLoop:
    """只验归因的轻量构造（不跑 run()）。"""
    a = ReActLoop.__new__(ReActLoop)
    a.tool_trace = trace
    a.quality = quality
    counts = {"outbound_empty_completion_count": 0, "outbound_failure_count": 0,
              "outbound_circuit_open_count": 0}
    counts.update(counters)
    a.llm = type("L", (), counts)()
    if baseline is not None:
        a._llm_fault_baseline = baseline
    return a


# ── 1. 判据：三个计数缺一不可 ────────────────────────────────────────────

def test_fault_counters_are_exported_by_llm_health():
    """`_LLM_FAULT_COUNTERS` 必须是 `LLMClient.health()` 真导出的键。

    改名/漏导出会让归因**静默降级**回旧行为（错得毫无察觉），必须源码级钉住。
    """
    src = (_ROOT / "anima" / "llm" / "__init__.py").read_text(encoding="utf-8-sig")
    for name in _LLM_FAULT_COUNTERS:
        assert f'"{name}": self.{name}' in src, f"health() 未导出 {name}"


def test_fault_counters_are_a_triple_and_frozen():
    assert _LLM_FAULT_COUNTERS == (
        "outbound_empty_completion_count", "outbound_failure_count",
        "outbound_circuit_open_count",
    )


def test_missing_counter_degrades_to_not_silent():
    """任一计数读不到 ⇒ **不**宣称上游故障（fail-closed，绝不误报）。"""
    a = _agent([], None, baseline=(0, 0, 0), outbound_empty_completion_count=9)
    # 换一个缺 `failure_count` 的替身（老版本 llm / 未迁移的假 client）。
    a.llm = type("L", (), {"outbound_empty_completion_count": 9,
                           "outbound_circuit_open_count": 0})()
    assert a._upstream_went_silent() is False


def test_no_baseline_degrades_to_not_silent():
    """从不 run() 的对象没有基线 ⇒ 不把历史累计值当"本轮发生了降级"。"""
    a = _agent([], None, baseline=None, outbound_empty_completion_count=9, outbound_failure_count=9)
    assert a._upstream_went_silent() is False


# ── 2. 归因：传输故障/熔断 不再被洗成 no_tool ─────────────────────────────

def test_transport_failure_is_attributed_to_upstream(rq):
    """14:55 的病因：候选穷尽后仍失败（`failure_count` 增量），**没有** 200-空。"""
    a = _agent([], rq, baseline=(0, 0, 0), outbound_failure_count=1)
    assert a._upstream_went_silent() is True
    assert a._upstream_went_empty() is False, "传输故障不是 200-空，不得混为一谈"
    assert a._empty_content_fallback() in _NO_TOOL_FALLBACKS
    rep = rq.report()
    assert rep["fallback_upstream_error"] == 1
    assert rep["fallback_no_tool"] == 0, "传输故障不得再被读成「模型自己无话可说」"
    assert rep["fallback_total"] == 1


def test_breaker_open_is_attributed_to_upstream(rq):
    """熔断计数增量同样是上游故障 —— 熔断时 `_post` 早退，连计数都不增。"""
    a = _agent([], rq, baseline=(0, 0, 0), outbound_circuit_open_count=1)
    assert a._empty_content_fallback() in _NO_TOOL_FALLBACKS
    assert rq.report()["fallback_upstream_error"] == 1


def test_empty_completion_keeps_its_own_bucket(rq):
    """两种病因**分桶**：200-空 与 传输/熔断 混在一起就等于没修归因。"""
    a = _agent([], rq, baseline=(0, 0, 0), outbound_empty_completion_count=6, outbound_failure_count=2)
    a._empty_content_fallback()
    rep = rq.report()
    assert rep["fallback_upstream_empty"] == 1
    assert rep["fallback_upstream_error"] == 0, "200-空优先，不得落到传输桶"


def test_tool_trace_no_longer_steals_attribution(rq):
    """工具成功过也不改变归因 —— 真实病因优先（与 09-22 定的口径一致）。"""
    a = _agent([{"ok": True, "name": "x_search"}], rq,
               baseline=(0, 0, 0), outbound_failure_count=3)
    a._empty_content_fallback()
    rep = rq.report()
    assert rep["fallback_upstream_error"] == 1
    assert rep["fallback_tool_ok"] == 0


def test_model_says_nothing_still_lands_in_no_tool(rq):
    """反向守卫：模型真的自己无话可说（计数无增量）仍记 `no_tool`。"""
    a = _agent([], rq, baseline=(0, 0, 0))
    assert a._upstream_went_silent() is False
    a._empty_content_fallback()
    assert rq.report()["fallback_no_tool"] == 1


# ── 3. 等待预算（纯函数语义） ────────────────────────────────────────────

def test_patience_is_off_by_default():
    a = _loop(_StubLLM([]), None, patience=0.0)
    assert a._patience_wait() is None


def test_patience_waits_full_budget_when_no_breaker():
    a = _loop(_StubLLM([]), None, patience=6.0)
    assert a._patience_wait() == pytest.approx(6.0, abs=1e-6)


def test_patience_waits_out_a_short_breaker():
    """熔断剩余在预算内 ⇒ 等到它自然关闭再试（这才是等的意义）。"""
    a = _loop(_StubLLM([]), None, patience=6.0,
              circuit_until=time.time() + 2.0)
    wait = a._patience_wait()
    assert wait is not None and abs(wait - 2.0) < 0.5


def test_patience_gives_up_when_breaker_outlasts_budget():
    """熔断剩余超过预算 ⇒ 等不到头，直接出兜底（别白等）。"""
    a = _loop(_StubLLM([]), None, patience=6.0,
              circuit_until=time.time() + 600.0)
    assert a._patience_wait() is None


# ── 4. 挽救：真的救回正文 ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_upstream_silence_is_rescued_by_one_more_attempt(rq, slept):
    """16:31 那种抖动：等一次再要，正文救回来了 ⇒ **不出罐头话术**。"""
    llm = _StubLLM(["", "", _SAVED_REPLY])
    a = _loop(llm, rq, patience=6.0)

    reply = await a.run([{"role": "user", "content": "鸡妈妈的cv是谁"}])

    assert reply == _SAVED_REPLY
    assert slept == [6.0], "应当且只应当等一次"
    rep = rq.report()
    assert rep["fallback_total"] == 0, "救回来了就不算兜底出站"


@pytest.mark.asyncio
async def test_still_empty_after_patience_falls_back_once(rq, slept):
    """上游持续静默 ⇒ 等一次后仍空 ⇒ 落罐头话术，且**只等一次**（不做重试风暴）。"""
    llm = _StubLLM(["", "", ""])
    a = _loop(llm, rq, patience=6.0)

    reply = await a.run([{"role": "user", "content": "鸡妈妈的cv是谁"}])

    assert reply in _NO_TOOL_FALLBACKS
    assert slept == [6.0]
    assert rq.report()["fallback_upstream_empty"] == 1
    assert rq.report()["fallback_total"] == 1


@pytest.mark.asyncio
async def test_no_extra_latency_when_model_simply_says_nothing(rq, slept):
    """模型自己无话可说（上游没坏）⇒ 不加延迟、不重试，行为与改动前一致。"""
    llm = _StubLLM(["", ""], bump=False)
    a = _loop(llm, rq, patience=6.0)

    reply = await a.run([{"role": "user", "content": "怎么说"}])

    assert reply in _NO_TOOL_FALLBACKS
    assert slept == [], "病因不是上游静默时不得为它付出延迟"
    assert llm.chat_calls == 1, "只应有一次纯对话重试（改动前的行为）"
    assert rq.report()["fallback_no_tool"] == 1


@pytest.mark.asyncio
async def test_patience_disabled_keeps_old_behaviour(rq, slept):
    """一键回滚：预算 0 ⇒ 立刻出兜底，零延迟。"""
    llm = _StubLLM(["", "", _SAVED_REPLY])
    a = _loop(llm, rq, patience=0.0)

    reply = await a.run([{"role": "user", "content": "鸡妈妈的cv是谁"}])

    assert reply in _NO_TOOL_FALLBACKS
    assert slept == []
    assert llm.chat_calls == 1, "关闭时不得再发一次请求"


@pytest.mark.asyncio
async def test_long_breaker_skips_the_wait_entirely(rq, slept):
    """熔断还剩很久 ⇒ 不等待、不重试，立刻出兜底（省掉一段白等）。"""
    llm = _StubLLM(["", ""])
    a = _loop(llm, rq, patience=6.0, circuit_until=time.time() + 600.0)

    reply = await a.run([{"role": "user", "content": "怎么说"}])

    assert reply in _NO_TOOL_FALLBACKS
    assert slept == []
    assert rq.report()["fallback_upstream_empty"] == 1


# ── 5. 源码级：判据不得被悄悄放宽 ────────────────────────────────────────

def test_source_keeps_attribution_on_counter_deltas_only():
    """归因不得回退到话术/`tool_trace` 判据（那是 09-22 定案要修掉的错位）。

    先剥 docstring 再断言 —— 判据的**解释性文字**里引用 `tool_trace`（正是在说
    "不许用它"），不剥就会把说明当成实现。这与既有「源码级断言必须先剥注释」同源。
    """
    src = (_ROOT / "anima" / "agent" / "react.py").read_text(encoding="utf-8-sig")
    body = src.split("def _upstream_went_silent")[1].split("def _empty_content_fallback")[0]
    body = re.sub(r'"""[\s\S]*?"""', "", body)
    body = "\n".join(line.split("#")[0] for line in body.splitlines())
    assert "tool_trace" not in body
    assert "_llm_fault_counts()" in body


def test_source_fallback_path_is_shared_by_both_exits():
    """两处出口（早退 / 触顶收尾）都必须走同一个挽救入口，避免口径各自漂移。"""
    src = (_ROOT / "anima" / "agent" / "react.py").read_text(encoding="utf-8-sig")
    assert src.count("await self._finalize_or_fallback(") == 2
    assert src.count("self._empty_content_fallback()") == 1, (
        "罐头话术出口必须是**单一**入口（`_finalize_or_fallback`），"
        "否则两处出口的挽救/计数口径迟早各自漂移"
    )


# ── 6. 预算默认值必须落在「观测到的静默窗」之内 ──────────────────────────

def test_default_budget_covers_observed_silence_window():
    """🔴 6.0 是被数据否掉的值，别再退回去（2026-09-25 复核 48h）。

    4 次真实样本（18:37 / 22:21 / 23:25 / 00:03）**全部 6s 后重试仍空**；
    静默实际持续 **8–20s**（23:25 那次直到 30s 熔断）。⇒ 小于静默下限的预算
    **结构上不可能成功**，那 6s 是纯延迟、零收益。默认值必须高于观测下限。
    """
    from anima.agent.react import _DEFAULT_UPSTREAM_PATIENCE_SEC
    from anima.core.config import ReactConfig

    assert _DEFAULT_UPSTREAM_PATIENCE_SEC >= 8.0, "低于观测静默下限（8s）= 注定失败的白等"
    assert _DEFAULT_UPSTREAM_PATIENCE_SEC == 20.0, "取观测静默窗上沿（8–20s）"
    assert ReactConfig().upstream_patience_sec == _DEFAULT_UPSTREAM_PATIENCE_SEC, (
        "配置默认与代码默认必须同源，否则「没写这个键」的两条读取路径会给出两个答案"
    )


def test_default_yaml_documents_the_same_budget():
    """出厂配置里写的值也必须同源（生产没这个键 ⇒ 走代码默认，两边不能打架）。"""
    import yaml

    cfg = yaml.safe_load((_ROOT / "config" / "default.yaml").read_text(encoding="utf-8"))
    from anima.agent.react import _DEFAULT_UPSTREAM_PATIENCE_SEC
    assert float(cfg["react"]["upstream_patience_sec"]) == _DEFAULT_UPSTREAM_PATIENCE_SEC
