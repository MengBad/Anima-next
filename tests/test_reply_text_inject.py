"""回归测试：QQ 引用（reply）消息正文应注入 LLM 上下文，让 bot 真正「看到」被引用内容。

根因：此前 handler 只把 reply 段用于 reply_to_self 判定与图片提取，被引用的正文
从未进入 LLM 上下文，导致 bot 对「引用一句话后提问」完全失明。
"""
import pytest
from anima.channels.onebot11 import parse_onebot_event
from anima.pipeline.stages import generation_stage
from anima.pipeline import PipelineContext, StageResult


# ---------------------------------------------------------------------------
# 1) 入站解析：NapCat reply 段内联正文时直接捕获
# ---------------------------------------------------------------------------
def test_parse_onebot_event_captures_inline_reply_text():
    raw = {
        "post_type": "message",
        "message_type": "group",
        "self_id": 100,
        "user_id": 200,
        "group_id": 727631526,
        "message_id": 5,
        "message": [
            {"type": "reply", "data": {"id": 999, "text": "被引用的原文内容"}},
            {"type": "text", "data": {"text": "你怎么看"}},
        ],
        "raw_message": "[quote]你怎么看",
    }
    ev = parse_onebot_event(raw)
    assert ev.get("reply_message_id") == 999
    assert ev.get("reply_text") == "被引用的原文内容"


def test_parse_onebot_event_reply_id_only_no_inline_text():
    raw = {
        "post_type": "message",
        "message_type": "group",
        "self_id": 100,
        "user_id": 200,
        "group_id": 727631526,
        "message_id": 5,
        "message": [
            {"type": "reply", "data": {"id": 999}},
            {"type": "text", "data": {"text": "你怎么看"}},
        ],
    }
    ev = parse_onebot_event(raw)
    assert ev.get("reply_message_id") == 999
    # 没有内联正文时不预设 reply_text；handler 端会兜底 get_msg 拿正文
    assert "reply_text" not in ev


# ---------------------------------------------------------------------------
# 2) handler._extract_reply_text：从 get_msg 返回结构里提取纯文本
# ---------------------------------------------------------------------------
def test_extract_reply_text_from_get_msg_shape():
    from anima.llm.handler import LLMHandler
    replied = {
        "status": "ok",
        "data": {
            "message_id": 999,
            "message": [
                {"type": "text", "data": {"text": "早安"}},
                {"type": "text", "data": {"text": "今天天气不错"}},
                {"type": "face", "data": {"id": "1"}},
            ],
            "sender": {"user_id": 300},
        },
    }
    # 不依赖 self 的纯函数方法，传 None 作为 self 即可
    assert LLMHandler._extract_reply_text(None, replied) == "早安 今天天气不错"


def test_extract_reply_text_edge_cases():
    from anima.llm.handler import LLMHandler
    assert LLMHandler._extract_reply_text(None, {"data": {"message": "纯字符串正文"}}) == "纯字符串正文"
    assert LLMHandler._extract_reply_text(None, None) == ""
    assert LLMHandler._extract_reply_text(None, {}) == ""
    assert LLMHandler._extract_reply_text(None, {"data": {"message": []}}) == ""


# ---------------------------------------------------------------------------
# 3) generation_stage：把引用正文注入 LLM 用户轮（不动 ctx.text，保持记忆干净）
# ---------------------------------------------------------------------------
def _fake_core():
    class _FakeLLM:
        model = "fake-model"
    class _FakeCore:
        llm = _FakeLLM()
        config = None
        vector_memory = None
        tools = None
    return _FakeCore()


@pytest.mark.asyncio
async def test_generation_stage_injects_reply_text(monkeypatch):
    class _FakeReAct:
        def __init__(self, *a, **k):
            pass
        async def run(self, messages, **k):
            return "好的"
    import anima.agent.react as react_mod
    monkeypatch.setattr(react_mod, "ReActLoop", _FakeReAct)

    ctx = PipelineContext(
        event={"reply_text": "被引用的上一条消息"},
        text="你怎么看",
        user_id=200,
        group_id=727631526,
        detail_type="group",
        sender_name="小明",
        meta={"core": _fake_core()},
    )
    ctx.messages = []
    result = await generation_stage(ctx)
    assert result == StageResult.CONTINUE
    user_msgs = [m for m in ctx.messages if m.get("role") == "user"]
    assert user_msgs, "没有 user 消息被追加"
    last = user_msgs[-1]["content"]
    assert "被引用的上一条消息" in last
    assert "你怎么看" in last
    assert "[群聊消息]" in last


@pytest.mark.asyncio
async def test_generation_stage_no_reply_text_when_absent(monkeypatch):
    class _FakeReAct:
        def __init__(self, *a, **k):
            pass
        async def run(self, messages, **k):
            return "好的"
    import anima.agent.react as react_mod
    monkeypatch.setattr(react_mod, "ReActLoop", _FakeReAct)

    ctx = PipelineContext(
        event={},
        text="直接说的话",
        user_id=200,
        group_id=727631526,
        detail_type="group",
        sender_name="小明",
        meta={"core": _fake_core()},
    )
    ctx.messages = []
    await generation_stage(ctx)
    last = [m for m in ctx.messages if m.get("role") == "user"][-1]["content"]
    assert "引用的上一条消息" not in last
    assert "直接说的话" in last


@pytest.mark.asyncio
async def test_generation_stage_falls_back_when_react_raises(monkeypatch):
    """回归：生成阶段（react.run）若抛异常，必须降级为可见回复而非静默不回。

    根因：Pipeline.run 对每个 stage 仅打日志后继续，不会代为设置回复；
    若 generation_stage 的 react.run 抛异常且无人接住，ctx.reply 永远为空，
    bot 对这条消息完全不回——最伤体验的 silent non-reply。
    """
    class _BoomReAct:
        def __init__(self, *a, **k):
            pass
        async def run(self, messages, **k):
            raise RuntimeError("simulated LLM/tool crash")
    import anima.agent.react as react_mod
    monkeypatch.setattr(react_mod, "ReActLoop", _BoomReAct)

    ctx = PipelineContext(
        event={},
        text="你好",
        user_id=200,
        group_id=727631526,
        detail_type="group",
        sender_name="小明",
        meta={"core": _fake_core()},
    )
    ctx.messages = []
    result = await generation_stage(ctx)
    assert result == StageResult.CONTINUE
    assert ctx.reply, "生成阶段异常后必须设置降级回复，不能静默不回"
    assert "脑子卡了一下" in ctx.reply
