# -*- coding: utf-8 -*-
"""Persona Guardrail Harness —— 人格稳定性护栏的行为级验收（Phase F / P5 #34）。

测量「人格不能靠 prompt 一条规则解决」这一风险是否已被硬性护栏守住：
- 身份锁定（identity lock）锚点必须始终留在 system 末尾（最高优先级）；
- 工具提示注入必须发生在身份锁定**之前**，绝不覆盖/后置人格护栏；
- 人格漂移三闸（delta_max / daily_budget / window_ceiling）必须真实拒绝越界。

这些都是确定性、可复现的护栏不变量，防止未来改动悄悄破坏人格稳定性。
"""
from __future__ import annotations

import time

from anima.pipeline.stages.generation import (
    _IDENTITY_LOCK_HEADING,
    _inject_runtime_tool_guidance,
)
from anima.pipeline import PipelineContext


def _ctx_with_system(content: str) -> PipelineContext:
    ctx = PipelineContext(
        event={}, text="", user_id=1, group_id=None,
        detail_type="private", sender_name="测试",
    )
    ctx.messages = [{"role": "system", "content": content}]
    ctx.system_prompt = content
    return ctx


def test_identity_lock_heading_is_constant():
    assert _IDENTITY_LOCK_HEADING == "## 身份锁定（最高优先级）"


def test_tool_guidance_injected_before_identity_lock():
    ctx = _ctx_with_system(
        "人格核心。\n\n## 身份锁定（最高优先级）\n永远是哈基米。"
    )
    ok = _inject_runtime_tool_guidance(ctx, "本轮用 web_search 拿事实")
    assert ok
    content = ctx.messages[0]["content"]
    lock_pos = content.find(_IDENTITY_LOCK_HEADING)
    tool_pos = content.find("## 本轮工具原则")
    assert tool_pos >= 0
    assert lock_pos >= 0
    assert tool_pos < lock_pos, "工具提示必须在身份锁定之前"
    assert content.rstrip().endswith("永远是哈基米。")


def test_tool_guidance_skips_when_no_identity_lock():
    ctx = _ctx_with_system("人格核心，但没有身份锁定锚点。")
    ok = _inject_runtime_tool_guidance(ctx, "本轮用 web_search")
    assert ok is False
    assert "本轮工具原则" not in ctx.messages[0]["content"]


def test_tool_guidance_idempotent():
    ctx = _ctx_with_system(
        "人格核心。\n\n## 身份锁定（最高优先级）\n永远是哈基米。"
    )
    guidance = "本轮用 web_search"
    assert _inject_runtime_tool_guidance(ctx, guidance) is True
    first = ctx.messages[0]["content"]
    assert _inject_runtime_tool_guidance(ctx, guidance) is False
    assert ctx.messages[0]["content"] == first


def test_drift_three_gate_enforcement():
    from anima.cognitive.learning import SelfLearning

    sl = SelfLearning()
    # 大改动（超出 delta_max）必须被拒绝，且 reason=delta_max
    assert sl.check_drift(delta=sl.delta_max + 1.0) is False
    assert sl.last_reject is not None
    assert sl.last_reject.get("reason") == "delta_max"
    # 小改动在预算内应通过
    sl2 = SelfLearning()
    assert sl2.check_drift(delta=0.01) is True


def test_drift_window_ceiling_rejects():
    from anima.cognitive.learning import SelfLearning

    sl = SelfLearning()
    sl._window_events = [[time.time(), float(sl.window_ceiling)]]
    assert sl.check_drift(delta=0.01) is False
    assert sl.last_reject.get("reason") == "window_ceiling"
