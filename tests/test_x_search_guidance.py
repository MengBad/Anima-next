"""X（Twitter）搜索引导回归测试。

背景：用户明确要求「去 X/推特搜 XX」时，Anima 必须调用 x_search 去查，
而不是因为人格提示「可以拒绝」而推给用户自己翻。
验证三层：
1. persona_core.yaml 含「搜索请求必须执行、不得拒绝」规则
2. generation._X_SEARCH_RE 能命中 X/推特/男娘推荐 等请求
3. generation_stage 收到 X 搜索请求时，把 _X_SEARCH_GUIDANCE 注入唯一 system
"""
import pathlib

import pytest

from anima.pipeline import PipelineContext, StageResult
from anima.pipeline.stages import generation_stage
from anima.pipeline.stages.generation import (
    _X_SEARCH_RE,
    _X_SEARCH_GUIDANCE,
    _IDENTITY_LOCK_HEADING,
)


def _fake_core():
    class _FakeLLM:
        model = "fake-model"

    class _FakeTools:
        tools = {"x_search": object()}

        def set_call_context(self, **kw):
            self._ctx = dict(kw)
            return object()

        def get_call_context(self):
            return {"generated_image_paths": []}

        def reset_call_context(self, token):
            self._ctx = {}

    class _Sticker:
        share_across_groups = True

    class _Config:
        sticker = _Sticker()

    class _FakeCore:
        llm = _FakeLLM()
        config = _Config()
        vector_memory = None
        tools = _FakeTools()

    return _FakeCore()


_NL = chr(10)
_IDENTITY_BLOCK = f"{_IDENTITY_LOCK_HEADING}{_NL}你是哈基米，一只猫娘。"


# ---------------------------------------------------------------------------
# 1. persona_core.yaml 必须包含「搜索请求不拒绝」规则
# ---------------------------------------------------------------------------
def test_persona_core_requires_search_execution():
    import yaml

    repo = pathlib.Path(__file__).resolve().parent.parent
    cfg = yaml.safe_load((repo / "config" / "persona_core.yaml").read_text(encoding="utf-8"))
    prompt = cfg["system_prompt"]
    # 搜索请求必须执行，不得拒绝/推给用户
    assert "x_search" in prompt or "web_search" in prompt
    assert "不能拒绝" in prompt
    assert "查资料" in prompt or "搜索" in prompt


# ---------------------------------------------------------------------------
# 2. _X_SEARCH_RE 命中 X/推特/男娘推荐 等请求
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "去X搜男娘推荐",
    "在推特上搜一下男娘",
    "去 X 搜",
    "推特搜",
    "看看推特上有什么",
    "x.com 上找点好玩的",
    "帮我查推特上的男娘推荐",
])
def test_x_search_regex_hits(text):
    assert _X_SEARCH_RE.search(text), f"应命中 X 搜索请求: {text}"


@pytest.mark.parametrize("text", [
    "今天天气怎么样",
    "你会做饭吗",
    "哈哈哈哈哈哈",
])
def test_x_search_regex_does_not_false_positive(text):
    assert not _X_SEARCH_RE.search(text), f"不应误命中: {text}"


# ---------------------------------------------------------------------------
# 3. generation_stage 收到 X 搜索请求时注入 _X_SEARCH_GUIDANCE
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_generation_stage_injects_x_search_guidance(monkeypatch):
    class _FakeReAct:
        def __init__(self, *a, **k):
            pass

        async def run(self, messages, **k):
            return "好的，本喵帮你去翻翻喵"

    import anima.agent.react as react_mod
    monkeypatch.setattr(react_mod, "ReActLoop", _FakeReAct)

    ctx = PipelineContext(
        event={},
        text="去X搜男娘推荐",
        user_id=200,
        group_id=727631526,
        detail_type="group",
        sender_name="小明",
        meta={"core": _fake_core()},
    )
    ctx.messages = [{"role": "system", "content": f"系统设定{_NL}{_NL}{_IDENTITY_BLOCK}"}]

    result = await generation_stage(ctx)
    assert result == StageResult.CONTINUE

    sys_msgs = [m for m in ctx.messages if m.get("role") == "system"]
    assert sys_msgs
    sys_text = sys_msgs[0]["content"]
    assert _X_SEARCH_GUIDANCE in sys_text, "X 搜索引导应注入唯一 system"
