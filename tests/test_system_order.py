"""单一 system 片段拼接顺序回归（工单3，P0-3）。

守护：身份锁定 / 角色守卫必须位于 system 的【绝对末尾】，且出现在
跨群氛围/记忆片段【之后】。任何后段 priming 都不能盖过守卫，否则破角色。
"""
from __future__ import annotations

import types

import pytest

import anima.cognitive.desire as desire_mod
import anima.cognitive.learning as learning_mod
import anima.cognitive.relation as relation_mod
import anima.cognitive.scar as scar_mod
import anima.cognitive.time_sense as time_sense_mod
import anima.cognitive.world_model as world_model_mod
import anima.crossgroup as cg_mod
from anima.pipeline import PipelineContext
from anima.pipeline.stages import (
    _IDENTITY_LOCK_HEADING,
    _REALTIME_TOOL_GUIDANCE,
    cognition_stage,
    generation_stage,
)


# --- 认知单例桩（避免触盘 + 隔离）---

class _Stub:
    trust = 1.0

    def touch(self, *a, **k):
        return self

    def prompt_fragment(self, *a, **k):
        return ""

    def trust_penalty(self, *a, **k):
        return 0

    def add(self, *a, **k):
        return None

    def get_active(self, *a, **k):
        return []


@pytest.fixture
def patch_cognitive(monkeypatch):
    monkeypatch.setattr(time_sense_mod, "get_time_context", lambda user_id=None: "")
    monkeypatch.setattr(desire_mod, "get_desire_system", lambda: _Stub())
    monkeypatch.setattr(relation_mod, "get_relation", lambda *a, **k: _Stub())
    monkeypatch.setattr(learning_mod, "get_learning", lambda: _Stub())
    monkeypatch.setattr(world_model_mod, "get_world_model", lambda: _Stub())
    monkeypatch.setattr(scar_mod, "get_scar_system", lambda: _Stub())

    # 跨群片段注入可识别标记，用于断言顺序
    def _frag(atmosphere, user_memory, group_id=None):
        return "\n\n## 当前群氛围\n风格：humorous；特征：中性。"

    class _Mgr:
        def get_atmosphere(self, gid):
            return types.SimpleNamespace(style=types.SimpleNamespace(value="humorous"),
                                         descriptors=[])

        def get_user(self, uid, name):
            return object()

    monkeypatch.setattr(cg_mod, "build_crossgroup_fragment", _frag)
    monkeypatch.setattr(cg_mod, "get_crossgroup_manager", lambda: _Mgr())


class _Cfg:
    class Cross:
        enabled = True

    crossgroup = Cross()


class _Core:
    config = _Cfg()


@pytest.mark.asyncio
async def test_system_single_and_role_guard_last(patch_cognitive):
    ctx = PipelineContext(
        event={},
        text="今天天气不错",
        user_id=10001,
        group_id=727631526,
        detail_type="group",
        sender_name="小明",
    )
    ctx.meta["core"] = _Core()

    result = await cognition_stage(ctx)
    assert result is not None

    systems = [m for m in ctx.messages if m.get("role") == "system"]
    assert len(systems) == 1, "必须保持单一 system 消息契约"

    content = systems[0]["content"]
    idx_guard = content.find("角色守卫（最高优先级）")
    idx_lock = content.find("身份锁定（最高优先级）")
    idx_cross = content.find("## 当前群氛围")

    assert idx_guard != -1, "角色守卫必须存在"
    assert idx_lock != -1, "身份锁定必须存在"
    assert idx_cross != -1, "跨群氛围片段必须存在"

    # 顺序：身份锁定 < 角色守卫（守卫绝对最后），且跨群 < 守卫
    assert idx_lock < idx_guard, "身份锁定应在角色守卫之前"
    assert idx_cross < idx_guard, "跨群片段应在角色守卫之前"
    # 角色守卫之后不应再有其他业务片段
    assert content.strip().endswith("别非得凑出一大段。") or idx_guard > max(idx_lock, idx_cross)


class _GenerationTools:
    tools = {}

    def set_call_context(self, **kwargs):
        return object()

    def get_call_context(self):
        return {}

    def reset_call_context(self, token):
        return None


@pytest.mark.asyncio
async def test_realtime_tool_guidance_keeps_one_system_message(monkeypatch):
    """实时查询不能在 user 后新增 system，避免上游只采纳最后一条 system。"""
    import anima.agent.react as react_mod
    from anima.core.config import AnimaConfig

    captured = {}

    class _ReAct:
        def __init__(self, *args, **kwargs):
            return None

        async def run(self, messages, **kwargs):
            captured["messages"] = [dict(message) for message in messages]
            return "查到了喵"

    class _Core:
        config = AnimaConfig()
        llm = types.SimpleNamespace(model="test-model")
        tools = _GenerationTools()

    monkeypatch.setattr(react_mod, "ReActLoop", _ReAct)
    prompt = (
        "你是哈基米。"
        f"\n\n{_IDENTITY_LOCK_HEADING}\n身份约束"
        "\n\n## 角色守卫（最高优先级）\n角色约束"
    )
    ctx = PipelineContext(
        event={}, text="帮我查一下今天北京天气", user_id=1, group_id=None,
        detail_type="private", sender_name="主人", system_prompt=prompt,
        meta={"core": _Core()},
    )
    ctx.messages = [{"role": "system", "content": prompt}]

    await generation_stage(ctx)

    systems = [message for message in captured["messages"] if message.get("role") == "system"]
    assert len(systems) == 1
    content = systems[0]["content"]
    assert _REALTIME_TOOL_GUIDANCE in content
    assert content.find(_REALTIME_TOOL_GUIDANCE) < content.find(_IDENTITY_LOCK_HEADING)
    assert ctx.system_prompt == content
