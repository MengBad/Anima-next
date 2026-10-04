"""ReAct 整回合墙钟预算（2026-10-03）。

单次工具超时（``react.tool_timeout_sec``，45s）只挡住「**一个**工具卡死」，
挡不住「**多轮**工具 + 多次 LLM 往返」的**累计**。10-03 实测 ``x_search`` 回合
单次耗 102s，期间同群串行锁被持住 ⇒ 同群其它用户排队（白等 2 分钟、零回复）。

修法：给整回合加墙钟预算 ``react.turn_deadline_sec``。超预算时**不再发起新一轮
工具**，带**已得到的工具结果**收口作答（保留「能回答」，砍掉「无限等」），
从而给单回合持锁时间一个硬上界。

默认 **0.0 = 关闭**（不改变既有行为）——与 ``upstream_patience_sec`` 同一策略：
生产 config 无该键走代码默认，要启用请显式配置。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from anima.agent.react import ReActLoop, _DEFAULT_TURN_DEADLINE_SEC

_ROOT = Path(__file__).resolve().parents[1]


# ── 桩件 ─────────────────────────────────────────────────────────────────

class _StubTools:
    def get_schemas(self, permission_level, sort_by_priority=True):
        return []


class _ToolCallLLM:
    """主循环（带 tools）每轮返回一个**参数递增**的工具调用（避开反空转守卫）；
    收口（不带 tools）返回固定正文。"""

    def __init__(self, content: str = "收口答案"):
        self.calls = 0
        self._content = content
        self.outbound_empty_completion_count = 0

    async def chat_structured(self, messages, tools, temperature=0.8, max_tokens=512):
        self.calls += 1
        if tools:
            return {"content": "", "tool_calls": [{
                "id": f"c{self.calls}",
                "function": {"name": "slow", "arguments": json.dumps({"q": self.calls})},
            }]}
        return {"content": self._content, "tool_calls": []}

    async def chat(self, messages, temperature=0.8, max_tokens=512):
        self.calls += 1
        return self._content


class _SleepyTools:
    def __init__(self, delay: float):
        self.n = 0
        self._delay = delay

    def get_schemas(self, permission_level, sort_by_priority=True):
        return [{"type": "function",
                 "function": {"name": "slow", "parameters": {"type": "object", "properties": {}}}}]

    async def call(self, name, args, level):
        self.n += 1
        await asyncio.sleep(self._delay)
        return "tool-result"


def _loop(llm, *, turn_deadline_sec=None, tools=None) -> ReActLoop:
    kw = {}
    if turn_deadline_sec is not None:
        kw["turn_deadline_sec"] = turn_deadline_sec
    return ReActLoop(llm, tools or _StubTools(), upstream_patience_sec=0.0, **kw)


# ── 1. 默认关闭 + 可配 + 三处同源 ────────────────────────────────────────

def test_turn_deadline_default_is_off():
    """默认必须为 0（关闭）——新键不得在未显式启用时改变行为。"""
    assert _DEFAULT_TURN_DEADLINE_SEC == 0.0


def test_turn_deadline_is_overridable():
    a = _loop(_ToolCallLLM(), turn_deadline_sec=90.0)
    assert a.turn_deadline_sec == pytest.approx(90.0)


def test_turn_deadline_default_applied():
    a = _loop(_ToolCallLLM())
    assert a.turn_deadline_sec == _DEFAULT_TURN_DEADLINE_SEC


def test_config_default_matches_code_default():
    from anima.core.config import ReactConfig

    assert ReactConfig().turn_deadline_sec == _DEFAULT_TURN_DEADLINE_SEC


def test_default_yaml_has_turn_deadline():
    import yaml

    cfg = yaml.safe_load((_ROOT / "config" / "default.yaml").read_text(encoding="utf-8"))
    assert float(cfg["react"]["turn_deadline_sec"]) == _DEFAULT_TURN_DEADLINE_SEC


def test_generation_wires_turn_deadline():
    """`generation` 必须把配置传进 `ReActLoop`，否则生产覆盖静默失效。"""
    src = (_ROOT / "anima" / "pipeline" / "stages" / "generation.py").read_text(
        encoding="utf-8-sig"
    )
    assert "turn_deadline_sec=_turn_deadline_sec(core)" in src


# ── 2. 判据函数 ──────────────────────────────────────────────────────────

def test_deadline_off_never_exceeds():
    a = _loop(_ToolCallLLM())
    a._turn_start = time.monotonic() - 10_000
    assert a._turn_deadline_exceeded() is False


def test_deadline_exceeded_reads_elapsed():
    a = _loop(_ToolCallLLM(), turn_deadline_sec=10.0)
    a._turn_start = time.monotonic() - 11.0
    assert a._turn_deadline_exceeded() is True
    a._turn_start = time.monotonic()
    assert a._turn_deadline_exceeded() is False


def test_deadline_exceeded_is_safe_without_turn_start():
    a = _loop(_ToolCallLLM(), turn_deadline_sec=10.0)
    a._turn_start = None
    assert a._turn_deadline_exceeded() is False


# ── 3. 端到端行为 ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_cuts_off_when_turn_deadline_exceeded():
    """超预算 ⇒ 不再发起新一轮工具，带已得结果收口作答（且不记为触顶）。"""
    tools = _SleepyTools(delay=0.15)
    llm = _ToolCallLLM(content="收口答案")
    a = _loop(llm, turn_deadline_sec=0.05, tools=tools)

    reply = await a.run([{"role": "user", "content": "查一下 X"}])

    assert reply == "收口答案", f"应带已得结果收口，实得 {reply!r}"
    assert a._hit_turn_deadline is True
    assert tools.n < 5, "超时后不得再跑满全部轮次"
    assert a._hit_max_rounds is False, "整回合超时不得误报为触顶"


@pytest.mark.asyncio
async def test_run_without_deadline_runs_all_rounds():
    """反向守卫：未设预算（0）时行为不变 —— 跑满轮次、记为触顶、不误标超时。"""
    tools = _SleepyTools(delay=0.0)
    llm = _ToolCallLLM(content="收口答案")
    a = _loop(llm, tools=tools)  # 默认 turn_deadline_sec=0

    reply = await a.run([{"role": "user", "content": "查一下 X"}])

    assert reply == "收口答案"
    assert a._hit_turn_deadline is False
    assert a._hit_max_rounds is True
    assert tools.n == 5, f"关闭预算时应跑满 max_rounds，实得 {tools.n}"


def test_source_no_hardcoded_deadline():
    """必须走可配属性，不得硬编码。"""
    src = (_ROOT / "anima" / "agent" / "react.py").read_text(encoding="utf-8-sig")
    assert "self.turn_deadline_sec" in src
    assert "_turn_deadline_exceeded" in src
