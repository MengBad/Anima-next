"""Memoh 事件订阅：把进展主动推到 QQ。"""

from __future__ import annotations

from anima.tools import memoh_events as me


def test_narrate_activity_ready():
    assert me.narrate({"type": "activity_ready"}) != ""


def test_narrate_ignores_noise():
    assert me.narrate({"type": "session_compaction"}) == ""
    assert me.narrate({}) == ""
    assert me.narrate({"type": "whatever"}) == ""


def test_should_speak_throttles():
    book: dict = {}
    assert me.should_speak("s1", now=1000.0, state=book) is True
    assert me.should_speak("s1", now=1010.0, state=book) is False, "10 秒内不该重复"
    assert me.should_speak("s1", now=1000.0 + me.MIN_INTERVAL + 1, state=book) is True


def test_should_speak_separate_sessions():
    book: dict = {}
    assert me.should_speak("a", now=1000.0, state=book) is True
    assert me.should_speak("b", now=1000.0, state=book) is True


def test_should_speak_rejects_empty():
    assert me.should_speak("", now=1000.0, state={}) is False