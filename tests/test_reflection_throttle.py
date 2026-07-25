"""反思限频 + 角色守卫 回归测试（对应体检报告 P1 + 用户贴出的破角色回复）。

覆盖：
- RuminationEngine 限频门控（maybe_ruminate 在冷却内直接返回 None）
- SelfLearning.ruminate 生成提示词含「工程黑话/纠偏」禁用规则
- cognition_stage 把「角色守卫」块追加进 system_prompt（防止破角色扮演分析工具）
"""
import asyncio
import time
import types

import pytest

from anima.cognitive.rumination import RuminationEngine
from anima.cognitive.learning import SelfLearning


class _FakeCore:
    llm = None

    class config:
        class persistence:
            data_dir = ""


def test_maybe_ruminate_interval_gate(tmp_path):
    """冷却内再次触发应直接返回 None（限频核心行为）。"""
    core = _FakeCore()
    core.config.persistence.data_dir = str(tmp_path)
    eng = RuminationEngine(core, data_dir=str(tmp_path), interval_sec=300)
    eng._last_run = time.time()  # 模拟刚跑过
    assert asyncio.run(eng.maybe_ruminate()) is None


def test_maybe_ruminate_after_interval_proceeds(tmp_path):
    """冷却过后应进入后续逻辑（不再被门控拦截）。"""
    core = _FakeCore()
    core.config.persistence.data_dir = str(tmp_path)
    eng = RuminationEngine(core, data_dir=str(tmp_path), interval_sec=1)
    eng._last_run = 0.0  # 很久以前
    # 门控放行后进入 memory/llm 分支；core.llm is None → 在该分支返回 None，
    # 但关键是「没有被 interval 门控提前 return」，用 _last_run 已推进佐证。
    asyncio.run(eng.maybe_ruminate())
    assert time.time() - eng._last_run < 5  # 已被刷新为本次调用时刻


class _FakeLLM:
    def __init__(self):
        self.calls = []

    async def chat(self, messages, temperature=0.7, max_tokens=120):
        self.calls.append(messages[0]["content"])
        prompt = messages[0]["content"]
        if "生成一条简短的自我反思" in prompt:
            return "我刚才对用户有点太凶了"
        if "核心洞察" in prompt:  # 反思语义去重判定
            return "DUPLICATE: NO"
        return "ok"


def test_ruminate_prompt_avoids_jargon(tmp_path):
    """ruminate 的生成提示词必须包含禁用工程黑话的规则。"""
    sl = SelfLearning(str(tmp_path))
    llm = _FakeLLM()
    ctx = "用户说本喵总是提男娘和 GOP，还爱刷屏骂人，本喵有点烦。" * 8
    asyncio.run(sl.ruminate(llm, ctx))
    gen = [p for p in llm.calls if "生成一条简短的自我反思" in p]
    assert gen, "反思生成提示词未被捕获"
    assert "工程黑话" in gen[0] or "纠偏" in gen[0]


class _StubFrag:
    def prompt_fragment(self, *a, **k):
        return ""

    def touch(self, *a, **k):
        return None

    def trust_penalty(self, *a, **k):
        return 0.0

    def add(self, *a, **k):
        return None

    def get_active(self, *a, **k):
        return []


def test_cognition_stage_appends_role_guard(monkeypatch):
    """cognition_stage 必须把「角色守卫」块追加进 system_prompt。"""
    import anima.pipeline.stages as stages

    monkeypatch.setattr(
        stages, "load_persona_template",
        lambda: "{time_context}{desire_context}{self_notes}{world_context}{scar_context}",
    )
    monkeypatch.setattr("anima.cognitive.time_sense.get_time_context", lambda user_id=None: "")
    monkeypatch.setattr("anima.cognitive.desire.get_desire_system", lambda: _StubFrag())
    monkeypatch.setattr("anima.cognitive.relation.get_relation", lambda: _StubFrag())
    monkeypatch.setattr("anima.cognitive.learning.get_learning", lambda *a, **k: _StubFrag())
    monkeypatch.setattr("anima.cognitive.world_model.get_world_model", lambda *a, **k: _StubFrag())
    monkeypatch.setattr("anima.cognitive.scar.get_scar_system", lambda: _StubFrag())

    ctx = types.SimpleNamespace(
        user_id="1", text="hi", meta={}, system_prompt="",
        atmosphere_style=None, messages=[],
    )
    asyncio.run(stages.cognition_stage(ctx))
    assert "角色守卫" in ctx.system_prompt
    # 守卫明确禁止编造百分比 / 扮演分析工具
    assert "百分比" in ctx.system_prompt
    assert "结构化解剖" in ctx.system_prompt
