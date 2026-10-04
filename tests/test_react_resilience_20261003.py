"""ReAct 韧性修复（2026-10-03）：整链失败不再白等 ＋ 工具超时可配。

两处都来自 2026-10-03 的审核（`reports/2026-10-03_reply-problems.md`）：

1. **整链失败后的 20s 白等**。`upstream_patience_sec=20.0` 的设计前提是「上游瞬时
   抖动、等一会儿能救回」。但 10-03 簇 A 实测：**两次整链失败之后仍等满 20s，
   第三次照样全空** ⇒ 那 20s 是纯延迟、零收益，用户白等。
   判据：**出站桶** `consecutive_failures >= 2`（连续两次整链失败）⇒ 上游处于
   窗口性故障，等待无意义，直接出兜底；只有**单次**失败才保留等待（原设计）。
   计数粒度已核实：`consecutive_failures += 1`（`llm/__init__.py:815`）在候选循环
   **之后**，即按「整链失败事件」计，非按候选计。

2. **工具单次超时硬编码 120s**。10-02 实测 `x_search` 单次耗 **102 秒**
   （未触发超时），同群两条消息排队 125s/121s 后各用 49ms 判 idle —— 用户白等 2 分钟。
   改为可配置 `react.tool_timeout_sec`，默认收紧到 45s（覆盖内部串行最坏：
   browser `goto` 30s + `click` 15s），并把超时文案里的秒数改成同源，避免
   「实际 45s、文案写 120 秒」的自相矛盾。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from anima.agent.react import (
    ReActLoop,
    _DEFAULT_TOOL_TIMEOUT_SEC,
    _NO_TOOL_FALLBACKS,
)
from anima.cognitive.reply_quality import ReplyQuality
from anima.llm import (
    LLMClient,
    _CIRCUIT_BACKGROUND,
    _CIRCUIT_OUTBOUND,
    outbound_consecutive_failures,
)

_ROOT = Path(__file__).resolve().parents[1]


# ── 桩件 ─────────────────────────────────────────────────────────────────

class _StubTools:
    def get_schemas(self, permission_level, sort_by_priority=True):
        return []


def _client() -> LLMClient:
    """最小可用 client（不建连，只用来读/写熔断桶状态）。"""
    return LLMClient("http://127.0.0.1:1/v1", "k", retries=0, retry_backoff_sec=0)


@pytest.fixture()
def rq(tmp_path) -> ReplyQuality:
    return ReplyQuality(str(tmp_path))


# ── 1. 出站桶读取（新增的模块级助手） ────────────────────────────────────

def test_outbound_consecutive_failures_reads_outbound_bucket():
    """必须读出站桶 —— 读错桶会把背景抖动当成出站故障（10-02 分桶引入的漂移）。"""
    c = _client()
    c._circuits[_CIRCUIT_OUTBOUND]["failures"] = 2
    c._circuits[_CIRCUIT_BACKGROUND]["failures"] = 7
    assert outbound_consecutive_failures(c) == 2


def test_outbound_consecutive_failures_failsafe_on_legacy_stub():
    """旧替身没有出站桶 API ⇒ 返回 0（fail-open 到「仍可等待」），绝不抛。"""
    class _Legacy:
        pass

    assert outbound_consecutive_failures(_Legacy()) == 0
    assert outbound_consecutive_failures(None) == 0


# ── 2. 判据：连续整链失败 ≥2 ⇒ 不等 ──────────────────────────────────────

def _loop(llm, *, patience, tool_timeout_sec=None) -> ReActLoop:
    kw = {}
    if tool_timeout_sec is not None:
        kw["tool_timeout_sec"] = tool_timeout_sec
    return ReActLoop(llm, _StubTools(), upstream_patience_sec=patience, **kw)


def test_patience_skips_after_two_consecutive_failures():
    """🔴 核心判据：出站已连续两次整链失败 ⇒ 返回 None（立刻兜底，不白等）。

    10-03 簇 A：02:00:53 与 02:00:56 两次整链失败后，仍等 20s 才重试，
    第三次照样全空 ⇒ 那 20s 是纯延迟。
    """
    c = _client()
    c._circuits[_CIRCUIT_OUTBOUND]["failures"] = 2
    a = _loop(c, patience=20.0)
    assert a._patience_wait() is None


def test_patience_waits_when_only_single_failure():
    """反向守卫：只失败过一次（可能是瞬时抖动）⇒ 保留原设计的等待。"""
    c = _client()
    c._circuits[_CIRCUIT_OUTBOUND]["failures"] = 1
    a = _loop(c, patience=20.0)
    assert a._patience_wait() == pytest.approx(20.0, abs=1e-6)


def test_patience_ignores_background_bucket_failures():
    """背景桶连续失败再多也不得短路出站等待 —— 两桶语义独立。"""
    c = _client()
    c._circuits[_CIRCUIT_BACKGROUND]["failures"] = 9
    a = _loop(c, patience=20.0)
    assert a._patience_wait() == pytest.approx(20.0, abs=1e-6)


@pytest.mark.asyncio
async def test_run_skips_wait_and_falls_back_when_upstream_circuit_degraded(rq):
    """端到端：出站已连续失败 ⇒ `run()` 不睡、不重试，立刻兜底。"""
    slept: list[float] = []

    async def _fake_sleep(seconds):
        slept.append(float(seconds))

    c = _client()
    c._circuits[_CIRCUIT_OUTBOUND]["failures"] = 2

    class _EmptyLLM:
        def __init__(self, real):
            self._real = real
            self.outbound_empty_completion_count = 0
            self.outbound_failure_count = 0
            self.outbound_circuit_open_count = 0

        def __getattr__(self, item):
            return getattr(self._real, item)

        async def chat_structured(self, messages, tools, temperature=0.8, max_tokens=512):
            self.outbound_empty_completion_count += 1
            self.outbound_failure_count += 1
            return {"content": "", "tool_calls": []}

        async def chat(self, messages, temperature=0.8, max_tokens=512):
            self.outbound_empty_completion_count += 1
            return ""

    a = _loop(_EmptyLLM(c), patience=20.0)
    import anima.agent.react as _react
    orig = asyncio.sleep
    asyncio.sleep = _fake_sleep
    try:
        reply = await a.run([{"role": "user", "content": "在吗"}])
    finally:
        asyncio.sleep = orig
    assert reply in _NO_TOOL_FALLBACKS
    assert slept == [], "连续整链失败后不得再白等"


# ── 3. 工具超时：可配置 + 默认收紧 ───────────────────────────────────────

def test_tool_timeout_default_is_tightened():
    """默认值必须显著低于旧的 120s（102s 的 x_search 就是被 120s 放过去的）。"""
    assert _DEFAULT_TOOL_TIMEOUT_SEC == 45.0


def test_tool_timeout_is_overridable():
    a = _loop(_client(), patience=0.0, tool_timeout_sec=7.5)
    assert a.tool_timeout_sec == pytest.approx(7.5)


def test_tool_timeout_default_applied():
    a = _loop(_client(), patience=0.0)
    assert a.tool_timeout_sec == _DEFAULT_TOOL_TIMEOUT_SEC


def test_source_has_no_hardcoded_120_timeout():
    """源码不得再有硬编码 `timeout=120.0`（正是本次要拆掉的常量）。"""
    src = (_ROOT / "anima" / "agent" / "react.py").read_text(encoding="utf-8-sig")
    assert "timeout=120.0" not in src
    assert "timeout=self.tool_timeout_sec" in src


def test_tool_timeout_config_defaults_are_same_source():
    """配置默认、代码默认、出厂 yaml 三处必须同源（生产无该键 ⇒ 走代码默认）。"""
    import yaml

    from anima.core.config import ReactConfig

    assert ReactConfig().tool_timeout_sec == _DEFAULT_TOOL_TIMEOUT_SEC
    cfg = yaml.safe_load((_ROOT / "config" / "default.yaml").read_text(encoding="utf-8"))
    assert float(cfg["react"]["tool_timeout_sec"]) == _DEFAULT_TOOL_TIMEOUT_SEC


def test_generation_wires_tool_timeout():
    """`generation` 必须把配置传进 `ReActLoop`，否则生产覆盖不生效（静默失效）。"""
    src = (_ROOT / "anima" / "pipeline" / "stages" / "generation.py").read_text(
        encoding="utf-8-sig"
    )
    assert "tool_timeout_sec=_tool_timeout_sec(core)" in src


@pytest.mark.asyncio
async def test_slow_tool_is_cut_off_by_configured_timeout(rq):
    """行为：1s 超时必须立刻砍掉 5s 的慢工具，且超时文案反映真实秒数。"""
    calls = {"n": 0}

    class _SlowTools:
        def get_schemas(self, permission_level, sort_by_priority=True):
            return [{"type": "function",
                     "function": {"name": "slow", "parameters": {"type": "object", "properties": {}}}}]

        async def call(self, name, args, level):
            calls["n"] += 1
            await asyncio.sleep(5)
            return "never"

    class _ToolCallLLM:
        def __init__(self):
            self.n = 0
            self.outbound_empty_completion_count = 0
            self.outbound_failure_count = 0
            self.outbound_circuit_open_count = 0
            self.circuit_open_until = 0.0
            self.seen: list = []

        async def chat_structured(self, messages, tools, temperature=0.8, max_tokens=512):
            self.n += 1
            if self.n == 1:
                return {"content": "", "tool_calls": [
                    {"id": "c1", "function": {"name": "slow", "arguments": "{}"}}]}
            return {"content": "好了喵", "tool_calls": []}

        async def chat(self, messages, temperature=0.8, max_tokens=512):
            return "好了喵"

    llm = _ToolCallLLM()
    a = ReActLoop(llm, _SlowTools(), quality=rq, max_rounds=2, tool_timeout_sec=1.0)
    t0 = time.monotonic()
    reply = await a.run([{"role": "user", "content": "跑个慢工具"}])
    dt = time.monotonic() - t0
    assert calls["n"] == 1
    assert dt < 3.0, f"1s 超时应立刻砍掉 5s 慢工具，实测 {dt:.1f}s"
    assert reply == "好了喵"
    trace = a.tool_trace[0]
    assert "超时" in trace["result"] and "1" in trace["result"], trace["result"]
