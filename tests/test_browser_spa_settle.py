# -*- coding: utf-8 -*-
"""浏览器必须等 SPA 渲染完再取正文/截图（2026-11-07）。

## 生产实证

华为云 `/grow` 是纯 JS 页面。`page.goto(wait_until="domcontentloaded")`
一返回就抓正文 / 点击 / 截图 ⇒
  · 截图恒定 **5407 字节**（空白页 PNG），连发 3 张给主人
  · `browse_web` 的 5875 字全为空壳
  · `click(selector)` 找不到元素 → 15s 超时
  · 同任务里正常渲染的截图是 416319 / 36485 字节

bot 据此对外说「浏览器渲染出来是空白」，其实是自己没等 JS 跑完。

## 修法

`_settle_spa(page)`：轮询可见正文长度，连续 N 次不变且 ≥ 阈值才算稳定。
不用 networkidle（SPA 有轮询/长连接，永不 idle）。超时返回 (False, n)，
调用方**如实标注**「可能未渲染完」，不假装成功。

接入点：`_fetch_async` / `_run_actions(navigate)` / `_do_screenshot`。
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from anima.tools import browser


class _Page:
    """模拟 SPA：前几次采样返回空壳，之后正文逐渐长出来并稳定。"""
    def __init__(self, seq, delay=0.0):
        self.seq = list(seq)
        self.delay = delay
        self.calls = 0

    async def evaluate(self, js):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.seq[min(self.calls - 1, len(self.seq) - 1)]


def test_settle_when_text_grows_then_stabilizes():
    """0 → 0 → 120 → 120 ⇒ 稳定，返回 True。"""
    p = _Page([0, 0, 120, 120, 120])
    ok, n = asyncio.run(browser._settle_spa(p))
    assert ok is True
    assert n == 120


def test_settle_false_when_page_stays_blank():
    """一直是空壳 ⇒ 超时返回 False（不要把空壳当成功）。"""
    p = _Page([0])
    ok, n = asyncio.run(browser._settle_spa(p))
    assert ok is False
    assert n == 0


def test_settle_false_when_text_below_threshold():
    """只有几个字（骨架/加载中）⇒ 不算渲染完成。"""
    p = _Page([5])
    ok, _ = asyncio.run(browser._settle_spa(p))
    assert ok is False


def test_page_result_marks_unsettled_as_unreliable():
    """render() 必须如实标注「可能未渲染完」，让下游别据此下结论。"""
    r = browser.PageResult(url="https://x/grow", title="", text="空壳",
                           settled=False, visible_chars=0)
    out = r.render(2000)
    assert "可能未渲染完成" in out
    assert "请勿据此判断" in out


def test_page_result_settled_has_no_warning():
    r = browser.PageResult(url="https://x", title="T", text="正文",
                           settled=True, visible_chars=100)
    out = r.render(2000)
    assert "可能未渲染完成" not in out
    assert "T" in out and "正文" in out


def test_navigate_logs_unsettled_warning():
    src = inspect.getsource(browser._run_actions)
    assert "_settle_spa" in src
    assert "页面可能未渲染完成" in src


def test_screenshot_waits_and_warns():
    src = inspect.getsource(browser._do_screenshot)
    assert "_settle_spa" in src
    assert "此图可能是空壳" in src


def test_fetch_async_waits():
    src = inspect.getsource(browser._fetch_async)
    assert "_settle_spa" in src
    assert "settled" in src
