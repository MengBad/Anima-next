"""霸凌连续 3 次沉默：端到端验证 generation 阶段行为（任务2/任务3）。"""
import asyncio
import types

from anima.pipeline import PipelineContext, StageResult
from anima.pipeline.stages.generation import generation_stage


def _fake_core():
    # 只需要 llm 为真（不触发 SKIP_REPLY 早期返回）；霸凌分支不依赖 config/tools。
    return types.SimpleNamespace(llm=object(), config=None, tools=None, reply_quality=None)


def _make_ctx(recent_count: int):
    ctx = PipelineContext(
        event={},
        text="傻逼",
        user_id=123,
        group_id=1,
        detail_type="group",
        sender_name="tester",
        meta={
            "core": _fake_core(),
            "bullying_detected": True,
            "bullying_recent_count": recent_count,
        },
    )
    ctx.messages.append({"role": "system", "content": "system"})
    return ctx


def test_generation_silence_after_three():
    ctx = _make_ctx(recent_count=3)
    result = asyncio.run(generation_stage(ctx))
    assert result == StageResult.SKIP_REPLY
    assert ctx.should_reply is False
    assert ctx.reply is None


def test_generation_cold_reply_under_three():
    ctx = _make_ctx(recent_count=1)
    result = asyncio.run(generation_stage(ctx))
    assert result == StageResult.CONTINUE
    assert ctx.reply is not None
    assert len(ctx.reply) <= 30
