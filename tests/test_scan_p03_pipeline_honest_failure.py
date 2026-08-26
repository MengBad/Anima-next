# -*- coding: utf-8 -*-
"""P0-A4：Generation 阶段失败必须如实降级——显式标记+中止后续阶段；
非生成阶段保持故障隔离。"""
import asyncio

from anima.pipeline import Pipeline, PipelineContext


def _ctx():
    return PipelineContext(event={}, text="hi", user_id=1, group_id=None,
                           detail_type="private", sender_name="t")


def test_generation_failure_is_honest():
    called = []

    async def permission_stage(ctx):
        return None

    async def generation_stage(ctx):  # 名字与真实注册一致（特判锚点）
        raise RuntimeError("LLM 熔断")

    async def guardrail_stage(ctx):
        called.append("guardrail")
        return None

    p = Pipeline().use(permission_stage).use(generation_stage).use(guardrail_stage)
    ctx = _ctx()
    asyncio.run(p.run(ctx))
    assert called == [], "generation 失败后不得继续执行 guardrail"
    assert ctx.should_reply is False and ctx.stop_reason == "generation_failed"
    assert ctx.meta.get("generation_failed") is True


def test_non_generation_failure_still_isolated():
    order = []

    async def memory_stage(ctx):
        raise ValueError("记忆抖动")

    async def cognition_stage(ctx):
        order.append("cognition")
        return None

    p = Pipeline().use(memory_stage).use(cognition_stage)
    ctx = _ctx()
    asyncio.run(p.run(ctx))
    assert order == ["cognition"], "非生成阶段异常仍应隔离并继续"
    assert "generation_failed" not in ctx.meta
