# -*- coding: utf-8 -*-
"""生图跨模型容灾：主图像模型遇 429/500 等可重试状态时 fallback 到 image_fallback_models。

覆盖：
- gemini 主模型失败(503) -> failover 到 grok-imagine（走 /images/generations，b64_json）
- 非可重试状态(401)不切模型，直接失败
- 端点路由：imagine 类走 images 端点，gemini 类走 chat 端点
"""
import base64

import aiohttp.web
import pytest

from anima.llm import LLMClient

_PNG = b"\x89PNG\r\n\x1a\n" + b"abc"


async def _server(handlers):
    app = aiohttp.web.Application()
    app.router.add_post("/v1/chat/completions", handlers["chat"])
    app.router.add_post("/v1/images/generations", handlers["images"])
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}/v1"


def _chat_img_response():
    b64 = base64.b64encode(_PNG).decode("ascii")
    return aiohttp.web.json_response({
        "choices": [{"message": {"images": [{"image_url": {"url": f"data:image/png;base64,{b64}"}}]}}]
    })


def _images_response():
    b64 = base64.b64encode(_PNG).decode("ascii")
    return aiohttp.web.json_response({"data": [{"b64_json": b64}]})


@pytest.mark.asyncio
async def test_image_fallback_gemini_fails_then_grok_succeeds():
    """gemini 主模型 503 -> failover 到 grok-imagine（images 端点）成功。"""
    calls = {"chat": 0, "images": 0}

    async def chat(request):
        calls["chat"] += 1
        return aiohttp.web.Response(status=503, text="busy")

    async def images(request):
        calls["images"] += 1
        assert request.query.get("size") is None  # prompt 在 body
        body = await request.json()
        assert body["model"] == "grok-imagine-image"
        return _images_response()

    runner, endpoint = await _server({"chat": chat, "images": images})
    client = LLMClient(
        endpoint, "key", model="gemini-3.7-flash-high",
        image_fallback_models=["grok-imagine-image"],
    )
    out = await client.generate_image("一只猫", model="gemini-3.1-flash-image", max_bytes=100000)
    assert out == _PNG
    assert calls["chat"] == 1, "主模型只试一次"
    assert calls["images"] == 1, "fallback 走 images 端点一次"
    await client.close()
    await runner.cleanup()


@pytest.mark.asyncio
async def test_image_no_fallback_when_auth_failure():
    """401（非可重试状态）不切模型，直接失败。"""
    calls = {"chat": 0, "images": 0}

    async def chat(request):
        calls["chat"] += 1
        return aiohttp.web.Response(status=401, text="bad key")

    async def images(request):
        calls["images"] += 1
        return _images_response()

    runner, endpoint = await _server({"chat": chat, "images": images})
    client = LLMClient(
        endpoint, "key", model="gemini-3.7-flash-high",
        image_fallback_models=["grok-imagine-image"],
    )
    out = await client.generate_image("猫", model="gemini-3.1-flash-image", max_bytes=100000)
    assert out is None
    assert calls["chat"] == 1
    assert calls["images"] == 0, "401 不切模型"
    await client.close()
    await runner.cleanup()


@pytest.mark.asyncio
async def test_image_primary_succeeds_no_fallback():
    """主模型成功时不触发 fallback。"""
    calls = {"chat": 0, "images": 0}

    async def chat(request):
        calls["chat"] += 1
        return _chat_img_response()

    async def images(request):
        calls["images"] += 1
        return _images_response()

    runner, endpoint = await _server({"chat": chat, "images": images})
    client = LLMClient(
        endpoint, "key", model="gemini-3.7-flash-high",
        image_fallback_models=["grok-imagine-image"],
    )
    out = await client.generate_image("猫", model="gemini-3.1-flash-image", max_bytes=100000)
    assert out == _PNG
    assert calls["chat"] == 1
    assert calls["images"] == 0
    await client.close()
    await runner.cleanup()
