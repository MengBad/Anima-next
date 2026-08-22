# -*- coding: utf-8 -*-
"""Agentic Harness —— Agentic Runtime 行为级验收（Goal §20 / P2）。

断言「何时该用工具、何时绝不该用」的确定性决策层：
- 简单聊天 → 不触发任何工具引导（绝不为了显得聪明乱调工具）；
- 需要实时信息 → 触发搜索；
- 需要外部资料/代码 → 触发对应工具引导；
- 复杂问题 → TaskPlanner 产出多步计划（含兜底，无 LLM 也能规划）；
- 图片请求 → 触发 want_images（回归 Round 2 修复）。

驱动真实组件（路由正则 + TaskPlanner 确定性兜底），无 LLM 调用，可复现。
"""
from __future__ import annotations

import asyncio

from anima.pipeline.stages.generation import (
    _REALTIME_TOOL_RE,
    _X_SEARCH_RE,
    _X_IMAGE_RE,
    _IMAGE_MANIP_RE,
)
from anima.agent.planner import TaskPlanner


def _route(text: str, has_image: bool = False) -> dict:
    return {
        "realtime": bool(_REALTIME_TOOL_RE.search(text or "")),
        "x_search": bool(_X_SEARCH_RE.search(text or "")),
        "x_image": bool(_X_SEARCH_RE.search(text or "")
                        and _X_IMAGE_RE.search(text or "")),
        "image_manip": has_image and bool(_IMAGE_MANIP_RE.search(text or "")),
    }


def test_simple_chat_uses_no_tool():
    r = _route("你好呀，最近怎么样")
    assert not any(r.values()), r


def test_simple_factual_question_uses_no_tool_when_common_knowledge():
    # 「1+1 等于几」是常识，不应触发搜索
    r = _route("1+1 等于几")
    assert not r["realtime"], r


def test_realtime_question_routes_to_search():
    r = _route("帮我查一下今天天气")
    assert r["realtime"], r


def test_complex_problem_routes_to_search_then_plan():
    # 复杂工程问题：先搜资料，再进入规划
    r = _route("帮我查一下这个东西为什么坏了，分析原因")
    assert r["realtime"], r


def test_image_request_routes_to_want_images():
    r = _route("去X上搜一下这个人的照片")
    assert r["x_search"] and r["x_image"], r


def test_planner_produces_steps_without_llm():
    # 无 LLM（确定性兜底）也能对复杂请求产出有序计划
    planner = TaskPlanner(llm=None)
    steps = asyncio.run(planner.plan("帮我查一下为什么坏了，写个报告，再跑一下验证"))
    assert len(steps) >= 2, steps
    descs = " ".join(s.description for s in steps)
    assert "收集" in descs or "检索" in descs, descs  # 有检索步骤
    assert "验证结果正确性" in descs  # 有验证收尾
    assert "汇总回复主人" in descs  # 有汇总出口


def test_planner_steps_are_ordered_and_indexed():
    planner = TaskPlanner(llm=None)
    steps = asyncio.run(planner.plan("写个总结报告"))
    assert [s.index for s in steps] == list(range(len(steps)))
    assert steps[-1].description == "汇总回复主人"


def test_planner_never_returns_empty():
    planner = TaskPlanner(llm=None)
    for req in ("随便聊聊", "在吗", ""):
        steps = asyncio.run(planner.plan(req))
        assert steps, f"规划器对 {req!r} 不应返回空计划"


def test_planner_llm_parse_falls_back_gracefully():
    planner = TaskPlanner(llm=None)
    # 畸形 LLM 输出也能回退到确定性兜底
    parsed = TaskPlanner._parse_llm("这不是合法 JSON 数组")
    assert parsed == []
    # 但 _decompose 整体仍走兜底
    steps = asyncio.run(planner.plan("查一下问题"))
    assert steps


def test_casual_time_words_do_not_trigger_tools():
    # 回归：裸「最近/今天/现在」不得把闲聊误判为实时查询（Round 6-7 修复）
    for text in (
        "最近怎么样", "最近在忙什么", "最近过得怎么样",
        "今天心情怎么样", "今天过得怎么样", "今天在干嘛",
        "现在怎么样", "现在在忙吗",
    ):
        assert not _REALTIME_TOOL_RE.search(text), f"{text!r} 不应触发搜索工具"


def test_legit_factual_time_queries_still_trigger():
    for text in (
        "最近天气怎么样", "最近有什么新闻", "帮我查一下最近股价",
        "今天天气如何", "今天有什么新闻", "现在几点",
    ):
        assert _REALTIME_TOOL_RE.search(text), f"{text!r} 应触发搜索工具"
