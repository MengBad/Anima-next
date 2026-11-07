"""Memoh 回复清洗：内部协议标记绝不能外泄到 QQ 或 Anima 模型。"""

from __future__ import annotations

import pytest

from anima.tools import memoh as mm
from anima.tools.memoh_sanitize import sanitize, is_clean


@pytest.mark.parametrize("raw,expected", [
    ("你好 <｜DSML｜｜ invoke name=\"exec\"> 世界", "你好 世界"),
    ("<｜DSML｜｜ invoke name=\"exec\">", ""),
    ("<｜DSML｜｜ par", ""),
    ("我先检查一下", "我先检查一下"),
    ("", ""),
])
def test_sanitize(raw, expected):
    assert sanitize(raw) == expected


def test_sanitize_multiline_dsml_block():
    raw = ("<｜DSML｜｜ invoke name=\"exec\">\n"
           "<｜DSML｜｜ parameter name=\"command\">ls\n"
           "这是结果")
    assert "DSML" not in sanitize(raw)
    assert "这是结果" in sanitize(raw)


def test_is_clean_rejects_internal_markers():
    assert is_clean("一句人话")
    assert not is_clean("<｜DSML｜｜ invoke name=\"exec\">")
    assert not is_clean("")


def test_delegate_tool_uses_sanitizer(monkeypatch):
    """memoh.py 的出口必须走共享 sanitizer。"""
    payload = {"items": [{"role": "assistant", "messages": [
        {"type": "text", "content": "<｜DSML｜｜ invoke name=\"exec\">"}]}]}
    monkeypatch.setattr(mm, "_api", lambda *a, **k: payload)
    assert mm._latest_assistant("tok", "sid") == ""


def test_delegate_tool_keeps_clean_text(monkeypatch):
    payload = {"items": [{"role": "assistant", "messages": [
        {"type": "text", "content": "已完成，积分 120"}]}]}
    monkeypatch.setattr(mm, "_api", lambda *a, **k: payload)
    assert mm._latest_assistant("tok", "sid") == "已完成，积分 120"


def test_strip_backend_scaffolding_is_wired(monkeypatch):
    """_strip_backend_scaffolding 不能再是那套失效的老正则。"""
    assert mm._strip_backend_scaffolding("<｜DSML｜｜ invoke name=\"exec\">") == ""
    assert mm._strip_backend_scaffolding("正常回复") == "正常回复"