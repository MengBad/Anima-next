"""P2：同群跨用户回合互斥（并发窗口）的 RED/GREEN 测试。

## 为什么需要它

生产事故（2026-09-21 15:00，`reports/2026-09-21_answer_mismatch_cross_user_context.md`）：
同群两条不同用户的消息相隔 4.1s 各自 @bot，`_turn_lock` / `_reply_inflight` 的 key
**都含 user_id** ⇒ 两个回合被**并发**处理；而 `_group_recent` 是**群级共享**、回复要到
最后一段（guardrail）才写回群缓冲。于是后处理的回合拿到的 messages 里，除了自己这条，
还带着一条「**别人**问 bot、且**尚未**被回复」的 user turn —— 两者同形、中间没有
assistant 隔断、也没有任何标记 ⇒ 模型被结构诱导，正文答了上一条（「答非所问」）。

09-21 的修复是**注入侧标注**（`[旁听·他人提问·非本轮]`，见
`tests/test_group_context_attribution.py`）—— 那是**缓解**：让模型不被结构骗到。
本文件守的是**根因**：同群回合根本不该并发。

## 设计要点

- 不动的：`_turn_lock(detail_type, group_id, user_id)` 仍按 (类型, 群/用户, 用户) 隔离
  —— `tests/test_conversation_assembler.py::test_handler_turn_lock_serializes_only_same_session`
  钉的是它的契约，本修复**不碰它**（上轮改它的 key 正是撞上那条测试的原因）。
- 新增的：`_group_turn_lock(group_id)` —— **只按群**取锁，`handle()` 在
  `detail_type == "group"` 时用它把 `_handle_inner` 串行化。
- 私聊**不串行**（不同用户本来就互不影响，串行只会白加延迟）。
- 不同群**不互相串行**（锁按群隔离）。

⚠️ `handle()` 在 `finally` 里 `await self._drain_coalesced(key)`，而后者会再进
`self.handle(synth)` ⇒ 群锁必须在**进入 finally 之前**释放，否则自锁死
（`asyncio.Lock` 不可重入）。测试 `test_group_lock_released_before_drain` 守这条。
"""
from __future__ import annotations

import asyncio
import logging
import types
from pathlib import Path

import pytest

from anima.llm.handler import LLMHandler

_ROOT = Path(__file__).resolve().parents[1]


def _code(rel: str) -> str:
    """剥注释后再断言 —— 解释性注释会把源码守卫变成假红/假绿。"""
    src = (_ROOT / rel).read_text(encoding="utf-8-sig")
    return "\n".join(ln.split("#")[0] for ln in src.splitlines())


def _bare_handler(inner) -> LLMHandler:
    """不跑 __init__ 的 Handler：只装本测试需要的那几个属性。"""
    h = object.__new__(LLMHandler)
    h.core = types.SimpleNamespace(audit=None)
    h._turn_locks = {}
    h._reply_inflight = set()
    h._coalesced = {}
    h._group_turn_locks = {}
    h._handle_inner = inner
    return h


def _group_ev(uid: int, gid: int = 100, text: str = "在吗", mentioned: bool = True) -> dict:
    return {
        "user_id": uid, "group_id": gid, "text": text, "message_id": uid,
        "mentions_self": mentioned,
        "raw": {"message_type": "group", "sender": {"nickname": "u%d" % uid}},
    }


def _private_ev(uid: int, text: str = "在吗") -> dict:
    return {
        "user_id": uid, "text": text, "message_id": uid,
        "raw": {"message_type": "private", "sender": {"nickname": "u%d" % uid}},
    }


def _overlap_probe():
    """返回 (inner, state)。inner 记录「同群回合是否被并发执行」。"""
    state = {"running": 0, "overlaps": [], "order": []}

    async def inner(event: dict) -> None:
        state["running"] += 1
        who = (event.get("group_id"), event.get("user_id"))
        if state["running"] > 1:
            state["overlaps"].append(who)
        state["order"].append("start%s" % (who,))
        await asyncio.sleep(0.03)
        state["order"].append("end%s" % (who,))
        state["running"] -= 1

    return inner, state


# ---------------------------------------------------------------------------
# 1. 根因：同群不同用户不得并发
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_same_group_different_users_do_not_overlap():
    """🔴 本修复的核心判据。旧实现下这条是**红的**（两回合并发进 `_handle_inner`）。"""
    inner, state = _overlap_probe()
    h = _bare_handler(inner)

    await asyncio.gather(
        h.handle(_group_ev(728536612)),
        h.handle(_group_ev(2038678896)),
    )

    assert state["overlaps"] == [], (
        "同群两个用户被并发处理 ⇒ 群级共享上下文会被跨用户污染；"
        "实际重叠: %r，执行序: %r" % (state["overlaps"], state["order"])
    )
    # 非平凡性：两个回合都必须真的跑过（不能被合并/吞掉）
    assert len([s for s in state["order"] if s.startswith("start")]) == 2, state["order"]


@pytest.mark.asyncio
async def test_group_lock_is_keyed_by_group_only():
    h = _bare_handler(_overlap_probe()[0])
    assert h._group_turn_lock(100) is h._group_turn_lock(100)
    assert h._group_turn_lock(100) is not h._group_turn_lock(200)


# ---------------------------------------------------------------------------
# 2. 不得过度串行（否则只是拿延迟换正确性）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_private_turns_still_run_concurrently():
    """私聊不同用户互不影响，串行化它们只是白加延迟。"""
    inner, state = _overlap_probe()
    h = _bare_handler(inner)

    await asyncio.gather(
        h.handle(_private_ev(42)),
        h.handle(_private_ev(43)),
    )

    assert state["overlaps"], "私聊被串行了 —— 本修复不应影响私聊并发"


@pytest.mark.asyncio
async def test_different_groups_run_concurrently():
    """锁按群隔离：A 群在途不应拖住 B 群（F6 的按群隔离纪律）。"""
    inner, state = _overlap_probe()
    h = _bare_handler(inner)

    await asyncio.gather(
        h.handle(_group_ev(1, gid=100)),
        h.handle(_group_ev(2, gid=200)),
    )

    assert state["overlaps"], "不同群之间被串行了 —— 群锁必须按群隔离"


# ---------------------------------------------------------------------------
# 3. 不得自锁死：群锁必须先于 _drain_coalesced 释放
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_lock_not_leaked_before_drain():
    """群锁必须在回合结束前释放，且吸收的后续@仍能被 drain 出去。

    若把 `async with` 写成包住整个 `try/finally`，`_drain_coalesced` → `handle()`
    会在**仍持有**群锁时再取同一把锁 ⇒ 永久排队（`asyncio.Lock` 不可重入）。
    """
    order = []

    async def inner(event: dict) -> None:
        order.append(event.get("text"))
        await asyncio.sleep(0.01)

    h = _bare_handler(inner)

    await asyncio.wait_for(h.handle(_group_ev(1, text="第一句")), timeout=2.0)
    assert not h._group_turn_lock(100).locked(), "回合结束后群锁仍被持有"

    # 先占住 inflight，让第二条被吸收；再手动 drain
    h._reply_inflight.add(("group", 100, 1))
    await h.handle(_group_ev(1, text="第二句"))
    assert h._coalesced.get(("group", 100, 1)), "第二条没被吸收 ⇒ 本测试失去意义"
    h._reply_inflight.discard(("group", 100, 1))

    await asyncio.wait_for(h._drain_coalesced(("group", 100, 1)), timeout=2.0)

    assert "第二句" in order, "吸收的后续@没能被 drain 出去（疑似群锁自锁死）"
    assert not h._group_turn_lock(100).locked(), "drain 后群锁泄漏"


# ---------------------------------------------------------------------------
# 4. 可观测性：发生排队必须留痕（否则运行期无法证伪「并发窗口」）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_contention_is_logged(caplog):
    """群锁被占用时打 INFO —— 这是本修复唯一的运行期证据面。"""
    async def inner(event: dict) -> None:
        await asyncio.sleep(0.05)

    h = _bare_handler(inner)

    with caplog.at_level(logging.INFO, logger="anima.llm.handler"):
        await asyncio.gather(
            h.handle(_group_ev(1)),
            h.handle(_group_ev(2)),
        )

    assert "同群串行" in caplog.text, f"排队没留痕: {caplog.text!r}"


@pytest.mark.asyncio
async def test_no_contention_no_log(caplog):
    """无竞争不刷日志（INFO 面不能被常态淹没）。"""
    async def inner(event: dict) -> None:
        await asyncio.sleep(0.005)

    h = _bare_handler(inner)

    with caplog.at_level(logging.INFO, logger="anima.llm.handler"):
        await h.handle(_group_ev(1))

    assert "同群串行" not in caplog.text


# ---------------------------------------------------------------------------
# 5. 源码级接线守卫：防未来重构把群锁摘掉
# ---------------------------------------------------------------------------

def test_handle_acquires_group_lock_for_group_events():
    code = _code("anima/llm/handler.py")
    assert code.count("_group_turn_lock(") >= 2, (
        "必须同时存在 `_group_turn_lock` 的定义与调用点"
    )
    assert "async with _group_lock" in code or "async with group_lock" in code, (
        "handle() 必须用 `async with` 持有群锁"
    )
    # 只对群生效：私聊不得取群锁
    assert 'detail_type == "group"' in code


def test_turn_lock_contract_untouched():
    """🔴 `_turn_lock` 的 key 结构不得改动 —— 它有自己的契约测试。"""
    code = _code("anima/llm/handler.py")
    assert 'key = (str(detail_type), int(group_id) if group_id is not None else None, int(user_id))' in code, (
        "`_turn_lock` 的 key 结构被改动 ⇒ test_handler_turn_lock_serializes_only_same_session 会红"
    )
