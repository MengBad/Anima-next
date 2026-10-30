"""ask_user 结构化提问（项目执行书 P1）。

生产实证（2026-10-30）：两次「帮我做个工具」都因缺关键信息跑偏，
而模型没有「问一句」的出口 —— 只能把疑问写进普通回复文本，
agent 循环并不知道它在等人回答。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from anima.agent import ask_user as au


# ── 结构校验 ───────────────────────────────────────────────────────────────
def test_normalize_single_select():
    got = au.normalize_questions([{
        "text": "用哪个 API？", "kind": "single_select",
        "options": [{"label": "和风", "description": "免费"}, "OpenWeather"],
    }])
    assert got[0]["kind"] == "single_select"
    assert len(got[0]["options"]) == 2
    assert got[0]["options"][0]["label"] == "和风"


def test_text_kind_forbids_options():
    got = au.normalize_questions([{"text": "账号是什么？", "kind": "text"}])
    assert got[0]["options"] == []


def test_rejects_empty():
    with pytest.raises(au.AskUserError):
        au.normalize_questions([])
    with pytest.raises(au.AskUserError):
        au.normalize_questions("nope")


def test_rejects_select_without_options():
    with pytest.raises(au.AskUserError):
        au.normalize_questions([{"text": "选一个", "kind": "single_select"}])


def test_rejects_bad_kind():
    with pytest.raises(au.AskUserError):
        au.normalize_questions([{"text": "x", "kind": "dropdown"}])


def test_rejects_too_many_questions():
    qs = [{"text": f"q{i}", "kind": "text"} for i in range(au.MAX_QUESTIONS + 1)]
    with pytest.raises(au.AskUserError):
        au.normalize_questions(qs)


# ── QQ 渲染 ────────────────────────────────────────────────────────────────
def test_render_includes_numbered_options():
    qs = au.normalize_questions([{
        "text": "用哪个？", "kind": "single_select",
        "options": [{"label": "甲", "description": "快"}, {"label": "乙"}],
    }])
    text = au.render_qq(qs)
    assert "用哪个？" in text
    assert "1. 甲" in text
    assert "2. 乙" in text
    assert "也可以直接说别的" in text


def test_render_marks_multi_select():
    qs = au.normalize_questions([{
        "text": "要哪些？", "kind": "multi_select",
        "options": [{"label": "甲"}, {"label": "乙"}],
    }])
    assert "可以选多个" in au.render_qq(qs)


# ── 答案解析 ───────────────────────────────────────────────────────────────
def _q_single():
    return {"text": "用哪个？", "kind": "single_select",
            "options": [{"label": "和风天气"}, {"label": "OpenWeather"}],
            "allow_custom": True}


def test_parse_by_number():
    assert au.parse_answer("1", _q_single())["label"] == "和风天气"
    assert au.parse_answer("2", _q_single())["label"] == "OpenWeather"


def test_parse_by_label():
    assert au.parse_answer("用和风天气吧", _q_single())["label"] == "和风天气"


def test_parse_out_of_range_falls_back_to_text():
    got = au.parse_answer("9", _q_single())
    assert got["kind"] == "text"


def test_parse_free_text():
    got = au.parse_answer("用彩云天气", _q_single())
    assert got == {"kind": "text", "text": "用彩云天气"}


def test_parse_multi():
    q = {"text": "要哪些？", "kind": "multi_select",
         "options": [{"label": "甲"}, {"label": "乙"}, {"label": "丙"}],
         "allow_custom": False}
    got = au.parse_answer("1,3", q)
    assert got["kind"] == "multi"
    assert got["labels"] == ["甲", "丙"]


def test_parse_empty():
    assert au.parse_answer("", _q_single()) is None


# ── 任务挂载与恢复 ─────────────────────────────────────────────────────────
class _Task:
    def __init__(self):
        self.id = "t1"
        self.status = "executing"
        self.pending_question = {}


class _Store:
    def __init__(self):
        self.saved = 0

    def save(self, task):
        self.saved += 1


class _Agent:
    def __init__(self):
        self.store = _Store()


class _Core:
    def __init__(self):
        self._active_agent_task = None
        self.agent = _Agent()


def test_ask_user_tool_sets_waiting_and_renders():
    core = _Core()
    core._active_agent_task = _Task()
    tool = au.make_ask_user_tool(core)

    import asyncio
    out = asyncio.run(tool([{"text": "用哪个？", "kind": "single_select",
                             "options": [{"label": "甲"}]}]))
    assert "用哪个？" in out
    assert core._active_agent_task.status == "waiting_user"
    assert core._active_agent_task.pending_question["questions"]


def test_ask_user_without_task_fails_closed():
    core = _Core()
    tool = au.make_ask_user_tool(core)
    import asyncio
    out = asyncio.run(tool([{"text": "x", "kind": "text"}]))
    assert "错误" in out


def test_resolve_reply_resumes_task():
    core = _Core()
    task = _Task()
    task.status = "waiting_user"
    task.pending_question = {"questions": [_q_single()]}
    core._active_agent_task = task

    out = au.resolve_reply(core, "1")
    assert out is not None and "和风天气" in out
    assert task.status == "executing"
    assert task.pending_question == {}


def test_resolve_reply_ignores_when_no_pending():
    core = _Core()
    core._active_agent_task = _Task()
    assert au.resolve_reply(core, "今天天气不错") is None
