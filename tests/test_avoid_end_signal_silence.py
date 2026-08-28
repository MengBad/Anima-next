"""① 回避期 + 对方示意结束 → 彻底沉默（不补冷话收尾）。

覆盖：占用 2 条伤痕进入回避期的用户，若其消息命中结束信号
（转私聊 / 别说了 / 晚安…），generation_stage 应 SKIP_REPLY 完全静默，
而不是从 COLD_RESPONSES 补一句「本喵先不回了」。
"""
from unittest.mock import MagicMock
import asyncio

from anima.cognitive import scar as scar_mod
from anima.pipeline import PipelineContext, StageResult
from anima.pipeline.stages.generation import generation_stage


def _clear_scar_cache(data_dir):
    scar_mod._scar_systems.pop(data_dir, None)


def _make_core(data_dir):
    llm = MagicMock()
    llm.model = "test-model"
    llm.circuit_open_until = 0
    core = MagicMock()
    core.llm = llm
    core.config.persistence.data_dir = data_dir
    core.config.sticker.share_across_groups = False
    return core


def _make_scar(data_dir):
    _clear_scar_cache(data_dir)
    return scar_mod.ScarSystem(data_dir)


async def test_avoid_end_signal_silence(tmp_path):
    data_dir = str(tmp_path / "s1")
    scar = _make_scar(data_dir)
    scar.add(123, "滚", severity=0.6)
    scar.add(123, "你sb", severity=0.6)
    assert scar.is_avoiding(123), "前置：用户应处于回避期"
    # 让 singleton 缓存指向同一 system，generation 才能读到
    scar_mod._scar_systems[data_dir] = scar

    core = _make_core(data_dir)
    ctx = PipelineContext(
        event={"reply_text": None},
        text="行了 转私聊了 别再说了",
        user_id=123,
        group_id=456,
        detail_type="group",
        sender_name="测试",
        meta={"core": core},
    )

    result = await generation_stage(ctx)
    assert result == StageResult.SKIP_REPLY
    assert ctx.should_reply is False
    assert ctx.reply is None
    # 完全沉默：不补冷话，也不走 LLM
    assert core.llm.chat.called is False


async def test_avoid_no_end_signal_still_short(tmp_path):
    """回避期但未示意结束：仍保留极短冷回（防长篇辩解）。"""
    data_dir = str(tmp_path / "s2")
    scar = _make_scar(data_dir)
    scar.add(123, "滚", severity=0.6)
    scar.add(123, "你sb", severity=0.6)
    assert scar.is_avoiding(123)
    scar_mod._scar_systems[data_dir] = scar

    core = _make_core(data_dir)
    ctx = PipelineContext(
        event={"reply_text": None},
        text="随便说点什么别的",
        user_id=123,
        group_id=456,
        detail_type="group",
        sender_name="测试",
        meta={"core": core},
    )

    result = await generation_stage(ctx)
    assert result == StageResult.CONTINUE
    assert ctx.reply and len(ctx.reply) <= 30
