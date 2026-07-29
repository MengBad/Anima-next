"""霸凌冷回单元测试（任务2）：冲突中回复必须短。"""
from anima.pipeline.stages.generation import COLD_RESPONSES, _resolve_bullying_reply


def test_cold_responses_all_short():
    for c in COLD_RESPONSES:
        assert len(c) <= 30, f"冷回过长：{c!r}"


def test_cold_reply_short_for_low_counts():
    for n in (0, 1, 2):
        r = _resolve_bullying_reply(n)
        assert r is not None
        assert len(r) <= 30


def test_silence_after_three():
    # 连骂 >=3 次：完全沉默（返回 None，由调用方置 should_reply=False）
    assert _resolve_bullying_reply(3) is None
    assert _resolve_bullying_reply(5) is None
