"""被动回复效果自学习 —— 记录「开口之后有没有人接」，归纳成经验事实。

## 为什么要它

`_active_feedback_pending`（`core/autonomy.py`）已经给**主动发言**建了完整回路：
挂 pending → 窗口内有人接记 engaged / 无人理记 ignored → Beta 平滑 → 降频/冷落。
但**被动回复**（被叫到、熟人插话、唤醒窗内接话）**零采集点** —— 闸门
（`llm/group_gate.py`）只读**静态** `familiarity` 阈值，开口效果永不回流。

本模块补的就是这条断线，且**严格分两步**：

* **B1（本次）＝ 只采集**：挂 pending、记 engaged/ignored、落盘、归纳、注入。
  **不碰闸门、不改任何开口行为** ⇒ 零行为风险。
* **B2（未做）**：等真数字出来，再决定 `familiar_min` 要不要动态化。

## 一条硬约束：写「经验事实」，不写「社交策略」

`cognitive/learning.py` 的 `ruminate()` prompt 里硬编码着
「**绝不写社交策略分析**」（防「分析腔」露馅，与 09-28 人视角审计的露馅清单同源）。
那条禁令**是对的**，所以这里**绕开它、不推翻它**：

* ✅ 允许：「被叫到时开口，12 次里 9 次有人接着聊」
* ❌ 禁止：「我应该更克制，收敛插话频率」

`fragment()` 内置 fail-closed 过滤：命中策略腔的笔记**整条不注入**。
"""

from __future__ import annotations

import time
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from anima.cognitive.reply_learning import (
    ReplyLearning, close_reply_learning, get_reply_learning,
)

GROUP = 727631526
GROUP_B = 552994229
U = 10001


@pytest.fixture()
def engine(tmp_path):
    close_reply_learning()
    yield ReplyLearning(str(tmp_path), window_sec=300, records_max=50,
                        inject_top_k=3, min_samples=2)
    close_reply_learning()


# ----------------------------------------------------------------------
# 采集：挂 pending / 记账 / 过期
# ----------------------------------------------------------------------

def test_mark_reply_creates_pending(engine):
    engine.mark_reply(GROUP, trigger="at", message_id="m1", now=1000.0)
    assert engine.pending_count() == 1
    assert engine.stats(GROUP)["engaged"] == 0
    assert engine.stats(GROUP)["ignored"] == 0


def test_observe_within_window_counts_engaged(engine):
    engine.mark_reply(GROUP, trigger="at", now=1000.0)
    engine.observe(GROUP, now=1000.0 + 299)
    st = engine.stats(GROUP)
    assert st["engaged"] == 1 and st["ignored"] == 0
    assert engine.pending_count() == 0, "结算后 pending 必须清掉，防重复计数"


def test_observe_after_window_is_noop(engine):
    """窗口外的群消息不是「接话」——那是过期结算的地盘。"""
    engine.mark_reply(GROUP, trigger="at", now=1000.0)
    engine.observe(GROUP, now=1000.0 + 301)
    st = engine.stats(GROUP)
    assert st["engaged"] == 0 and st["ignored"] == 0


def test_expire_after_window_counts_ignored(engine):
    engine.mark_reply(GROUP, trigger="at", now=1000.0)
    engine.expire(now=1000.0 + 301)
    st = engine.stats(GROUP)
    assert st["ignored"] == 1 and st["engaged"] == 0
    assert engine.pending_count() == 0


def test_expire_inside_window_is_noop(engine):
    engine.mark_reply(GROUP, trigger="at", now=1000.0)
    engine.expire(now=1000.0 + 299)
    st = engine.stats(GROUP)
    assert st["ignored"] == 0 and st["engaged"] == 0
    assert engine.pending_count() == 1, "窗口内不该被结算掉"


def test_groups_are_isolated(engine):
    engine.mark_reply(GROUP, trigger="at", now=1000.0)
    engine.observe(GROUP_B, now=1000.0 + 10)      # 别的群说话不算 A 群被接话
    assert engine.stats(GROUP)["engaged"] == 0
    engine.observe(GROUP, now=1000.0 + 20)
    assert engine.stats(GROUP)["engaged"] == 1


def test_trigger_buckets_are_tracked(engine):
    """「被叫到时」与「其他情况插话」的经验必须分开记 —— 这是学习的主要信号。"""
    engine.mark_reply(GROUP, trigger="at", now=1000.0)
    engine.observe(GROUP, now=1010.0)
    engine.mark_reply(GROUP, trigger="other", now=2000.0)
    engine.expire(now=2400.0)

    by = engine.stats(GROUP)["by_trigger"]
    assert by["at"]["engaged"] == 1
    assert by["other"]["ignored"] == 1


def test_unknown_trigger_falls_back_to_other(engine):
    """trigger 走白名单，防任意字符串撑爆磁盘。"""
    engine.mark_reply(GROUP, trigger="~~malicious~~", now=1000.0)
    engine.observe(GROUP, now=1010.0)
    assert set(engine.stats(GROUP)["by_trigger"]) <= {"at", "other"}


def test_new_reply_settles_previous_pending_as_engaged(engine):
    """连回两条：第一条的窗口内既然又来了消息，第一条就该算被接话。"""
    engine.mark_reply(GROUP, trigger="at", now=1000.0)
    engine.mark_reply(GROUP, trigger="at", now=1100.0)
    assert engine.stats(GROUP)["engaged"] == 1, "旧 pending 被新消息接住，应结算"
    assert engine.pending_count() == 1, "当前只剩新的那条 pending"


def test_persist_and_reload(tmp_path):
    e1 = ReplyLearning(str(tmp_path), window_sec=300)
    e1.mark_reply(GROUP, trigger="at", now=1000.0)
    e1.observe(GROUP, now=1010.0)

    e2 = ReplyLearning(str(tmp_path), window_sec=300)
    assert e2.stats(GROUP)["engaged"] == 1, "统计必须跨进程存活"


# ----------------------------------------------------------------------
# 归纳：门控 + 只写经验事实
# ----------------------------------------------------------------------

def test_summarize_due_groups_requires_min_samples(engine):
    engine.mark_reply(GROUP, trigger="at", now=1000.0)
    engine.observe(GROUP, now=1010.0)
    assert engine.summarize_due_groups() == [], "样本不足 min_samples（=2）不该归纳"

    engine.mark_reply(GROUP, trigger="at", now=2000.0)
    engine.observe(GROUP, now=2010.0)
    assert engine.summarize_due_groups() == [GROUP]


def test_summarize_is_not_repeated_for_same_sample_count(engine):
    for i in range(2):
        engine.mark_reply(GROUP, trigger="at", now=1000.0 + i * 100)
        engine.observe(GROUP, now=1010.0 + i * 100)
    assert engine.summarize_due_groups() == [GROUP]
    engine._data["summarized"][str(GROUP)] = engine._samples(GROUP)   # 模拟已归纳
    assert engine.summarize_due_groups() == [], "同一批样本不该反复花钱归纳"


@pytest.mark.asyncio
async def test_summarize_writes_note(engine):
    for i in range(2):
        engine.mark_reply(GROUP, trigger="at", now=1000.0 + i * 100)
        engine.observe(GROUP, now=1010.0 + i * 100)

    llm = MagicMock()
    llm.chat = AsyncMock(return_value='{"note": "被叫到时开口，群里都有人接着聊"}')
    note = await engine.summarize(llm, GROUP)

    assert note == "被叫到时开口，群里都有人接着聊"
    assert engine.notes(GROUP)[-1]["text"] == note


@pytest.mark.asyncio
async def test_summarize_prompt_forbids_strategy_talk(engine):
    """禁令必须写在 prompt 里 —— 否则归纳会退化成自我解剖。"""
    for i in range(2):
        engine.mark_reply(GROUP, trigger="at", now=1000.0 + i * 100)
        engine.observe(GROUP, now=1010.0 + i * 100)

    llm = MagicMock()
    llm.chat = AsyncMock(return_value='{"note": "x"}')
    await engine.summarize(llm, GROUP)

    prompt = llm.chat.await_args.args[0][0]["content"]
    assert "不" in prompt
    for kw in ("我应该", "策略", "建议"):
        assert kw in prompt, f"prompt 必须明确禁止「{kw}」类输出"


@pytest.mark.asyncio
async def test_summarize_survives_upstream_garbage(engine):
    """上游无视 JSON 约束时退化为整句，不抛异常。"""
    for i in range(2):
        engine.mark_reply(GROUP, trigger="at", now=1000.0 + i * 100)
        engine.observe(GROUP, now=1010.0 + i * 100)
    llm = MagicMock()
    llm.chat = AsyncMock(return_value="被叫到时基本都有人接话")
    assert await engine.summarize(llm, GROUP) == "被叫到时基本都有人接话"


@pytest.mark.asyncio
async def test_summarize_returns_none_on_empty(engine):
    for i in range(2):
        engine.mark_reply(GROUP, trigger="at", now=1000.0 + i * 100)
        engine.observe(GROUP, now=1010.0 + i * 100)
    llm = MagicMock()
    llm.chat = AsyncMock(return_value="")
    assert await engine.summarize(llm, GROUP) is None


# ----------------------------------------------------------------------
# 注入：经验事实，不是策略
# ----------------------------------------------------------------------

def test_fragment_empty_without_notes(engine):
    assert engine.fragment(GROUP) == "", "没归纳过就不该占 prompt 预算"


def test_fragment_reports_experience_facts(engine):
    engine._data["notes"][str(GROUP)] = [
        {"text": "被叫到时开口，12 次里 9 次有人接着聊", "ts": 1.0, "samples": 12},
    ]
    frag = engine.fragment(GROUP)
    assert "12 次里 9 次" in frag
    assert frag.startswith("## ")


def test_fragment_rejects_strategy_language(engine):
    """🔴 fail-closed：模型万一写了自我解剖/策略腔，整条丢弃，绝不进 system。"""
    engine._data["notes"][str(GROUP)] = [
        {"text": "我应该更克制，收敛插话频率", "ts": 1.0, "samples": 12},
        {"text": "这说明我需要调整人设边界", "ts": 2.0, "samples": 12},
        {"text": "被叫到时开口，多数时候有人接话", "ts": 3.0, "samples": 12},
    ]
    frag = engine.fragment(GROUP)
    assert "被叫到时开口" in frag
    for bad in ("我应该", "克制", "收敛", "人设", "边界", "需要"):
        assert bad not in frag, f"策略腔「{bad}」漏进了 system"


def test_fragment_without_group_id_reports_global(engine):
    engine._data["notes"][str(GROUP)] = [
        {"text": "被叫到时开口，多数时候有人接话", "ts": 1.0, "samples": 12},
    ]
    engine._data["notes"][str(GROUP_B)] = [
        {"text": "这个群里开口基本没人理", "ts": 1.0, "samples": 9},
    ]
    frag = engine.fragment()
    assert "被叫到时" in frag and "没人理" in frag


# ----------------------------------------------------------------------
# 接线：出站挂 pending
# ----------------------------------------------------------------------

def _make_handler():
    from anima.llm.handler import LLMHandler

    core = types.SimpleNamespace()
    core.config = types.SimpleNamespace(
        permissions=types.SimpleNamespace(owners=[U]),
        persistence=types.SimpleNamespace(data_dir="data"),
        compat=types.SimpleNamespace(astrbot_enabled=False),
        crossgroup=types.SimpleNamespace(enabled=False),
        autonomy=types.SimpleNamespace(enabled=True, reply_only_when_mentioned=True),
        sticker=types.SimpleNamespace(enabled=False, auto_collect=False,
                                      collect_min_interval=0, always_atmosphere=[]),
        conversation=types.SimpleNamespace(enabled=False),
        reply_split=types.SimpleNamespace(enabled=False),
    )
    core.gateway = MagicMock()
    core.gateway.send = AsyncMock(return_value=12345)
    core.agent = None
    core.agenda = MagicMock()
    core.agenda.pending = MagicMock(return_value=[])
    core.audit = None
    return LLMHandler(core)


def _fake_ctx(detail_type: str, reply: str = "喵"):
    return types.SimpleNamespace(
        reply=reply, should_reply=True, meta={}, detail_type=detail_type,
        group_id=GROUP if detail_type == "group" else None,
        user_id=U, target_id=GROUP if detail_type == "group" else U,
        text="在吗",
    )


class _FakePipeline:
    def __init__(self, ctx):
        self._ctx = ctx

    async def run(self, ctx):
        return self._ctx


@pytest.mark.asyncio
async def test_group_outbound_marks_pending(monkeypatch):
    import anima.cognitive.reply_learning as rl_mod

    engine = MagicMock()
    monkeypatch.setattr(rl_mod, "get_reply_learning", lambda *a, **k: engine)

    handler = _make_handler()
    handler.pipeline = _FakePipeline(_fake_ctx("group"))
    handler._maybe_record_feedback = MagicMock(return_value=None)
    handler.core.group_presence = types.SimpleNamespace(
        observe=lambda *a, **k: types.SimpleNamespace(state="quiet"))

    await handler._handle_inner({
        "text": "在吗", "user_id": U, "group_id": GROUP, "message_id": 1,
        "raw": {"message_type": "group", "sender": {"nickname": "群友"}},
    })

    assert engine.mark_reply.called, "群聊出站后必须挂 pending（否则永远学不到效果）"
    assert engine.mark_reply.call_args.args[0] == GROUP


@pytest.mark.asyncio
async def test_private_outbound_does_not_mark(monkeypatch):
    """私聊没有「群里有没有人接话」这回事，不该采集。"""
    import anima.cognitive.reply_learning as rl_mod

    engine = MagicMock()
    monkeypatch.setattr(rl_mod, "get_reply_learning", lambda *a, **k: engine)

    handler = _make_handler()
    handler.pipeline = _FakePipeline(_fake_ctx("private"))
    handler._maybe_record_feedback = MagicMock(return_value=None)

    await handler._handle_inner({
        "text": "在吗", "user_id": U, "group_id": None, "message_id": 2,
        "raw": {"message_type": "private", "sender": {"nickname": "主人"}},
    })

    assert not engine.mark_reply.called


@pytest.mark.asyncio
async def test_no_reply_does_not_mark(monkeypatch):
    """被闸门拦下（should_reply=False）不产生开口，自然不挂 pending。"""
    import anima.cognitive.reply_learning as rl_mod

    engine = MagicMock()
    monkeypatch.setattr(rl_mod, "get_reply_learning", lambda *a, **k: engine)

    handler = _make_handler()
    ctx = _fake_ctx("group")
    ctx.should_reply = False
    handler.pipeline = _FakePipeline(ctx)
    handler._maybe_record_feedback = MagicMock(return_value=None)
    handler.core.group_presence = types.SimpleNamespace(
        observe=lambda *a, **k: types.SimpleNamespace(state="quiet"))

    await handler._handle_inner({
        "text": "嗯", "user_id": U, "group_id": GROUP, "message_id": 3,
        "raw": {"message_type": "group", "sender": {"nickname": "群友"}},
    })

    assert not engine.mark_reply.called


@pytest.mark.asyncio
async def test_send_failure_does_not_mark(monkeypatch):
    """全段发送失败 = 没开口，不该算样本。"""
    import anima.cognitive.reply_learning as rl_mod

    engine = MagicMock()
    monkeypatch.setattr(rl_mod, "get_reply_learning", lambda *a, **k: engine)

    handler = _make_handler()
    handler.core.gateway.send = AsyncMock(return_value=None)   # 渠道返回 None = 失败
    handler.pipeline = _FakePipeline(_fake_ctx("group"))
    handler._maybe_record_feedback = MagicMock(return_value=None)
    handler.core.group_presence = types.SimpleNamespace(
        observe=lambda *a, **k: types.SimpleNamespace(state="quiet"))

    await handler._handle_inner({
        "text": "在吗", "user_id": U, "group_id": GROUP, "message_id": 4,
        "raw": {"message_type": "group", "sender": {"nickname": "群友"}},
    })

    assert not engine.mark_reply.called


# ----------------------------------------------------------------------
# 接线：autonomy 的观察点与过期点
# ----------------------------------------------------------------------

def _make_autonomy(tmp_path, monkeypatch, engine):
    import anima.cognitive.reply_learning as rl_mod
    monkeypatch.setattr(rl_mod, "get_reply_learning", lambda *a, **k: engine)
    from anima.core.autonomy import Heartbeat
    core = MagicMock()
    core.config.persistence.data_dir = str(tmp_path)
    return Heartbeat(core, tick_sec=30, cooldown_sec=300, daily_max=10,
                     ratio_max=0.2, active_feedback_window_sec=300,
                     data_dir=str(tmp_path))


def test_count_message_observes_reply_learning(tmp_path, monkeypatch):
    """观察点必须复用既有的群消息回调（天然在闸门之前）。"""
    engine = ReplyLearning(str(tmp_path), window_sec=300)
    # 🔴 不打补丁 time.time（全局替换会拖垮 logging/pytest 内部计时），
    #    改用相对当前时间的 now。
    engine.mark_reply(GROUP, trigger="at", now=time.time() - 10)

    auto = _make_autonomy(tmp_path, monkeypatch, engine)
    auto.count_message(GROUP)

    assert engine.stats(GROUP)["engaged"] == 1, "群里来消息 ⇒ 被动回复被接话"


def test_expire_reply_feedback_counts_ignored(tmp_path, monkeypatch):
    engine = ReplyLearning(str(tmp_path), window_sec=300)
    engine.mark_reply(GROUP, trigger="at", now=time.time() - 400)

    auto = _make_autonomy(tmp_path, monkeypatch, engine)
    auto._expire_reply_feedback()

    assert engine.stats(GROUP)["ignored"] == 1, "窗口过了没人理 ⇒ 记 ignored"


def test_tick_wires_reply_feedback_expiry():
    """接线守卫：心跳每拍必须结算一次被动回复窗口。

    漏了这一步 ⇒ pending 永远不结算 ⇒ 样本只有 engaged 一侧，统计倾斜。
    """
    import inspect

    from anima.core.autonomy import Heartbeat

    src = inspect.getsource(Heartbeat._tick)
    assert "_expire_reply_feedback()" in src, "心跳没接上被动回复的过期结算"
