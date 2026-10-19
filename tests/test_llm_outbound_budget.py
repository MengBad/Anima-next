# -*- coding: utf-8 -*-
"""出站 LLM 轮次预算（D-39，2026-10-19）。

生产实证：24h 里真实回复轮次 **p50 5419 / p90 105022 / max 209215 ms**（n=35），
最慢三轮 `generation_stage` 独占 102945 / 103290 / 107354 ms —— 用户等两分钟，
收到的第一句是「本喵这边卡壳了喵」。

根因：`react.turn_deadline_sec=90` 只约束 ReAct 内部；出站调用是
`llm.timeout=60s × retries=2 × 6 个候选` ⇒ 理论最坏 1080s，没人管总账。

修法（语义边界**必须**守住）：
  * 预算只在 `llm_scope(..., outbound=True)` 内生效 —— 旁路/后台任务不该被
    QQ 的体验预算牵连，否则后台记忆整理会被莫名掐断；
  * `outbound_turn_budget_sec <= 0` ⇒ 关闭，行为与修复前**逐字相同**（回滚点）；
  * 单次尝试的超时也不得超过剩余预算（否则一次 60s 就能把 45s 预算吃穿）。
"""
from __future__ import annotations

import asyncio
import time

import aiohttp.web
import pytest

from anima.llm import LLMClient
from anima.llm.usage import llm_scope


async def _server(handler):
    app = aiohttp.web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}/v1"


@pytest.mark.asyncio
async def test_outbound_budget_caps_a_blackhole_upstream():
    """上游只回 503 且不结束 → 出站必须在预算内放弃（不能等 timeout×retries×候选）。"""
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return aiohttp.web.Response(status=503, text="busy")

    runner, endpoint = await _server(handler)
    client = LLMClient(
        endpoint, "key", timeout=60.0, retries=3, retry_backoff_sec=0.0,
        fallback_models=["m2", "m3", "m4", "m5"],
        outbound_turn_budget_sec=1.5, outbound_retries=3,
    )
    t0 = time.perf_counter()
    with llm_scope("chat", "chat", outbound=True):
        out = await client.chat([{"role": "user", "content": "hi"}])
    elapsed = time.perf_counter() - t0
    await client.close()
    await runner.cleanup()

    assert out == "", "预算耗尽时返回空，交给上层落兜底"
    assert elapsed < 3.0, "出站必须被预算截住，实际耗时 %.2fs" % elapsed
    assert client.health()["last_error"].startswith("turn_budget_exhausted")
    # 预算耗尽后不该把 5 个候选 × 4 次全试完
    assert calls < 5 * 4, "候选/重试没有被预算截断，调用 %d 次" % calls


@pytest.mark.asyncio
async def test_budget_disabled_keeps_old_behaviour():
    """0 = 关闭：与修复前一致（这里用 401 非重试错误验"不额外限制"）。"""
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return aiohttp.web.Response(status=401, text="bad key")

    runner, endpoint = await _server(handler)
    client = LLMClient(endpoint, "key", retries=2, retry_backoff_sec=0.0,
                       fallback_models=["m2"], outbound_turn_budget_sec=0)
    with llm_scope("chat", "chat", outbound=True):
        assert await client.chat([{"role": "user", "content": "hi"}]) == ""
    await client.close()
    await runner.cleanup()
    assert client._outbound_deadline is None, "关闭时不应设置 deadline"
    assert calls == 1, "401 不可重试，只该打一次"


@pytest.mark.asyncio
async def test_background_scope_is_not_capped():
    """旁路/后台（outbound=False）不受预算约束 —— 这是刻意的语义边界。"""
    async def handler(request):
        return aiohttp.web.Response(status=503, text="busy")

    runner, endpoint = await _server(handler)
    client = LLMClient(endpoint, "key", retries=0, retry_backoff_sec=0.0,
                       fallback_models=[], outbound_turn_budget_sec=1.0)
    with llm_scope("memory", "summarize", outbound=False):
        await client.chat([{"role": "user", "content": "hi"}])
        assert client._outbound_deadline is None, "后台调用不得被出站预算限制"
    await client.close()
    await runner.cleanup()


@pytest.mark.asyncio
async def test_outbound_retries_override_is_used():
    """出站用 `outbound_retries`，后台仍用 `retries`。"""
    counts = {"n": 0}

    async def handler(request):
        counts["n"] += 1
        return aiohttp.web.Response(status=503, text="busy")

    runner, endpoint = await _server(handler)
    # 出站：outbound_retries=0 ⇒ 单候选只打 1 次；候选只有 1 个
    c1 = LLMClient(endpoint, "key", retries=3, retry_backoff_sec=0.0,
                   outbound_turn_budget_sec=30, outbound_retries=0)
    with llm_scope("chat", "chat", outbound=True):
        await c1.chat([{"role": "user", "content": "hi"}])
    assert counts["n"] == 1, "出站应遵守 outbound_retries=0（1 次尝试），实际 %d" % counts["n"]
    await c1.close()

    # 后台：仍用 retries=3 ⇒ 4 次尝试
    counts["n"] = 0
    c2 = LLMClient(endpoint, "key", retries=3, retry_backoff_sec=0.0,
                   outbound_turn_budget_sec=30, outbound_retries=0)
    with llm_scope("memory", "summarize", outbound=False):
        await c2.chat([{"role": "user", "content": "hi"}])
    assert counts["n"] == 4, "后台应仍按 retries=3（4 次），实际 %d" % counts["n"]
    await c2.close()
    await runner.cleanup()


@pytest.mark.asyncio
async def test_budget_does_not_break_normal_fast_success():
    """正常快成功不受影响（预算只截慢/失败路径）。"""
    async def handler(request):
        return aiohttp.web.json_response({"choices": [{"message": {"content": "ok"}}]})

    runner, endpoint = await _server(handler)
    client = LLMClient(endpoint, "key", outbound_turn_budget_sec=45.0, outbound_retries=1)
    with llm_scope("chat", "chat", outbound=True):
        assert await client.chat([{"role": "user", "content": "hi"}]) == "ok"
    await client.close()
    await runner.cleanup()


def test_config_exposes_the_two_keys_with_safe_defaults():
    """配置面：新增键必须有 pydantic 字段（否则只写 yaml 会被静默丢弃）。"""
    from anima.core.config import LLMConfig

    cfg = LLMConfig()
    assert cfg.outbound_turn_budget_sec == 45.0
    assert cfg.outbound_retries == 1
