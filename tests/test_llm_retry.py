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


@pytest.mark.asyncio
async def test_llm_retries_transient_failure_then_succeeds():
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return aiohttp.web.Response(status=503, text="busy")
        return aiohttp.web.json_response({"choices": [{"message": {"content": "ok"}}]})

    runner, endpoint = await _server(handler)
    client = LLMClient(endpoint, "key", retries=1, retry_backoff_sec=0)
    assert await client.chat([{"role": "user", "content": "hi"}]) == "ok"
    assert calls == 2
    assert client.health()["retry_count"] == 1
    await client.close()
    await runner.cleanup()


@pytest.mark.asyncio
async def test_llm_does_not_retry_auth_failure():
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return aiohttp.web.Response(status=401, text="bad key")

    runner, endpoint = await _server(handler)
    client = LLMClient(endpoint, "key", retries=3, retry_backoff_sec=0)
    assert await client.chat([{"role": "user", "content": "hi"}]) == ""
    assert calls == 1
    assert client.health()["last_status"] == 401
    await client.close()
    await runner.cleanup()


@pytest.mark.asyncio
async def test_llm_health_separates_recovered_current_state_from_failure_history():
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return aiohttp.web.Response(status=503, text="busy")
        return aiohttp.web.json_response({"choices": [{"message": {"content": "ok"}}]})

    runner, endpoint = await _server(handler)
    client = LLMClient(endpoint, "key", retries=0, circuit_failure_threshold=3)
    assert await client.chat([{"role": "user", "content": "first"}]) == ""
    failed = client.health()
    assert failed["current_state"] == "degraded"
    assert failed["last_error_at"]

    assert await client.chat([{"role": "user", "content": "second"}]) == "ok"
    recovered = client.health()
    assert recovered["failure_count"] == 1
    assert recovered["current_state"] == "healthy"
    assert recovered["recovered"] is True
    await client.close()
    await runner.cleanup()


@pytest.mark.asyncio
async def test_llm_accepts_per_request_model_override():
    seen = {}

    async def handler(request):
        seen.update(await request.json())
        return aiohttp.web.json_response({"choices": [{"message": {"content": "ok"}}]})

    runner, endpoint = await _server(handler)
    client = LLMClient(endpoint, "key")
    assert await client.chat([{"role": "user", "content": "hi"}], model="vision-model") == "ok"
    assert seen["model"] == "vision-model"
    await client.close()
    await runner.cleanup()


@pytest.mark.asyncio
async def test_llm_fails_over_to_configured_model_on_transient_upstream_failure():
    models = []

    async def handler(request):
        payload = await request.json()
        models.append(payload["model"])
        if payload["model"] == "primary":
            return aiohttp.web.Response(status=503, text="busy")
        return aiohttp.web.json_response({"model": payload["model"], "choices": [{"message": {"content": "fallback ok"}}]})

    runner, endpoint = await _server(handler)
    client = LLMClient(endpoint, "key", model="primary", fallback_models=["secondary"], retries=0)
    assert await client.chat([{"role": "user", "content": "hi"}]) == "fallback ok"
    assert models == ["primary", "secondary"]
    health = client.health()
    assert health["failure_count"] == 0
    assert health["failover_count"] == 1
    assert health["last_failover_to"] == "secondary"
    await client.close()
    await runner.cleanup()


@pytest.mark.asyncio
async def test_llm_circuit_breaker_stops_repeated_transport_attempts():
    calls = 0

    async def handler(_request):
        nonlocal calls
        calls += 1
        return aiohttp.web.Response(status=503, text="busy")

    runner, endpoint = await _server(handler)
    client = LLMClient(endpoint, "key", retries=0, circuit_failure_threshold=1, circuit_cooldown_sec=60)
    assert await client.chat([{"role": "user", "content": "first"}]) == ""
    assert await client.chat([{"role": "user", "content": "second"}]) == ""
    assert calls == 1
    health = client.health()
    assert health["circuit_open_until"]
    assert health["last_error"] == "circuit_open"
    await client.close()
    await runner.cleanup()


@pytest.mark.asyncio
async def test_llm_413_degrades_and_recovers():
    """网关返回 413（请求体过大）时，应剥离图片/截断历史降级重试一次并恢复回复，
    而不是静默丢弃整条回复。不可解码的 data URI 无法再压缩，直接走剥离路径。"""
    bodies = []

    async def handler(request):
        bodies.append(await request.json())
        if len(bodies) == 1:
            return aiohttp.web.Response(
                status=413,
                text='{"error":{"message":"Request body too large. Maximum size is 10485760 bytes."}}',
            )
        return aiohttp.web.json_response({"choices": [{"message": {"content": "recovered"}}]})

    runner, endpoint = await _server(handler)
    client = LLMClient(endpoint, "key", retries=0, retry_backoff_sec=0)
    msgs = [
        {"role": "system", "content": "你是哈基米"},
        {"role": "user", "content": [
            {"type": "text", "text": "看图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]},
    ]
    assert await client.chat(msgs) == "recovered"
    # 第一次 413，第二次降级重试成功 -> 共 2 次调用
    assert len(bodies) == 2
    # 降级后图片块被剥离
    second = bodies[1]["messages"]
    assert not any(
        isinstance(p, dict) and p.get("type") == "image_url"
        for m in second
        for p in (m["content"] if isinstance(m["content"], list) else [])
    )
    health = client.health()
    assert health["degradation_count"] == 1
    assert health["last_degradation_reason"] == "http_413_trim_retry"
    await client.close()
    await runner.cleanup()


@pytest.mark.asyncio
async def test_llm_413_reencodes_real_image_before_strip():
    """可解码图片在 413 后应先再压缩并保留 image_url，而不是立刻剥离视觉。"""
    import base64
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (900, 900), color=(40, 120, 200)).save(buf, format="PNG")
    uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    bodies = []

    async def handler(request):
        bodies.append(await request.json())
        if len(bodies) == 1:
            return aiohttp.web.Response(status=413, text="too large")
        return aiohttp.web.json_response({"choices": [{"message": {"content": "seen"}}]})

    runner, endpoint = await _server(handler)
    client = LLMClient(endpoint, "key", retries=0, retry_backoff_sec=0)
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "看这张图"},
        {"type": "image_url", "image_url": {"url": uri}},
    ]}]
    assert await client.chat(msgs) == "seen"
    assert len(bodies) == 2
    second_parts = bodies[1]["messages"][0]["content"]
    assert any(isinstance(p, dict) and p.get("type") == "image_url" for p in second_parts)
    health = client.health()
    assert health["last_degradation_reason"] == "http_413_reencode_retry"
    await client.close()
    await runner.cleanup()


@pytest.mark.asyncio
async def test_llm_preflight_trim_on_oversize():
    """请求体预检超过 8MB 安全上限时，发送前即裁剪图片，不触发 413。"""
    bodies = []

    async def handler(request):
        bodies.append(await request.json())
        return aiohttp.web.json_response({"choices": [{"message": {"content": "ok"}}]})

    runner, endpoint = await _server(handler)
    client = LLMClient(endpoint, "key", retries=0, retry_backoff_sec=0)
    big = "A" * (9 * 1024 * 1024)  # 9MB base64，超过 8MB 安全上限
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": [
            {"type": "text", "text": "大图"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{big}"}},
        ]},
    ]
    assert await client.chat(msgs) == "ok"
    # 预检即裁剪，服务器只收到 1 次请求且图片已剥离
    assert len(bodies) == 1
    got = bodies[0]["messages"]
    assert not any(
        isinstance(p, dict) and p.get("type") == "image_url"
        for m in got
        for p in (m["content"] if isinstance(m["content"], list) else [])
    )
    health = client.health()
    assert health["degradation_count"] == 1
    assert health["last_degradation_reason"] == "payload_preflight_trim"
    await client.close()
    await runner.cleanup()


@pytest.mark.asyncio
async def test_llm_fallback_chain_skips_exhausted_primary():
    """主模型连续瞬态失败后切到备用；备用成功则 failover 计数+1 且不记最终失败。"""
    hits: list[str] = []

    async def handler(request):
        payload = await request.json()
        model = payload["model"]
        hits.append(model)
        if model == "primary":
            return aiohttp.web.Response(status=502, text="bad gateway")
        if model == "backup-a":
            return aiohttp.web.Response(status=503, text="busy")
        return aiohttp.web.json_response({
            "choices": [{"message": {"content": f"ok:{model}"}}],
        })

    runner, endpoint = await _server(handler)
    client = LLMClient(
        endpoint, "key",
        model="primary",
        fallback_models=["backup-a", "backup-b"],
        retries=0,
        circuit_failure_threshold=10,
    )
    assert await client.chat([{"role": "user", "content": "hi"}]) == "ok:backup-b"
    assert hits == ["primary", "backup-a", "backup-b"]
    health = client.health()
    assert health["failover_count"] >= 1
    assert health["last_failover_to"] == "backup-b"
    assert health["failure_count"] == 0
    await client.close()
    await runner.cleanup()


@pytest.mark.asyncio
async def test_llm_fallback_all_fail_then_circuit_opens():
    """主+备全部失败累计 consecutive_failures，达阈值后熔断。"""
    calls = 0

    async def handler(_request):
        nonlocal calls
        calls += 1
        return aiohttp.web.Response(status=503, text="down")

    runner, endpoint = await _server(handler)
    client = LLMClient(
        endpoint, "key",
        model="primary",
        fallback_models=["fb1"],
        retries=0,
        circuit_failure_threshold=2,
        circuit_cooldown_sec=120,
    )
    assert await client.chat([{"role": "user", "content": "a"}]) == ""
    # 第一次请求：primary + fb1 各试一次，整次记 1 次失败
    assert await client.chat([{"role": "user", "content": "b"}]) == ""
    health = client.health()
    assert health["circuit_open_until"]
    assert float(health["circuit_open_until"] or 0) > 0
    # 熔断后不再打上游（last_error 为 circuit_open）
    before = calls
    assert await client.chat([{"role": "user", "content": "c"}]) == ""
    assert calls == before
    assert client.health()["last_error"] == "circuit_open"
    await client.close()
    await runner.cleanup()


@pytest.mark.asyncio
async def test_llm_auth_failure_does_not_failover():
    """401 非瞬态：不切备用模型。"""
    models: list[str] = []

    async def handler(request):
        payload = await request.json()
        models.append(payload["model"])
        return aiohttp.web.Response(status=401, text="bad key")

    runner, endpoint = await _server(handler)
    client = LLMClient(
        endpoint, "key",
        model="primary",
        fallback_models=["secondary"],
        retries=0,
    )
    assert await client.chat([{"role": "user", "content": "hi"}]) == ""
    assert models == ["primary"]
    await client.close()
    await runner.cleanup()
