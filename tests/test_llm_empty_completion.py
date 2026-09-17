# -*- coding: utf-8 -*-
"""P0（2026-09-18）：上游「HTTP 200 但补全为空」必须判定为失败，并允许 failover。

生产现象（09-17 实测 25/1296 次 = 1.9% 零输出）：
  `_is_failoverable_status()` **只看 HTTP 码** → `200` 不在可重试集合里
  → `_try_post` 任何 200 直接 return（`retries` 都不消耗）
  → `_post` 见 `data is not None` 即当成功 → `fallback_models` **永无机会**、
    熔断器不计数 → 调用方拿到空串 → `react.py:166` 兜底 `（思考中...）`
  → `generation.py:771` 换成硬编码罐头「本喵刚刚脑子卡了一下…」。
本文件把「200-空 = 失败」这一契约钉死，并锁住它的**边界**（有 tool_calls 不算空）
与**成本上界**（不消耗原地重试）。
"""
import aiohttp.web
import pytest

from anima.llm import LLMClient


async def _server(handler):
    app = aiohttp.web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}/v1"


def _json(body):
    return aiohttp.web.json_response(body)


def _empty(model=None):
    body = {"choices": [{"message": {"content": ""}}]}
    if model:
        body["model"] = model
    return body


def _ok(text, model=None):
    body = {"choices": [{"message": {"content": text}}]}
    if model:
        body["model"] = model
    return body


@pytest.mark.asyncio
async def test_empty_completion_fails_over_to_fallback_model():
    """主模型 200+空 content → 必须切备用模型，而不是把空串当成功返回。"""
    models = []

    async def handler(request):
        payload = await request.json()
        models.append(payload["model"])
        if payload["model"] == "primary":
            return _json(_empty(payload["model"]))
        return _json(_ok("fallback ok", payload["model"]))

    runner, endpoint = await _server(handler)
    client = LLMClient(endpoint, "key", model="primary",
                       fallback_models=["secondary"], retries=2, retry_backoff_sec=0)
    assert await client.chat([{"role": "user", "content": "hi"}]) == "fallback ok"
    assert models == ["primary", "secondary"], f"应切备用模型，实际 {models}"
    health = client.health()
    assert health["failover_count"] == 1
    assert health["last_failover_to"] == "secondary"
    assert health["empty_completion_count"] == 1
    assert health["failure_count"] == 0, "备用接住后不应记最终失败"
    assert health["consecutive_failures"] == 0
    assert health["last_status"] == 200, "last_status 必须保留真实的 HTTP 码（不篡改遥测）"
    await client.close()
    await runner.cleanup()


@pytest.mark.asyncio
async def test_empty_completion_does_not_consume_inplace_retries():
    """成本上界：200-空 不消耗原地重试（否则 3 模型 × 3 次 + 退避 会拖垮延迟）。"""
    calls = []

    async def handler(request):
        payload = await request.json()
        calls.append(payload["model"])
        return _json(_empty(payload["model"]))

    runner, endpoint = await _server(handler)
    client = LLMClient(endpoint, "key", model="primary",
                       fallback_models=["secondary"], retries=3, retry_backoff_sec=0)
    assert await client.chat([{"role": "user", "content": "hi"}]) == ""
    assert calls == ["primary", "secondary"], f"每个候选模型只应打一次，实际 {calls}"
    health = client.health()
    assert health["empty_completion_count"] == 2
    assert health["retry_count"] == 0, "空补全不应消耗 retry_count"
    assert health["failure_count"] == 1
    assert health["consecutive_failures"] == 1
    assert health["last_error"] == "empty_completion"
    await client.close()
    await runner.cleanup()


@pytest.mark.asyncio
async def test_tool_call_response_is_not_empty_completion():
    """边界：content 为空但带 tool_calls 是**合法**响应，绝不能判失败/切模型。"""
    models = []
    tool_calls = [{"id": "c1", "type": "function",
                   "function": {"name": "get_member_facts", "arguments": "{}"}}]

    async def handler(request):
        payload = await request.json()
        models.append(payload["model"])
        return _json({"model": payload["model"], "choices": [{
            "finish_reason": "tool_calls",
            "message": {"content": "", "tool_calls": tool_calls},
        }]})

    runner, endpoint = await _server(handler)
    client = LLMClient(endpoint, "key", model="primary", fallback_models=["secondary"])
    result = await client.chat_structured(
        [{"role": "user", "content": "hi"}], tools=[{"type": "function", "function": {"name": "x"}}],
    )
    assert result["tool_calls"] == tool_calls
    assert models == ["primary"], "带 tool_calls 的空 content 不应触发换模型"
    health = client.health()
    assert health["empty_completion_count"] == 0
    assert health["failover_count"] == 0
    await client.close()
    await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("body,label", [
    ({"choices": []}, "choices 为空"),
    ({}, "无 choices 字段"),
    ({"choices": [{"message": {"content": "   \n  "}}]}, "纯空白 content"),
    ({"choices": [{"message": {"content": None}}]}, "content 为 None"),
    ({"choices": [{"message": {"content": "", "reasoning_content": "想了半天"}}]}, "只有 reasoning_content"),
    ({"choices": [{"message": {}}]}, "message 无 content"),
])
async def test_empty_variants_all_fail_over(body, label):
    """所有「空补全」形态都必须 failover；调用方拿到的 content 为空 = 用户看到罐头话术。"""
    models = []

    async def handler(request):
        payload = await request.json()
        models.append(payload["model"])
        if payload["model"] == "primary":
            return _json(body)
        return _json(_ok("rescued", payload["model"]))

    runner, endpoint = await _server(handler)
    client = LLMClient(endpoint, "key", model="primary", fallback_models=["secondary"])
    assert await client.chat([{"role": "user", "content": "hi"}]) == "rescued", f"未 failover：{label}"
    assert models == ["primary", "secondary"], f"{label}：实际 {models}"
    await client.close()
    await runner.cleanup()


@pytest.mark.asyncio
async def test_normal_response_still_succeeds_without_failover():
    """回归护栏：正常 200 有 content 时行为一字不变（不误伤）。"""
    models = []

    async def handler(request):
        payload = await request.json()
        models.append(payload["model"])
        return _json(_ok("正常回复", payload["model"]))

    runner, endpoint = await _server(handler)
    client = LLMClient(endpoint, "key", model="primary", fallback_models=["secondary"])
    assert await client.chat([{"role": "user", "content": "hi"}]) == "正常回复"
    assert models == ["primary"]
    health = client.health()
    assert health["empty_completion_count"] == 0
    assert health["failover_count"] == 0
    assert health["failure_count"] == 0
    await client.close()
    await runner.cleanup()


@pytest.mark.asyncio
async def test_non_failoverable_status_still_does_not_switch_models():
    """回归护栏：401 仍不切模型（本次改动只在 200-空 这一条路径上放宽）。"""
    calls = 0

    async def handler(_request):
        nonlocal calls
        calls += 1
        return aiohttp.web.Response(status=401, text="bad key")

    runner, endpoint = await _server(handler)
    client = LLMClient(endpoint, "key", model="primary",
                       fallback_models=["secondary"], retries=3)
    assert await client.chat([{"role": "user", "content": "hi"}]) == ""
    assert calls == 1
    assert client.health()["last_status"] == 401
    await client.close()
    await runner.cleanup()
