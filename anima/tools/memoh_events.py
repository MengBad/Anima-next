"""Memoh 事件订阅 —— 把容器里发生的事主动推给 QQ。

## 为什么需要

委派是「问一句等一句」，但真实任务常常要几十分钟：
  它可能卡在登录、可能中途需要确认、可能跑完时你早就去干别的了。
等着用户回来问「怎么样了」是差的设计。

## 怎么做的

Memoh 有一个 SSE 端点 `/bots/{id}/sessions/events`，持续吐会话活动
（实测能收到 activity_ready / session_compaction）。
本模块订阅它，把有意义的状态变化转成一句人话推到 QQ。

不刷屏的克制：
  - 只在**会话由静转动**（新活动）时说一次
  - 同一会话 5 分钟内不重复播报
  - 只推给 owner 私聊，群里一个字都不发
"""

from __future__ import annotations

import json
import logging
import time

logger = logging.getLogger("anima.memoh_events")

MEMOH_API = "http://127.0.0.1:8080"
MEMOH_BOT = "361f4184-1afe-4f90-9846-5bd532a6fe1f"
MEMOH_USER = "admin"
MEMOH_PASS = "AnimaMemoh2026"

#: 同一会话的播报最小间隔（秒），防刷屏。
MIN_INTERVAL = 300.0


def _login() -> str:
    import urllib.request
    body = json.dumps({"username": MEMOH_USER, "password": MEMOH_PASS}).encode()
    req = urllib.request.Request(
        MEMOH_API + "/auth/login", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return str(json.load(resp).get("access_token") or "")


def _iter_sse(token: str, timeout: float = 3600.0):
    """迭代 SSE 行。阻塞式，调用方应放线程里。"""
    import urllib.request

    url = MEMOH_API + "/bots/" + MEMOH_BOT + "/sessions/events"
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload:
                continue
            try:
                yield json.loads(payload)
            except Exception:
                continue


def narrate(evt: dict) -> str:
    """把一个事件转成一句人话；不值得说的返回空串。"""
    etype = str((evt or {}).get("type") or "")
    if etype == "activity_ready":
        return "那边有新动静了，我在盯着。"
    if etype == "session_compaction":
        return ""  # 内部整理，用户不关心
    return ""


def should_speak(session_id: str, *, now: float = 0.0, state: dict | None = None) -> bool:
    """同一会话的播报节流。纯函数便于单测。"""
    if not session_id:
        return False
    ts = float(now or time.time())
    book = state if state is not None else _LAST
    last = float(book.get(session_id) or 0)
    if ts - last < MIN_INTERVAL:
        return False
    book[session_id] = ts
    return True


_LAST: dict = {}


async def run(send) -> None:
    """订阅事件流，把值得说的推给 owner。

    send: async callable(text: str) —— 由调用方决定怎么发（QQ 私聊）。
    """
    import asyncio

    backoff = 5
    while True:
        try:
            token = await asyncio.to_thread(_login)
            if not token:
                raise RuntimeError("登录失败")
            logger.info("[memoh-events] 已订阅会话事件流")
            backoff = 5
            async for evt in _aiter_sse(token):
                sid = str((evt or {}).get("session_id") or "")
                line = narrate(evt)
                if not line:
                    continue
                if not should_speak(sid or "_global"):
                    continue
                logger.info("[memoh-events] 推送：%s", line)
                try:
                    await send(line)
                except Exception:
                    logger.debug("推送失败（已隔离）", exc_info=True)
        except Exception:
            logger.warning("[memoh-events] 订阅断开，%ds 后重连", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)


async def _aiter_sse(token: str):
    """把阻塞 SSE 迭代器包成 async 迭代器。"""
    import asyncio

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=256)

    def _pump():
        try:
            for evt in _iter_sse(token):
                fut = asyncio.run_coroutine_threadsafe(queue.put(evt), loop)
                fut.result(timeout=30)
        except Exception:
            pass
        finally:
            asyncio.run_coroutine_threadsafe(queue.put(None), loop)

    loop.run_in_executor(None, _pump)
    while True:
        item = await queue.get()
        if item is None:
            return
        yield item