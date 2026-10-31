"""P1-C 反问兜底。

A/B 实测依据：加硬规则后 ask_user 命中 3/4，仍有 1/4 用文字反问。
任务丢失对 owner 完全无感 —— 回复通顺，只是那条任务没了。
本兜底把「文字反问 + 零工具调用 + 有任务意图」转成结构化等待。
"""
from __future__ import annotations

import pytest

from anima.agent.ask_backstop import build_pending_question, is_ask_back


def test_detects_typical_ask_back():
    r = is_ask_back("主人是要找哪位画师呀？把名字告诉我",
                    detail_type="private", tool_calls=0)
    assert r is not None


def test_detects_code_written_without_saving():
    """生产实证：模型直接吐了一段代码，没保存没执行，等于白干。"""
    r = is_ask_back("主人~ 这个简单！给你写了一个用 Python 写的轻量级服务，发我服务器地址吧",
                    detail_type="private", tool_calls=0)
    assert r is not None


def test_group_never_converts():
    """群里问「是谁」会暴露它不是人。"""
    r = is_ask_back("主人是要找哪位画师呀？告诉我",
                    detail_type="group", tool_calls=0)
    assert r is None


def test_tool_calls_disable_backstop():
    """调了工具说明在干活，不是干等。"""
    r = is_ask_back("帮我查一下，是哪位？",
                    detail_type="private", tool_calls=1)
    assert r is None


def test_pure_chat_question_not_converted():
    """闲聊里的问号不该被当成任务。"""
    for t in ("你是猫吗？", "今天天气怎么样？", "在吗？"):
        assert is_ask_back(t, detail_type="private", tool_calls=0) is None, t


def test_completed_work_not_converted():
    """已经给出代码/结论的回复，不该转挂起。"""
    for t in ("帮我查的代码如下：\n```python\nprint(1)\n```",
              "帮我查到了，结果是 42"):
        assert is_ask_back(t, detail_type="private", tool_calls=0) is None, t


def test_long_reply_not_converted():
    """长回复多半是完整答复，不是一句追问。"""
    long_text = "帮我做完了。" + "嗯" * 600 + "？"
    assert is_ask_back(long_text, detail_type="private", tool_calls=0) is None


def test_empty_text():
    assert is_ask_back("", detail_type="private", tool_calls=0) is None
    assert is_ask_back(None, detail_type="private", tool_calls=0) is None


def test_build_pending_question_shape():
    q = build_pending_question("你要找哪位画师呀？")
    assert q["source"] == "ask_backstop"
    inner = q["questions"][0]
    assert inner["kind"] == "text"      # 开放问题不硬套选项
    assert inner["allow_custom"] is True
    assert "画师" in inner["text"]


def test_build_pending_truncates():
    q = build_pending_question("x" * 1000)
    assert len(q["questions"][0]["text"]) <= 300
