"""凭证类工具的返回值绝不落盘、不进审计预览。"""

from __future__ import annotations

import pytest

from anima.agent.executor import SECRET_TOOLS, redact_tool_result
from anima.tools import Tool


def test_secret_tools_contains_credential_only():
    assert SECRET_TOOLS == frozenset({"browse_credential"})


def test_redact_removes_credential_values():
    """凭证值被剔除，其余正文原样保留。"""
    text = "账号：alice\n密码：SECRET-XYZ-123\n页面显示已登录，无验证码"
    out = redact_tool_result(None, text, values=["SECRET-XYZ-123", "alice"])
    assert "SECRET-XYZ-123" not in out
    assert "alice" not in out
    # 🔴 关键回归：页面状态这类正文**必须留着**，否则验收器看不到、任务卡死
    assert "页面显示已登录" in out
    assert "无验证码" in out


def test_redact_keeps_partial_text_around_secret():
    out = redact_tool_result(None, "已登录 alice 的账户", values=["alice"])
    assert "alice" not in out
    assert out.startswith("已登录 ")
    assert out.endswith(" 的账户")


def test_redact_handles_multiple_values():
    out = redact_tool_result(None, "u=alice p=hunter2 x=keep",
                             values=["hunter2", "alice"])
    assert "alice" not in out and "hunter2" not in out
    assert "keep" in out


def test_redact_no_values_is_noop():
    text = "这是普通工具的正文"
    assert redact_tool_result(None, text, values=[]) == text


def test_redact_empty_text_is_noop():
    assert redact_tool_result(None, "", values=["x"]) == ""


def test_redact_no_longer_wipes_whole_step():
    """回归 01f70c6a75：调用过凭证工具的步骤，正文不能被整段抹成占位符。"""
    text = "步骤结果：已打开华为云，截图已保存"
    out = redact_tool_result("browse_credential", text, values=[])
    assert "已打开华为云" in out


def test_tool_secret_result_defaults_false():
    tool = Tool("t", "d", {"a": {"type": "string"}}, lambda a="": "x")
    assert tool.secret_result is False


def test_tool_secret_result_opt_in():
    tool = Tool("t", "d", {}, lambda: "x", secret_result=True)
    assert tool.secret_result is True


def test_registered_credential_tool_declares_secret_result():
    import inspect

    import anima.tools.builtin as builtin

    src = inspect.getsource(builtin)
    idx = src.find('"browse_credential"')
    assert idx != -1, "browse_credential 未注册"
    block = src[idx: src.find("registry.register", idx + 10)]
    assert "secret_result=True" in block


def test_audit_preview_empty_for_secret_tool():
    import inspect

    import anima.tools as tools_mod

    src = inspect.getsource(tools_mod)
    assert 'getattr(tool, "secret_result", False)' in src


def test_ask_user_schema_has_items():
    """回归：questions 只有 type=array 没 items，模型会传字符串数组。"""
    import inspect

    import anima.tools.builtin.agent_tools as at

    src = inspect.getsource(at)
    idx = src.find('"questions"')
    assert idx != -1
    block = src[idx: idx + 1600]
    assert '"items"' in block, "questions 缺 items，模型不知道要传对象"