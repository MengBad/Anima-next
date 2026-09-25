"""归因必须只认「出站正文调用」的上游故障（2026-09-24）。

背景
----
09-23 把归因从单计数扩成三计数（200-空 / 传输 / 熔断），但判据读的仍是 **进程级** 计数增量。
09-24 在生产复查时实证：**旁路 LLM 调用同样推高这些计数** ——

    17:04:07  闸门 `skip reason=unfamiliar` + `Pipeline 返回 STOP`（本 turn 不生成正文）
    17:04:09  三个候选 200-空 …
    17:04:15  `LLM 请求失败：empty_completion`        ← 实为旁路（跨群提取／表情判定）的调用

若同一窗口恰好有一个 turn 的正文为空，`_upstream_went_silent()` 就会把它记成
`fallback_upstream_empty`（本该 `fallback_no_tool`），并顺带触发挽救、多等一次预算。
用户可见影响很小，**失真的是遥测** —— 而分桶的全部意义就是让审计读得对。

判据因此收窄：只统计**显式标记为出站**的调用（`llm_scope(..., outbound=True)`）；
旁路调用（有 scope 的、以及完全没有 scope 的）一律不进判据。

本文件钉住三件事：
1. 判据读的是 outbound 计数，且默认 scope **不是**出站（fail-closed）；
2. llm 层真的按标记分流（`empty_completion` / `failure` / `circuit_open` 三处都分流）；
3. react 的三个出站调用点都带标记，缺一即回到被污染的老行为。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from anima.agent.react import (
    ReActLoop,
    _LLM_FAULT_COUNTERS,
    _NO_TOOL_FALLBACKS,
)
from anima.cognitive.reply_quality import ReplyQuality
from anima.llm import LLMClient
from anima.llm.usage import current_call_info, llm_scope

_ROOT = Path(__file__).resolve().parents[1]

_OUTBOUND_COUNTERS = (
    "outbound_empty_completion_count",
    "outbound_failure_count",
    "outbound_circuit_open_count",
)


# ── 桩件 ─────────────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):  # noqa: D105
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return ""


class _FakeSession:
    """只喂一个固定响应体，用来驱动 `_try_post` 的分类分支。"""

    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self._status = status

    def post(self, *args, **kwargs):  # noqa: D102
        return _FakeResponse(self._payload, self._status)


_EMPTY_COMPLETION_BODY = {"choices": []}


@pytest.fixture()
def rq(tmp_path) -> ReplyQuality:
    return ReplyQuality(str(tmp_path))


@pytest.fixture()
def llm() -> LLMClient:
    client = LLMClient("http://example.invalid", "test-key", model="m")
    client._session = _FakeSession(_EMPTY_COMPLETION_BODY)
    return client


def _agent(quality, *, baseline, **counters) -> ReActLoop:
    """只验归因的轻量构造（不跑 run()）。"""
    a = ReActLoop.__new__(ReActLoop)
    a.tool_trace = []
    a.quality = quality
    counts = {name: 0 for name in _OUTBOUND_COUNTERS}
    counts.update(counters)
    a.llm = type("L", (), counts)()
    if baseline is not None:
        a._llm_fault_baseline = baseline
    return a


# ── 1. 判据面：只认 outbound 三项 ────────────────────────────────────────

def test_attribution_counters_are_the_outbound_triple():
    assert _LLM_FAULT_COUNTERS == _OUTBOUND_COUNTERS


def test_outbound_counters_are_exported_by_llm_health():
    """改名/漏导出会让归因**静默降级**回旧行为，必须源码级钉住。"""
    src = (_ROOT / "anima" / "llm" / "__init__.py").read_text(encoding="utf-8-sig")
    for name in _OUTBOUND_COUNTERS:
        assert f'"{name}": self.{name}' in src, f"health() 未导出 {name}"


def test_default_scope_is_not_outbound():
    """没有任何 scope 时**不得**被当成出站（否则旁路默认值就把判据污染了）。"""
    assert current_call_info().outbound is False


def test_llm_scope_outbound_flag_round_trips():
    assert current_call_info().outbound is False
    with llm_scope("chat", "react", outbound=True):
        assert current_call_info().outbound is True
    assert current_call_info().outbound is False, "退出后必须复位"


# ── 2. llm 层：三处计数都按标记分流 ──────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_completion_counter_splits_by_outbound_flag(llm):
    with llm_scope("chat", "react", outbound=True):
        await llm._try_post({"model": "m"}, {})
    assert llm.empty_completion_count == 1
    assert llm.outbound_empty_completion_count == 1

    # 旁路：有 scope 但不是出站
    with llm_scope("exploration", "curiosity"):
        await llm._try_post({"model": "m"}, {})
    assert llm.empty_completion_count == 2
    assert llm.outbound_empty_completion_count == 1, "旁路不得进入出站计数"

    # 旁路：完全没有 scope（生产里 crossgroup/stickers 等正是这种）
    await llm._try_post({"model": "m"}, {})
    assert llm.empty_completion_count == 3
    assert llm.outbound_empty_completion_count == 1, "无 scope 默认不是出站"


@pytest.mark.asyncio
async def test_failure_counter_splits_by_outbound_flag(monkeypatch):
    client = LLMClient("http://example.invalid", "k", model="m")

    async def _always_none(payload, headers):
        client.last_status = None
        client._set_error("transport:ClientOSError")
        return None

    monkeypatch.setattr(client, "_try_post", _always_none)

    with llm_scope("chat", "chat", outbound=True):
        await client._post({"model": "m", "messages": []}, {})
    assert client.failure_count == 1
    assert client.outbound_failure_count == 1

    with llm_scope("stickers", "judge"):
        await client._post({"model": "m", "messages": []}, {})
    assert client.failure_count == 2
    assert client.outbound_failure_count == 1, "旁路传输故障不得进入出站计数"


@pytest.mark.asyncio
async def test_circuit_open_counter_splits_by_outbound_flag(monkeypatch):
    client = LLMClient("http://example.invalid", "k", model="m",
                       circuit_failure_threshold=1)

    async def _always_none(payload, headers):
        client.last_status = None
        client._set_error("transport:ServerDisconnectedError")
        return None

    monkeypatch.setattr(client, "_try_post", _always_none)

    with llm_scope("chat", "react_finalize", outbound=True):
        await client._post({"model": "m", "messages": []}, {})
    assert client.circuit_open_count == 1
    assert client.outbound_circuit_open_count == 1


# ── 3. react 层：旁路污染不再改变归因，也不再触发挽救 ────────────────────

def test_bypass_silence_does_not_count_as_upstream(rq):
    """旁路把**全量**计数推高，不构成「本 turn 上游静默」。"""
    a = _agent(rq, baseline=(0, 0, 0))
    a.llm.empty_completion_count = 6          # 旁路贡献的增量
    a.llm.failure_count = 2
    assert a._upstream_went_silent() is False
    assert a._upstream_went_empty() is False


def test_bypass_silence_keeps_the_no_tool_bucket(rq):
    """端到端：上游计数被旁路推高，但正文为空的原因是模型自己没话说 ⇒ 仍记 no_tool。"""
    a = _agent(rq, baseline=(0, 0, 0))
    a.llm.empty_completion_count = 9
    reply = a._empty_content_fallback()
    assert reply in _NO_TOOL_FALLBACKS
    rep = rq.report()
    assert rep["fallback_no_tool"] == 1
    assert rep["fallback_upstream_empty"] == 0, "旁路污染不得把桶翻成 upstream_empty"


def test_outbound_silence_still_attributed(rq):
    """反向守卫：真正的出站静默（outbound 增量）必须照旧被归因。"""
    a = _agent(rq, baseline=(0, 0, 0), outbound_empty_completion_count=3)
    assert a._upstream_went_silent() is True
    assert a._upstream_went_empty() is True
    a._empty_content_fallback()
    assert rq.report()["fallback_upstream_empty"] == 1


def test_missing_outbound_counter_still_fails_closed(rq):
    """任一 outbound 计数读不到 ⇒ 不宣称上游故障（老版本 llm / 未迁移替身）。"""
    a = _agent(rq, baseline=(0, 0, 0))
    a.llm = type("L", (), {"outbound_empty_completion_count": 9,
                          "outbound_circuit_open_count": 0})()  # 缺 failure
    assert a._upstream_went_silent() is False


# ── 4. 源码级：接线不许漏 ────────────────────────────────────────────────

def test_react_marks_all_three_outbound_calls():
    """三个出站调用点（纯对话重试 / ReAct 轮次 / 触顶收尾）缺一即回到被污染的老行为。"""
    src = (_ROOT / "anima" / "agent" / "react.py").read_text(encoding="utf-8-sig")
    code = re.sub(r'"""[\s\S]*?"""', "", src)  # 先剥 docstring
    code = "\n".join(line.split("#")[0] for line in code.splitlines())  # 再剥注释
    assert code.count("outbound=True") == 3, (
        "出站 LLM 调用必须逐个显式标记；漏标一处，该路径的上游故障就会被旁路计数淹没"
    )
    for operation in ("llm_scope(\"chat\", \"chat\", outbound=True)",
                      "llm_scope(\"chat\", \"react\", outbound=True)",
                      "llm_scope(\"chat\", \"react_finalize\", outbound=True)"):
        assert operation in code, f"缺少出站标记：{operation}"


def test_llm_increments_outbound_counter_next_to_full_counter():
    """分流必须紧挨着原计数增量，避免两处口径各自漂移。

    ⚠️ 必须**限定在函数体内**再定位：`self.failure_count += 1` 在生图路径另有 4 处，
    用 `src.index` 取全局首次出现会定位到那里（生图与出站正文归因无关，也刻意不分流）。
    """
    src = (_ROOT / "anima" / "llm" / "__init__.py").read_text(encoding="utf-8-sig")
    post_body = src.split("async def _post(")[1].split("async def _record_usage")[0]
    for full, outbound in (
        ("self.failure_count += 1", "self.outbound_failure_count += 1"),
        ("self.circuit_open_count += 1", "self.outbound_circuit_open_count += 1"),
    ):
        assert full in post_body and outbound in post_body, f"缺少计数分流：{outbound}"
        gap = post_body.index(outbound) - post_body.index(full)
        assert 0 < gap < 400, f"{outbound} 离原计数太远（gap={gap}），口径容易漂移"

    try_body = src.split("async def _try_post(")[1]
    assert "self.empty_completion_count += 1" in try_body
    assert "self.outbound_empty_completion_count += 1" in try_body

    # 分流必须由 outbound 标记驱动，不能换别的判据
    stripped = "\n".join(line.split("#")[0] for line in src.splitlines())
    assert stripped.count("current_call_info().outbound") >= 3
