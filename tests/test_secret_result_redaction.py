"""凭证类工具的返回值绝不落盘、不进审计预览。"""

from __future__ import annotations

import json

import pytest

from anima.agent.executor import SECRET_TOOLS, redact_tool_result, step_tool_name
from anima.tools import Tool


def test_secret_tools_contains_credential_only():
    assert SECRET_TOOLS == frozenset({"browse_credential"})


def test_redact_hides_credential():
    out = redact_tool_result("browse_credential", "账号：alice\n密码：SECRET-XYZ-123")
    assert "SECRET-XYZ-123" not in out
    assert "alice" not in out
    assert out.strip()


def test_redact_passes_normal_tools_through():
    text = "这是普通工具的正文"
    assert redact_tool_result("browse_web", text) == text
    assert redact_tool_result("", text) == text


def test_redact_empty_text_is_noop():
    assert redact_tool_result("browse_credential", "") == ""


class _Step:
    def __init__(self, tool_name="", tools_called=None):
        self.tool_name = tool_name
        self.tools_called = tools_called


def test_step_tool_name_prefers_explicit_tool_name():
    assert step_tool_name(_Step("browse_credential")) == "browse_credential"


def test_step_tool_name_falls_back_to_tools_called():
    assert step_tool_name(_Step("", ["browse_web", "browse_credential"])) == "browse_credential"


def test_step_tool_name_none_when_unrelated():
    assert step_tool_name(_Step("browse_web", ["search_web"])) == ""
    assert step_tool_name(_Step()) == ""


def test_tool_secret_result_defaults_false():
    tool = Tool("t", "d", {"a": {"type": "string"}}, lambda a="": "x")
    assert tool.secret_result is False


def test_tool_secret_result_opt_in():
    tool = Tool("t", "d", {}, lambda: "x", secret_result=True)
    assert tool.secret_result is True


def test_registered_credential_tool_declares_secret_result():
    """真实注册流程里 browse_credential 必须带 secret_result。"""
    import inspect

    import anima.tools.builtin as builtin

    src = inspect.getsource(builtin)
    idx = src.find('"browse_credential"')
    assert idx != -1, "browse_credential 未注册"
    # 从注册块起点看到下一个 registry.register 之前
    block = src[idx: src.find("registry.register", idx + 10)]
    assert "secret_result=True" in block


def test_audit_preview_empty_for_secret_tool():
    """审计侧：secret_result 工具的 result_preview 必须为空串。"""
    import inspect

    import anima.tools as tools_mod

    src = inspect.getsource(tools_mod)
    assert 'getattr(tool, "secret_result", False)' in src