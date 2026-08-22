# -*- coding: utf-8 -*-
"""§17 安全版反馈自然回话 —— 测试。

核心承诺：
- 只在「开启 + 私聊 + 识别到反馈 + 主回复已有文本」时，于主回复段末尾追加一句
  轻量确认；绝不额外发第二条消息、绝不改主回复内容。
- 默认关（prod 行为不变）；群聊绝不追加（防刷屏）；无主回复文本时绝不凭空造回复。
- 纯函数 _maybe_append_feedback_ack 不可变：传入列表不被改动。
"""
from __future__ import annotations

from unittest import mock

from anima.llm.handler import _FEEDBACK_ACK_SUFFIX, _maybe_append_feedback_ack


def _segs(*texts: str) -> list[dict]:
    return [{"type": "text", "data": {"text": t}} for t in texts]


def _fb():
    sig = mock.Mock()
    sig.kind = "bug"
    return sig


def test_private_with_main_reply_appends_once():
    segs = _segs("好的，我查一下喵")
    out = _maybe_append_feedback_ack(
        segs, detail_type="private", fb=_fb(), enabled=True)
    assert len(out) == 2
    assert out[-1]["type"] == "text"
    assert out[-1]["data"]["text"] == _FEEDBACK_ACK_SUFFIX
    # 主回复内容未被改动（仍是第一段）
    assert out[0]["data"]["text"] == "好的，我查一下喵"


def test_disabled_by_default_no_append():
    segs = _segs("好的喵")
    out = _maybe_append_feedback_ack(
        segs, detail_type="private", fb=_fb(), enabled=False)
    assert out is segs  # 原样返回同一列表，prod 行为不变


def test_group_never_appends():
    segs = _segs("好的喵")
    out = _maybe_append_feedback_ack(
        segs, detail_type="group", fb=_fb(), enabled=True)
    assert len(out) == 1  # 群聊绝不追加，防刷屏


def test_no_main_reply_text_no_append():
    # segments 无文本段（如只有表情/图片）时不凭空造回复
    segs = [{"type": "image", "data": {"url": "x.png"}}]
    out = _maybe_append_feedback_ack(
        segs, detail_type="private", fb=_fb(), enabled=True)
    assert len(out) == 1


def test_no_feedback_no_append():
    segs = _segs("好的喵")
    out = _maybe_append_feedback_ack(
        segs, detail_type="private", fb=None, enabled=True)
    assert len(out) == 1


def test_pure_not_mutating_input():
    segs = _segs("好的喵")
    snapshot = list(segs)
    _ = _maybe_append_feedback_ack(
        segs, detail_type="private", fb=_fb(), enabled=True)
    assert segs == snapshot  # 输入列表未被原地修改


def test_config_gate_natural_feedback_ack_reads_yaml(tmp_path):
    # 真实读取 config.yaml 的开关路径：默认关，置 true 即启
    import yaml
    from anima.core.config import load_config
    cfg = load_config(tmp_path / "c.yaml")  # 不存在 -> 用默认
    assert cfg.conversation.natural_feedback_ack is False
    p = tmp_path / "c.yaml"
    p.write_text("conversation:\n  natural_feedback_ack: true\n", encoding="utf-8")
    cfg2 = load_config(p)
    assert cfg2.conversation.natural_feedback_ack is True


def test_maybe_record_feedback_returns_signal():
    from anima.llm.handler import LLMHandler
    from anima.observation.feedback_detect import FeedbackSignal
    h = object.__new__(LLMHandler)
    sig = FeedbackSignal(kind="bug", severity="medium", problem="X 错了",
                         source="qq_private", sender="小明")
    with mock.patch("anima.observation.feedback_detect.detect_engineering_feedback",
                    return_value=sig):
        out = h._maybe_record_feedback(
            "你刚才答错了", sender_name="小明", user_id=123, group_id=None,
            detail_type="private", message_id=7, audit=None)
    assert out is sig  # 返回值供 §17 追加确认使用
