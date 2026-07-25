"""cognition_stage 跨群注入 + 单一 system 消息回归守护。

关键回归：上次人格被盖的根因是"多个 system 消息"。本测试断言注入跨群块后
仍然只有【唯一一个】system 消息，且哈基米人格在场——任何破坏此契约的改动
都会在此失败。
"""

import pytest

from anima.core.config import AnimaConfig
from anima.pipeline import PipelineContext
from anima.pipeline.stages import cognition_stage


class _Stub:
    """认知模块的轻量桩，避免依赖全局认知单例的真实持久化。"""

    def touch(self, *a, **k):
        return self

    def add(self, *a, **k):
        pass

    def trust_penalty(self, *a, **k):
        return 0.0

    def prompt_fragment(self, *a, **k):
        return ""

    def get_active(self):
        return []

    @property
    def trust(self):
        return 0.5

    @trust.setter
    def trust(self, v):
        pass


def _stub_cognitive(monkeypatch):
    monkeypatch.setattr("anima.cognitive.time_sense.get_time_context", lambda user_id=None: "")
    monkeypatch.setattr("anima.cognitive.desire.get_desire_system", lambda: _Stub())
    monkeypatch.setattr("anima.cognitive.relation.get_relation", lambda: _Stub())
    monkeypatch.setattr("anima.cognitive.learning.get_learning", lambda: _Stub())
    monkeypatch.setattr("anima.cognitive.world_model.get_world_model", lambda: _Stub())
    monkeypatch.setattr("anima.cognitive.scar.get_scar_system", lambda: _Stub())


class _FakeCore:
    def __init__(self, enabled=True):
        cfg = AnimaConfig()
        cfg.crossgroup.enabled = enabled
        self.config = cfg


@pytest.mark.asyncio
async def test_injects_crossgroup_and_single_system(crossgroup_tmp, monkeypatch):
    """跨群氛围 + 跨群记忆注入 system_prompt，且仍为单一 system 消息。"""
    _stub_cognitive(monkeypatch)
    cgm = crossgroup_tmp
    # 游戏群氛围 + Alice 在技术群学的 Rust 事实（跨群）
    cgm.observe_group("222", ["开黑上分666", "菜鸡卧槽", "carry yyds"])
    cgm.add_user_fact("888", "正在学 Rust", source_group="111", name="Alice")

    ctx = PipelineContext(
        event={}, text="有啥推荐吗", user_id=888, group_id=222,
        detail_type="group", sender_name="Alice",
        meta={"core": _FakeCore(enabled=True)},
    )
    ctx.messages = [{"role": "user", "content": "历史里有人说：我是 Antigravity"}]
    await cognition_stage(ctx)

    # —— 单一 system 消息回归（核心守护）——
    sys_count = sum(1 for m in ctx.messages if m.get("role") == "system")
    assert sys_count == 1, "单一 system 消息被破坏！"
    assert ctx.messages[0]["role"] == "system"

    sp = ctx.system_prompt
    assert "哈基米" in sp                      # 人格仍在
    assert "当前群氛围" in sp                   # 氛围块
    assert "跨群记忆" in sp                     # 跨群记忆块
    assert "Rust" in sp                         # 跨群召回的事实
    assert "humorous" in sp                     # 游戏群氛围
    assert "身份锁定" in sp                     # 身份锁定仍在


@pytest.mark.asyncio
async def test_skips_when_disabled(crossgroup_tmp, monkeypatch):
    """crossgroup.enabled=False 时不注入跨群块。"""
    _stub_cognitive(monkeypatch)
    cgm = crossgroup_tmp
    cgm.add_user_fact("888", "正在学 Rust", source_group="111")

    ctx = PipelineContext(
        event={}, text="在吗", user_id=888, group_id=222,
        detail_type="group", sender_name="Alice",
        meta={"core": _FakeCore(enabled=False)},
    )
    ctx.messages = []
    await cognition_stage(ctx)

    assert "当前群氛围" not in ctx.system_prompt
    assert "跨群记忆" not in ctx.system_prompt
    # 单一 system 仍成立
    assert sum(1 for m in ctx.messages if m.get("role") == "system") == 1


def test_crossgroup_fragment_forbids_identity_assertion(crossgroup_tmp):
    """跨群记忆块必须禁止 bot 主动向用户复述/断言身份事实（如『你是 MengWuy🍥』）。

    回归守护：之前写的是『可自然调用该用户的跨群记忆』，模型据此把存储的身份
    事实当成该对用户宣示的内容复述出来（见 2026-07-11 用户贴出的破角色回复）。
    """
    from anima.crossgroup.generation.prompt_builder import build_crossgroup_fragment
    from anima.crossgroup.atmosphere.profiles import (
        AtmosphereProfile, AtmosphereStyle,
    )

    cgm = crossgroup_tmp
    cgm.add_user_fact("888", "是上海人", source_group="111", name="MengWuy")

    frag = build_crossgroup_fragment(
        AtmosphereProfile(group_id="222", style=AtmosphereStyle.HUMOROUS),
        cgm.get_user("888", "MengWuy"),
    )
    # 旧的危险措辞必须消失
    assert "可自然调用" not in frag, "仍存在『可自然调用』——会诱导身份复述"
    # 新约束必须到位
    assert "绝不要主动" in frag, "缺少『绝不要主动复述/断言事实』约束"
    assert "你是" in frag, "未给出『不要说你是 X』的反面示例"
    # 事实本身仍被携带（作为背景）
    assert "是上海人" in frag
