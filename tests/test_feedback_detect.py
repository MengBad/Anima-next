# -*- coding: utf-8 -*-
"""P3 —— 工程反馈识别（Feedback Capture）单元测试。

覆盖 Goal §13-14 的命名场景：
- 群聊「@Bot 你刚才回答错了，你把 A 和 B 搞混了」→ behavior/bug 反馈
- 私聊「你这个图片识别有问题，明明是猫你说成狗」→ image 反馈
- 普通聊天（你好/谢谢/今天天气）→ 不误判为反馈
- 分类：bug/behavior/memory/persona/tool/image/system
- fail-closed：拿不准当普通聊天
- 只识别不执行：to_dict 无任何 execute/disable/deploy 动作字段
"""
from __future__ import annotations

from anima.observation.feedback_detect import (
    detect_engineering_feedback,
    FeedbackSignal,
)


def test_group_correction_is_feedback():
    sig = detect_engineering_feedback(
        "你刚才回答错了，你把 A 和 B 搞混了",
        sender_name="小明", message_id=123, conversation_id=456, source="qq_group",
    )
    assert sig is not None
    assert sig.kind in ("behavior", "bug", "memory")
    assert sig.source == "qq_group"
    assert sig.sender == "小明"
    assert sig.message_id == 123
    assert sig.conversation_id == 456
    assert sig.problem.startswith("你刚才回答错了")
    assert sig.evidence  # 有可溯源证据


def test_private_image_misrecognition_is_image_feedback():
    sig = detect_engineering_feedback(
        "你这个图片识别有问题，明明是猫你说成狗",
        sender_name="小雨", source="qq_private",
    )
    assert sig is not None
    assert sig.kind == "image"
    assert sig.source == "qq_private"


def test_memory_feedback_classified():
    sig = detect_engineering_feedback("你记错了，我昨天说的是周六不是周日")
    assert sig is not None
    assert sig.kind == "memory"


def test_tool_feedback_classified():
    sig = detect_engineering_feedback("你搜的结果不对，那明明是去年的新闻")
    assert sig is not None
    assert sig.kind == "tool"


def test_persona_feedback_classified():
    sig = detect_engineering_feedback("你说话语气不对，不该这么正式")
    assert sig is not None
    assert sig.kind == "persona"


def test_bug_feedback_classified():
    sig = detect_engineering_feedback("你又崩溃了，一直重复同一句话")
    assert sig is not None
    assert sig.kind == "bug"


def test_plain_chat_is_not_feedback():
    for t in ("你好呀", "谢谢", "今天天气怎么样", "我昨天跟你说的事还记得吗",
              "帮我看一下这张图是什么", "在吗"):
        assert detect_engineering_feedback(t) is None, t


def test_vague_error_without_self_reference_is_not_feedback():
    # 拿不准当普通聊天（fail-closed）：没有纠错 Bot 的明确指向
    assert detect_engineering_feedback("今天天气不错") is None


def test_long_text_is_not_feedback():
    long_msg = "这是一段很长的聊天内容" + "啊" * 220
    assert detect_engineering_feedback(long_msg) is None


def test_signal_bridges_to_observation_event():
    sig = detect_engineering_feedback("你识别错了，这是猫不是狗")
    ev = sig.to_observation_event()
    assert ev.type == "user_feedback"
    assert ev.source == "feedback"
    assert ev.severity in ("normal", "high")
    assert ev.title.startswith("[image]")


def test_signal_never_auto_executes():
    # 只建议不执行：FeedbackSignal 没有任何 execute/disable/deploy 动作字段
    sig = detect_engineering_feedback("你错了")
    d = sig.to_dict()
    assert "execute" not in d
    assert "disable" not in d
    assert "deploy" not in d
    assert "approve" not in d
