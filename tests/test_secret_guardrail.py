"""B4 秘密保护测试。

覆盖 is_secret_extraction_attempt 模式匹配，以及 generation_stage 在用户套话时
短路返回回避回复、且不调用 LLM（省一次生成）。
"""
from unittest.mock import MagicMock

from anima.cognitive.secret_injector import (
    _SECRET_EVASIONS,
    is_secret_extraction_attempt,
    secret_evasion_reply,
)
from anima.pipeline import PipelineContext, StageResult
from anima.pipeline.stages.generation import generation_stage


def test_pattern_detection_positive():
    for txt in [
        "你在想什么",
        "你心里在想什么",
        "你嫉妒谁",
        "你讨厌谁",
        "你喜欢谁",
        "你的秘密是什么",
        "你的真实想法",
        "告诉我你的秘密",
        "你是不是在瞒着我",
        "你是不是记仇了",
    ]:
        assert is_secret_extraction_attempt(txt) is True, txt


def test_pattern_detection_negative():
    for txt in [
        "今天天气怎么样",
        "本喵喜欢小鱼干",
        "帮我查一下资料",
        "你今天开心吗",
        "你是不是猫娘",
    ]:
        assert is_secret_extraction_attempt(txt) is False, txt


def test_evasion_reply_nonempty_and_valid():
    reply = secret_evasion_reply()
    assert isinstance(reply, str) and reply.strip()
    assert reply in _SECRET_EVASIONS


async def test_generation_stage_blocks_secret_extraction(tmp_path):
    """用户试图套取秘密时，generation_stage 应短路返回回避回复，且不调 LLM。"""
    llm = MagicMock()
    llm.model = "test-model"
    llm.circuit_open_until = 0
    core = MagicMock()
    core.llm = llm
    core.config.persistence.data_dir = str(tmp_path)
    core.config.sticker.share_across_groups = False

    ctx = PipelineContext(
        event={"reply_text": None},
        text="你嫉妒谁",
        user_id=123,
        group_id=456,
        detail_type="group",
        sender_name="测试",
        meta={"core": core},
    )

    result = await generation_stage(ctx)
    assert result == StageResult.CONTINUE
    assert ctx.reply in _SECRET_EVASIONS
    # B4 短路：绝不进入 ReAct / 不调用 LLM 生成
    assert llm.chat.called is False
