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

## 设计要点（C，2026-10-03 更新）

🔴 **锁的取得点已下移**：旧实现在 `handle()` 里用 `async with _group_lock` 包住
**整个** `_handle_inner` —— 连「群防抖等待 2s / 引用拉取 / 跨群观察 / owner 命令路由」
这些**与群级共享上下文无关**的前置工作也一起持锁，每条群消息白占群锁约 2s。
现在锁改在 `_handle_inner` 内、**群短时缓冲写入之前**取得，恰好覆盖：

    handler: observe_group_message(用户消息)   ← 写
        → memory_stage: get_recent_group_context  ← 读（组帧）
        → guardrail:  observe_group_bot_reply     ← 写回（助手）

前置工作移到锁外并发执行。**语义不变量不变**：同群不同用户的回合在关键段仍不并发。

- 不动的：`_turn_lock(detail_type, group_id, user_id)` 仍按 (类型, 群/用户, 用户) 隔离
  —— `tests/test_conversation_assembler.py::test_handler_turn_lock_serializes_only_same_session`
  钉的是它的契约，本修复**不碰它**。
- `_group_turn_lock(group_id)` —— **只按群**取锁，`_handle_inner` 在
  `detail_type == "group"` 时用它把回合关键段串行化。
- 私聊**不串行**（不同用户本来就互不影响，串行只会白加延迟）。
- 不同群**不互相串行**（锁按群隔离）。

⚠️ `handle()` 在 `finally` 里 `await self._drain_coalesced(key)`，而后者会再进
`self.handle(synth)` ⇒ 群锁必须在**进入 finally 之前**释放，否则自锁死
（`asyncio.Lock` 不可重入）。C 之后由 `_handle_inner` 内的 `async with` 保证在其
返回前释放；测试 `test_group_lock_released_before_drain` 守这条。

## 为什么测试直接跑真 `_handle_inner`

锁已在 `_handle_inner` 内部，**不能再**用「monkeypatch `_handle_inner` + 看它是否被
串行」来证伪（那会绕过锁本身）。因此本文件改为：装一个**最小但真实**的 handler
（沿用 `tests/test_reply_learning.py` 的核心桩），把 `pipeline.run` 换成会**记录重叠**
的探针 —— 探针正落在锁覆盖的区间内，于是「并发与否」可被直接观察。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from anima.llm.handler import LLMHandler

_ROOT = Path(__file__).resolve().parents[1]

OWNER = 10001
GROUP = 100
GROUP_B = 200


def _code(rel: str) -> str:
    """剥注释后再断言 —— 解释性注释会把源码守卫变成假红/假绿。"""
    src = (_ROOT / rel).read_text(encoding="utf-8-sig")
    return "\n".join(ln.split("#")[0] for ln in src.splitlines())


# ---------------------------------------------------------------------------
# 最小但真实的 handler + 会记录重叠的探针 pipeline
# ---------------------------------------------------------------------------

def _make_handler() -> LLMHandler:
    core = types.SimpleNamespace()
    core.config = types.SimpleNamespace(
        permissions=types.SimpleNamespace(owners=[OWNER]),
        persistence=types.SimpleNamespace(data_dir="data"),
        compat=types.SimpleNamespace(astrbot_enabled=False),
        crossgroup=types.SimpleNamespace(enabled=False),
        autonomy=types.SimpleNamespace(enabled=True, reply_only_when_mentioned=True),
        sticker=types.SimpleNamespace(
            enabled=False, auto_collect=False,
            collect_min_interval=0, always_atmosphere=[]),
        conversation=types.SimpleNamespace(enabled=False),
        reply_split=types.SimpleNamespace(enabled=False),
    )
    core.gateway = MagicMock()
    core.gateway.send = AsyncMock(return_value=12345)
    core.agent = None
    core.audit = None
    core.group_presence = types.SimpleNamespace(
        observe=lambda *a, **k: types.SimpleNamespace(state="quiet"))
    h = LLMHandler(core)
    # 反馈识别与本测试无关，且真实现会碰更多 core 面；整体短路。
    h._maybe_record_feedback = MagicMock(return_value=None)
    return h


class _OverlapPipeline:
    """探针：记录「同群回合是否在关键段被并发执行」。

    落在群锁覆盖区间内（`_handle_inner` 的 `async with` 里），因此可直接观察串行性。
    回合返回 `should_reply=False` 的 ctx ⇒ 收口于「无出站」分支，避开发送链路。
    """

    def __init__(self, state: dict, sleep: float = 0.03):
        self._state = state
        self._sleep = sleep

    async def run(self, ctx):
        st = self._state
        who = (ctx.group_id, ctx.user_id)
        st["running"] += 1
        if st["running"] > 1:
            st["overlaps"].append(who)
        st["order"].append("start%s" % (who,))
        st["texts"].append(ctx.text)
        await asyncio.sleep(self._sleep)
        st["order"].append("end%s" % (who,))
        st["running"] -= 1
        ctx.should_reply = False
        return ctx


def _probe_handler(sleep: float = 0.03):
    state = {"running": 0, "overlaps": [], "order": [], "texts": []}
    h = _make_handler()
    h.pipeline = _OverlapPipeline(state, sleep=sleep)
    return h, state


def _group_ev(uid: int, gid: int = GROUP, text: str = "在吗", mentioned: bool = True) -> dict:
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


# ---------------------------------------------------------------------------
# 1. 根因：同群不同用户不得并发（关键段）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_same_group_different_users_do_not_overlap():
    """🔴 本修复的核心判据。旧实现（锁在 handle() 之外、整段包裹）下同样为绿；
    本判据钉的是**新边界**：前置工作可并发，但「缓冲写入 → 组帧 → 出站」不得并发。
    """
    h, state = _probe_handler()

    await asyncio.gather(
        h.handle(_group_ev(728536612)),
        h.handle(_group_ev(2038678896)),
    )

    assert state["overlaps"] == [], (
        "同群两个用户在关键段被并发处理 ⇒ 群级共享上下文会被跨用户污染；"
        "实际重叠: %r，执行序: %r" % (state["overlaps"], state["order"])
    )
    # 非平凡性：两个回合都必须真的跑过（不能被合并/吞掉）
    assert len([s for s in state["order"] if s.startswith("start")]) == 2, state["order"]


@pytest.mark.asyncio
async def test_group_lock_is_keyed_by_group_only():
    h = _make_handler()
    assert h._group_turn_lock(GROUP) is h._group_turn_lock(GROUP)
    assert h._group_turn_lock(GROUP) is not h._group_turn_lock(GROUP_B)


# ---------------------------------------------------------------------------
# 2. 不得过度串行（否则只是拿延迟换正确性）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_private_turns_still_run_concurrently():
    """私聊不同用户互不影响，串行化它们只是白加延迟。"""
    h, state = _probe_handler()

    await asyncio.gather(
        h.handle(_private_ev(42)),
        h.handle(_private_ev(43)),
    )

    assert state["overlaps"], "私聊被串行了 —— 本修复不应影响私聊并发"


@pytest.mark.asyncio
async def test_different_groups_run_concurrently():
    """锁按群隔离：A 群在途不应拖住 B 群（F6 的按群隔离纪律）。"""
    h, state = _probe_handler()

    await asyncio.gather(
        h.handle(_group_ev(1, gid=GROUP)),
        h.handle(_group_ev(2, gid=GROUP_B)),
    )

    assert state["overlaps"], "不同群之间被串行了 —— 群锁必须按群隔离"


# ---------------------------------------------------------------------------
# 3. 不得自锁死：群锁必须先于 _drain_coalesced 释放
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_lock_released_before_drain():
    """群锁必须在回合结束前释放，且吸收的后续@仍能被 drain 出去。

    若把 `async with` 写成包住整个 `try/finally`，`_drain_coalesced` → `handle()`
    会在**仍持有**群锁时再取同一把锁 ⇒ 永久排队（`asyncio.Lock` 不可重入）。
    """
    h, state = _probe_handler()

    await asyncio.wait_for(h.handle(_group_ev(1, text="第一句")), timeout=2.0)
    assert not h._group_turn_lock(GROUP).locked(), "回合结束后群锁仍被持有"

    # 先占住 inflight，让第二条被吸收；再手动 drain
    h._reply_inflight.add(("group", GROUP, 1))
    await h.handle(_group_ev(1, text="第二句"))
    assert h._coalesced.get(("group", GROUP, 1)), "第二条没被吸收 ⇒ 本测试失去意义"
    h._reply_inflight.discard(("group", GROUP, 1))

    await asyncio.wait_for(h._drain_coalesced(("group", GROUP, 1)), timeout=2.0)

    assert any("第二句" in (t or "") for t in state["texts"]), (
        "吸收的后续@没能被 drain 出去（疑似群锁自锁死）"
    )
    assert not h._group_turn_lock(GROUP).locked(), "drain 后群锁泄漏"


# ---------------------------------------------------------------------------
# 3b. C 的收益面：前置工作不再被别的回合的群锁挡住
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prework_not_blocked_by_other_turns_lock():
    """🔴 C（2026-10-03）的直接判据。

    旧实现把整段 `_handle_inner`（含前置）包在群锁里 ⇒ T2 的**防抖等待**要等 T1
    整个回合结束才能跑。C 之后前置在锁外并发：T1 仍持群锁时，T2 的防抖必须已跑完。
    """
    h, _ = _probe_handler(sleep=0.20)          # T1 回合足够长，稳定持锁
    order: list[str] = []

    class _Deb:
        async def ingest(self, event):
            if event.get("message_id") == 2:
                order.append("t2_prework_in")
                await asyncio.sleep(0.02)
                order.append("t2_prework_out")
            return event

    h._group_debouncer = _Deb()

    t1 = asyncio.create_task(h.handle(_group_ev(1, text="A")))
    for _ in range(500):
        if h._group_turn_lock(GROUP).locked():
            break
        await asyncio.sleep(0.001)
    assert h._group_turn_lock(GROUP).locked(), "T1 未进入关键段 ⇒ 测试前提不成立"

    t2 = asyncio.create_task(h.handle(_group_ev(2, text="B")))
    await asyncio.sleep(0.06)                  # T2 前置（0.02s）早该完成
    assert order == ["t2_prework_in", "t2_prework_out"], (
        "T2 的前置工作被 T1 的群锁挡住了 ⇒ C 未生效（前置仍在锁内）"
    )
    assert h._group_turn_lock(GROUP).locked(), "T1 应仍在关键段（前提）"

    await asyncio.gather(t1, t2)


@pytest.mark.asyncio
async def test_merged_message_never_enters_locked_turn():
    """被防抖合并掉的消息根本不进关键段 —— 不占群锁、不进 pipeline。"""
    h, state = _probe_handler()

    class _Deb:
        async def ingest(self, event):
            return None                        # 被吸收合并

    h._group_debouncer = _Deb()

    await asyncio.wait_for(h.handle(_group_ev(1)), timeout=2.0)

    assert state["order"] == [], "被合并的消息不该进 pipeline（更不该占群锁）"
    assert not h._group_turn_lock(GROUP).locked()


# ---------------------------------------------------------------------------
# 4. 可观测性：发生排队必须留痕（否则运行期无法证伪「并发窗口」）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_contention_is_logged(caplog):
    """群锁被占用时打 INFO —— 这是本修复唯一的运行期证据面。"""
    h, _ = _probe_handler()

    with caplog.at_level(logging.INFO, logger="anima.llm.handler"):
        await asyncio.gather(
            h.handle(_group_ev(1)),
            h.handle(_group_ev(2)),
        )

    assert "同群串行" in caplog.text, f"排队没留痕: {caplog.text!r}"


@pytest.mark.asyncio
async def test_no_contention_no_log(caplog):
    """无竞争不刷日志（INFO 面不能被常态淹没）。"""
    h, _ = _probe_handler()

    with caplog.at_level(logging.INFO, logger="anima.llm.handler"):
        await h.handle(_group_ev(1))

    assert "同群串行" not in caplog.text


# ---------------------------------------------------------------------------
# 5. 源码级接线守卫：防未来重构把群锁摘掉 / 又搬回 handle()
# ---------------------------------------------------------------------------

def test_group_lock_acquired_inside_handle_inner_for_group_events():
    code = _code("anima/llm/handler.py")
    assert code.count("_group_turn_lock(") >= 2, (
        "必须同时存在 `_group_turn_lock` 的定义与调用点"
    )
    assert "async with (_group_lock" in code, (
        "回合关键段必须用 `async with` 持有群锁"
    )
    # 只对群生效：私聊不得取群锁
    assert 'detail_type == "group"' in code

    # 🔴 C（2026-10-03）：锁**必须**在 `_handle_inner` 内取得，且 `handle()` 不再自己取锁。
    inner = inspect.getsource(LLMHandler._handle_inner)
    assert "_group_turn_lock(" in inner, (
        "群锁必须在 `_handle_inner` 内取得（C：锁已下移到回合关键段）"
    )
    assert "async with (_group_lock" in "".join(
        ln.split("#")[0] for ln in inner.splitlines()), (
        "`_handle_inner` 必须用 `async with` 持有群锁"
    )
    outer = "".join(ln.split("#")[0] for ln in inspect.getsource(LLMHandler.handle).splitlines())
    assert "_group_turn_lock(" not in outer, (
        "`handle()` 不应再自己取群锁（C：锁已下移，避免前置工作白占锁）"
    )


def test_turn_lock_contract_untouched():
    """🔴 `_turn_lock` 的 key 结构不得改动 —— 它有自己的契约测试。"""
    code = _code("anima/llm/handler.py")
    assert 'key = (str(detail_type), int(group_id) if group_id is not None else None, int(user_id))' in code, (
        "`_turn_lock` 的 key 结构被改动 ⇒ test_handler_turn_lock_serializes_only_same_session 会红"
    )
