# -*- coding: utf-8 -*-
"""Tool Routing Harness —— 意图→工具路由的确定性验收（Goal Phase G / P2）。

测量「哪些话该触发工具、哪些不该」，覆盖 Goal 的核心场景：
- 普通聊天（你好）→ 不触发任何工具引导；
- 需要实时信息（今天天气/查一下为什么坏了）→ 触发实时/搜索工具；
- 明确要 X 上的图 → x_search + want_images；
- 图片操作请求 → 图片处理引导；
- 有图但无操作关键词 → 仅提示「沙箱有图可用」。

这些正则/判定是真实 intent→tool 路由层（generation_stage），本 harness 驱动它们
做行为级断言，防止「该用工具不用 / 不该用乱用」的回归。
"""
from __future__ import annotations

from anima.pipeline.stages.generation import (
    _REALTIME_TOOL_RE,
    _X_SEARCH_RE,
    _X_IMAGE_RE,
    _IMAGE_MANIP_RE,
)


def classify(text: str, *, has_image: bool = False) -> dict:
    """把一条用户消息分类为工具路由意图（确定性，无 LLM）。"""
    return {
        "realtime": bool(_REALTIME_TOOL_RE.search(text or "")),
        "x_search": bool(_X_SEARCH_RE.search(text or "")),
        "x_image": bool(_X_SEARCH_RE.search(text or "")
                        and _X_IMAGE_RE.search(text or "")),
        "image_manip": has_image and bool(_IMAGE_MANIP_RE.search(text or "")),
        "image_present": has_image,
    }


def test_plain_chat_triggers_no_tool():
    r = classify("你好呀")
    assert not r["realtime"], r
    assert not r["x_search"], r
    assert not r["image_manip"], r


def test_realtime_question_triggers_realtime_tool():
    for q in ("今天天气怎么样", "帮我查一下这个东西为什么坏了", "现在几点了"):
        assert classify(q)["realtime"], q


def test_x_search_with_image_request_triggers_want_images():
    r = classify("去推特上搜一下这个人的照片")
    assert r["x_search"], r
    assert r["x_image"], r


def test_x_search_without_image_request_does_not_force_images():
    r = classify("去X上搜一下这个人是谁")
    assert r["x_search"], r
    assert not r["x_image"], r


def test_image_manip_triggers_when_image_present():
    r = classify("帮我把这张图翻转一下", has_image=True)
    assert r["image_manip"], r


def test_image_manip_does_not_trigger_without_image():
    # 没图时「翻转」不应触发图片处理引导（沙箱里没有图）
    r = classify("帮我把图翻转一下", has_image=False)
    assert not r["image_manip"], r


def test_image_present_without_manip_keyword_is_marked():
    # 有图但没操作词 → 走 image_present 提示（沙箱有图可用），而非 manip 引导
    r = classify("这是什么", has_image=True)
    assert r["image_present"], r
    assert not r["image_manip"], r
