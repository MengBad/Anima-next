# -*- coding: utf-8 -*-
"""P3 —— handler 反馈识别钩子（_maybe_record_feedback）测试。

验证：识别到工程反馈时 emit 审计事件 + 记日志；普通聊天不 emit；识别失败不抛。
只审计不执行：绝不改变回复、绝不调用 DevLoop/部署。
"""
from __future__ import annotations

from unittest import mock

from anima.llm.handler import LLMHandler


def _handler():
    return object.__new__(LLMHandler)


class _Audit:
    def __init__(self):
        self.events = []

    def emit(self, name, **kwargs):
        self.events.append((name, kwargs))


def test_feedback_text_emits_audit():
    h = _handler()
    audit = _Audit()
    h._maybe_record_feedback(
        "你刚才回答错了，你把 A 和 B 搞混了",
        sender_name="小明", user_id=10001, group_id=727, detail_type="group",
        message_id=999, audit=audit,
    )
    assert audit.events, "应 emit engineering_feedback_detected"
    name, kwargs = audit.events[0]
    assert name == "engineering_feedback_detected"
    assert kwargs["kind"] in ("behavior", "bug", "memory")
    assert kwargs["user_id"] == 10001
    assert kwargs["group_id"] == 727
    assert kwargs["severity"] in ("normal", "high")
    assert "错" in kwargs["problem"]


def test_plain_chat_does_not_emit():
    h = _handler()
    audit = _Audit()
    h._maybe_record_feedback(
        "今天天气不错呀", sender_name="小明", user_id=10001, group_id=None,
        detail_type="private", message_id=1, audit=audit,
    )
    assert audit.events == []


def test_private_image_feedback_source():
    h = _handler()
    audit = _Audit()
    h._maybe_record_feedback(
        "你这个图片识别有问题，明明是猫你说成狗",
        sender_name="小雨", user_id=20002, group_id=None, detail_type="private",
        message_id=2, audit=audit,
    )
    name, kwargs = audit.events[0]
    assert kwargs["source"] == "qq_private"
    assert kwargs["kind"] == "image"


def test_no_audit_object_is_safe():
    # audit 为 None 时绝不抛异常
    h = _handler()
    h._maybe_record_feedback(
        "你错了", sender_name="x", user_id=1, group_id=None,
        detail_type="private", message_id=1, audit=None,
    )


def test_detection_failure_is_swallowed():
    h = _handler()
    audit = _Audit()
    with mock.patch(
        "anima.llm.handler.logger",
    ):
        # 即便识别层抛异常也不应冒泡
        with mock.patch(
            "anima.observation.feedback_detect.detect_engineering_feedback",
            side_effect=RuntimeError("boom"),
        ):
            h._maybe_record_feedback(
                "你错了", sender_name="x", user_id=1, group_id=None,
                detail_type="private", message_id=1, audit=audit,
            )
    # 未 emit（识别抛异常被吞掉），且不抛
    assert audit.events == []


def test_hook_never_auto_executes():
    # 钩子只审计，不返回任何执行动作；绝不调用 DevLoop/部署
    h = _handler()
    audit = _Audit()
    with mock.patch("anima.agent.devloop.DevLoopOrchestrator.run_issue") as run:
        h._maybe_record_feedback(
            "你错了", sender_name="x", user_id=1, group_id=None,
            detail_type="private", message_id=1, audit=audit,
        )
        run.assert_not_called()
