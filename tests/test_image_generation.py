from __future__ import annotations

from types import SimpleNamespace
import base64

from aiohttp import web
import pytest

from anima.llm import LLMClient
from anima.media_generation import ImageGenerationManager

_PNG = b"\x89PNG\r\n\x1a\n" + b"test-image"


@pytest.mark.asyncio
async def test_image_generation_persists_png_and_enforces_daily_budget(tmp_path):
    calls = []

    class LLM:
        async def generate_image(self, prompt, *, model, max_bytes):
            calls.append((prompt, model, max_bytes))
            return _PNG

    audit = SimpleNamespace(events=[], emit=lambda event_type, **fields: audit.events.append((event_type, fields)))
    config = SimpleNamespace(
        persistence=SimpleNamespace(data_dir=str(tmp_path)),
        image_generation=SimpleNamespace(enabled=True, model="image-model", daily_limit=1,
                                          max_output_bytes=1024),
    )
    core = SimpleNamespace(config=config, llm=LLM(), audit=audit)
    manager = ImageGenerationManager(core)
    path = await manager.generate("画一只猫")
    assert path.endswith(".png")
    assert (tmp_path / "generated").exists()
    assert calls == [("画一只猫", "image-model", 1024)]
    with pytest.raises(ValueError, match="额度"):
        await manager.generate("再画一只猫")
    assert audit.events[0][0] == "image_generated"


@pytest.mark.asyncio
async def test_image_generation_unlimited_when_daily_limit_zero(tmp_path):
    calls = []

    class LLM:
        async def generate_image(self, prompt, *, model, max_bytes):
            calls.append((prompt, model, max_bytes))
            return _PNG

    config = SimpleNamespace(
        persistence=SimpleNamespace(data_dir=str(tmp_path)),
        image_generation=SimpleNamespace(enabled=True, model="image-model", daily_limit=0,
                                          max_output_bytes=1024),
    )
    core = SimpleNamespace(config=config, llm=LLM(), audit=None)
    manager = ImageGenerationManager(core)
    for _ in range(3):
        path = await manager.generate("画一只猫")
        assert path.endswith(".png")
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_image_generation_requires_explicit_enablement(tmp_path):
    config = SimpleNamespace(
        persistence=SimpleNamespace(data_dir=str(tmp_path)),
        image_generation=SimpleNamespace(enabled=False, model="x", daily_limit=1, max_output_bytes=1024),
    )
    manager = ImageGenerationManager(SimpleNamespace(config=config, llm=None, audit=None))
    with pytest.raises(ValueError, match="未启用"):
        await manager.generate("猫")


@pytest.mark.asyncio
async def test_llm_image_endpoint_uses_b64_json_without_size():
    received = {}

    async def endpoint(request):
        received.update(await request.json())
        url = "data:image/png;base64," + base64.b64encode(_PNG).decode()
        return web.json_response({"choices": [{"message": {"images": [{"image_url": {"url": url}}]}}]})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", endpoint)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    client = LLMClient(f"http://127.0.0.1:{port}/v1", "key")
    try:
        assert await client.generate_image("蓝色圆形", model="image-model", max_bytes=1024) == _PNG
        assert received == {"model": "image-model",
                            "messages": [{"role": "user",
                                          "content": [{"type": "text", "text": "蓝色圆形"}]}],
                            "max_tokens": 4096}
    finally:
        await client.close()
        await runner.cleanup()
