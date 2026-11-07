"""429 限流时跳过同模型重试、直接切下一个候选。"""

from __future__ import annotations

import pytest

from anima.llm import LLMClient


def test_client_has_skip_retry_flag():
    c = LLMClient("http://127.0.0.1:1/v1", "k", "m")
    assert c._skip_retry is False


def test_429_is_failoverable():
    assert LLMClient._is_failoverable_status(429) is True


def test_400_not_failoverable():
    assert LLMClient._is_failoverable_status(400) is False


def test_skip_retry_breaks_loop(monkeypatch):
    """_try_post 里置位后，重试循环必须立刻 break。"""
    src = open("anima/llm/__init__.py", encoding="utf-8").read()
    assert "if self._skip_retry:" in src
    assert "self._skip_retry = True" in src
    assert "break" in src[src.find("if self._skip_retry:"):src.find("if self._skip_retry:") + 120]