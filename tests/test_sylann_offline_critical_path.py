# -*- coding: utf-8 -*-
"""sylann 观察移出关键路径（2026-10-04，stale-while-revalidate）。

问题：`cognition_stage` 的 **4–9 秒**几乎全部是 `sylann.observe()` 的一次 LLM 调用
（`cognition.py` 内唯一 await → `sylann_adapter.py:410` `engine.submit()`，
`_OBSERVE_TIMEOUT_SEC=15`）。而它产出的一切只用于一个语气片段
（`sylann_expression`，priority 40）与一个 quirk 布尔，**消费者都在同一 stage 内**。

🔴 方案成立的关键前提：`expression_hint()` 读的是 `self._last`
⇒ **本来就带滞后语义**，首次为 `None` 时返回 `""`。
所以「用上一份观察去注入」不是新引入的近似，而是它既有的契约。

行为契约（本判据逐条钉死）：
1. `hurt=True` ⇒ **必须同步等待**（保护语义最需要它的正是这类消息，不能滞后）；
2. `hurt=False` ⇒ **关键路径不得等**（用慢引擎断言耗时）；
3. 两条路径都要注入 hint（功能不回归）；
4. 后台任务抛异常 **不得** 影响主链路（fail-soft）；
5. 首次无 `_last` ⇒ 不写 `sylann_expression_hint`（与改前逐字节相同）；
6. `record_expression_hint(bool(hint))` 两条路径都要调用。
"""
import asyncio
import time

import pytest

from anima.pipeline.stages.cognition import _observe_sylann


class _FakeSylann:
    """最小替身：`observe` 可控延迟 + 可选抛错；`expression_hint` 读 `_last`（同真实语义）。"""

    def __init__(self, *, delay=0.0, last="上轮的语气提示", boom=False):
        self.delay = delay
        self._last = last
        self.boom = boom
        self.calls: list[dict] = []
        self.recorded: list[bool] = []
        self.submitted = asyncio.Event()

    async def observe(self, group_id, user_id, text, *, hurt=False,
                      message_id=None, values=None):
        self.calls.append({"hurt": hurt, "text": text, "message_id": message_id,
                            "values": values})
        if self.delay:
            await asyncio.sleep(self.delay)
        self.submitted.set()
        if self.boom:
            raise RuntimeError("engine 炸了")
        self._last = "本轮的语气提示"

    def expression_hint(self) -> str:
        return self._last or ""

    def record_expression_hint(self, applied: bool) -> None:
        self.recorded.append(bool(applied))


class _Ctx:
    def __init__(self, text: str):
        self.group_id = 727631526
        self.user_id = 1562290139
        self.text = text
        self.event = {"message_id": "m-1"}
        self.meta: dict = {}


async def _drain():
    """让后台任务有机会跑完（测试用，不是实现）。"""
    for _ in range(6):
        await asyncio.sleep(0.02)

# ── 1. hurt=True：必须同步等待 ──────────────────────────────────────

@pytest.mark.asyncio
async def test_hurt_waits_synchronously():
    s = _FakeSylann(delay=0.6)
    ctx = _Ctx("你好恶心")          # hurt_count=1 > playful=0
    t0 = time.monotonic()
    await _observe_sylann(s, ctx)
    dt = time.monotonic() - t0
    assert dt >= 0.5, f"hurt 消息必须同步等保护结果，实测 {dt*1000:.0f}ms"
    assert s.calls and s.calls[0]["hurt"] is True
    assert ctx.meta.get("sylann_expression_hint") == "本轮的语气提示", "同步分支必须用本轮结果"


# ── 2. hurt=False：关键路径不得等 ───────────────────────────────────

@pytest.mark.asyncio
async def test_non_hurt_does_not_block_critical_path():
    s = _FakeSylann(delay=1.5)
    ctx = _Ctx("在吗")               # 无 hurt 词
    t0 = time.monotonic()
    await _observe_sylann(s, ctx)
    dt = time.monotonic() - t0
    assert dt < 0.2, f"非 hurt 回合不得阻塞关键路径，实测 {dt*1000:.0f}ms"
    # 用上一份观察的 hint（既有滞后语义），而不是空
    assert ctx.meta.get("sylann_expression_hint") == "上轮的语气提示"


@pytest.mark.asyncio
async def test_non_hurt_still_refreshes_in_background():
    s = _FakeSylann(delay=0.3)
    ctx = _Ctx("在吗")
    await _observe_sylann(s, ctx)
    # 🔴 必须**等到事件真的置位**，不能靠固定 sleep 猜（否则测的是"睡够了没"不是"跑了没"）
    await asyncio.wait_for(s.submitted.wait(), timeout=2.0)
    assert s.calls and s.calls[0]["hurt"] is False


# ── 3. 两条路径都注入 hint + 记录 ───────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("text,hurt", [("你好恶心", True), ("在吗", False)])
async def test_hint_recorded_on_both_paths(text, hurt):
    s = _FakeSylann(delay=0.0)
    ctx = _Ctx(text)
    await _observe_sylann(s, ctx)
    assert s.recorded == [True], f"record_expression_hint 未被调用：{s.recorded}"
    assert ctx.meta.get("sylann_expression_hint")
    if not hurt:
        await _drain()


# ── 4. 后台异常 fail-soft ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_background_failure_does_not_propagate():
    s = _FakeSylann(delay=0.05, boom=True)
    ctx = _Ctx("在吗")
    await _observe_sylann(s, ctx)          # 不得抛
    await _drain()
    assert ctx.meta.get("sylann_expression_hint") == "上轮的语气提示", "失败时仍应用上一份"


@pytest.mark.asyncio
async def test_sync_failure_still_isolated():
    """同步分支的异常行为与改前一致：记日志、不外抛。"""
    s = _FakeSylann(delay=0.0, boom=True)
    ctx = _Ctx("你好恶心")
    await _observe_sylann(s, ctx)          # 不得抛
    assert "sylann_expression_hint" not in ctx.meta


# ── 5. 首次无 _last：不写 key（与改前逐字节相同）─────────────────────

@pytest.mark.asyncio
async def test_first_turn_without_last_writes_no_hint():
    s = _FakeSylann(delay=0.0, last="")
    ctx = _Ctx("在吗")
    await _observe_sylann(s, ctx)
    assert "sylann_expression_hint" not in ctx.meta
    assert s.recorded == [False]
    await _drain()


# ── 6. 传参完整性（values / message_id 不能因快路径丢失）────────────

@pytest.mark.asyncio
async def test_observe_receives_same_args_on_both_paths():
    for text, hurt in (("你好恶心", True), ("在吗", False)):
        s = _FakeSylann(delay=0.0)
        ctx = _Ctx(text)
        ctx.meta["desire_count"] = 3
        await _observe_sylann(s, ctx)
        if not hurt:
            await _drain()
        c = s.calls[0]
        assert c["message_id"] == "m-1", c
        assert c["values"]["desire_count"] == pytest.approx(0.3), c
        assert c["values"]["has_group_context"] == 1.0, c
        assert c["hurt"] is hurt, c


# ── 7. 无 sylann 时是 no-op ────────────────────────────────────────

@pytest.mark.asyncio
async def test_missing_sylann_is_noop():
    ctx = _Ctx("在吗")
    await _observe_sylann(None, ctx)        # 不得抛
    assert "sylann_expression_hint" not in ctx.meta
