# -*- coding: utf-8 -*-
"""P1-C：autonomy 后台任务统一收口——句柄注册、异常留痕、shutdown cancel。"""
import asyncio
import pytest

import anima.core.autonomy as au


async def test_launch_bg_tracks_and_discards():
    async def _ok():
        await asyncio.sleep(0)
        return 1
    t = au._launch_bg(_ok(), "ok")
    assert t in au._background_tasks
    await asyncio.sleep(0.02)
    assert t.done()
    assert t not in au._background_tasks, "完成后应被回调移出"


async def test_launch_bg_exception_logged_not_swallowed(monkeypatch):
    logged = []
    monkeypatch.setattr(au.logger, "exception", lambda *a, **k: logged.append(a))
    async def _boom():
        raise ValueError("bg boom")
    t = au._launch_bg(_boom(), "boom")
    await asyncio.sleep(0.02)
    assert t.done()
    assert logged, "后台异常必须留痕，不得静默"


async def test_shutdown_cancels_pending():
    started = asyncio.Event(); released = asyncio.Event()
    async def _long():
        started.set()
        await released.wait()
    t = au._launch_bg(_long(), "long")
    await started.wait()
    await au._shutdown_background_tasks()
    assert t.cancelled() or t.done()
    assert t not in au._background_tasks
