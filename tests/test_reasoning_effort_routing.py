# -*- coding: utf-8 -*-
"""P1（2026-10-02）：`reasoning_effort` 按模块下发，且**未配置的模块必须完全不变**。

实测（生产，10-01 23:5x，带 tools 的抽取任务，每档 3 次取中位）：

| 模型        | 不传            | minimal                     |
|-------------|-----------------|-----------------------------|
| 3.6-flash   | reasoning 505 / 4.1s / comp 532 | **reasoning 无 / 2.0s / comp 33** |
| 3-flash     | reasoning 235 / 2.0s / comp 287 | **reasoning 无 / 1.6s / comp 33** |
| 3.8-flash-high | reasoning 173 / 4.8s        | reasoning 168 / 7.4s（**未见收益**） |

⇒ 收益只在 3.6/3-flash 上明确成立，**主模型证据冲突**（与 10-01 纯文本实验的
"minimal → reasoning 0、−29%" 不一致）。故本轮**只给已验证的模块配 minimal**，
其余模块一律不下发 —— additive、零回归，主链路行为逐字节不变。

另：该参数**只对 `gemini` 系模型下发**（按 payload 里最终要发的 model 判定）。
它不是通用参数 —— 是拿 gemini 网关实测标定的；对不认识的模型硬发可能 400，
那是「把一次可选优化变成一次故障」，所以宁可漏发。
"""
import aiohttp.web
import pytest

from anima.llm import LLMClient
from anima.llm.usage import llm_scope

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def _endpoint():
    app = aiohttp.web.Application()
    runner = None

    async def register(handler):
        nonlocal runner
        app.router.add_post("/v1/chat/completions", handler)
        runner = aiohttp.web.AppRunner(app)
        await runner.setup()
        site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        return f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1"

    try:
        yield register
    finally:
        if runner is not None:
            try:
                import asyncio
                await asyncio.wait_for(runner.cleanup(), timeout=5)
            except Exception:
                pass


def _echo(seen: list):
    async def handler(request):
        payload = await request.json()
        seen.append(payload)
        return aiohttp.web.json_response(
            {"model": payload.get("model"), "choices": [{"message": {"content": "ok"}}]}
        )
    return handler


def _client(endpoint, **kw):
    # 模型名取生产真实形态（gemini 系）。`reasoning_effort` **只对该系下发** ——
    # 它是由自建 CPA 网关的 gemini 模型实测标定的；对不认识的模型硬发可能 400。
    params = dict(model="gemini-3.6-flash", retries=0, retry_backoff_sec=0)
    params.update(kw)
    return LLMClient(endpoint, "key", **params)


async def test_default_sends_no_reasoning_effort(_endpoint):
    """默认配置下不得凭空多出字段 —— 主链路行为必须逐字节不变。"""
    seen = []
    client = _client(await _endpoint(_echo(seen)))
    await client.chat([{"role": "user", "content": "hi"}])
    assert "reasoning_effort" not in seen[0], f"默认不应下发：{sorted(seen[0])}"


async def test_configured_module_gets_effort(_endpoint):
    """配置了 minimal 的模块，其请求必须带上顶层 reasoning_effort。"""
    seen = []
    client = _client(await _endpoint(_echo(seen)),
                     module_reasoning_effort={"sticker_label": "minimal"})
    with llm_scope("sticker_label", "classify"):
        await client.chat([{"role": "user", "content": "hi"}])
    assert seen[0].get("reasoning_effort") == "minimal"


async def test_unconfigured_module_unchanged(_endpoint):
    """同实例内未配置的模块不受影响。"""
    seen = []
    client = _client(await _endpoint(_echo(seen)),
                     module_reasoning_effort={"sticker_label": "minimal"})
    with llm_scope("chat", "chat"):
        await client.chat([{"role": "user", "content": "hi"}])
    with llm_scope("crossgroup", "extract_facts"):
        await client.chat([{"role": "user", "content": "hi"}])
    assert all("reasoning_effort" not in p for p in seen), [sorted(p) for p in seen]


async def test_structured_path_also_carries_effort(_endpoint):
    """`chat_structured`（后台抽取走的就是它）同样要下发。"""
    seen = []
    client = _client(await _endpoint(_echo(seen)),
                     module_reasoning_effort={"vision": "minimal"})
    tools = [{"type": "function", "function": {
        "name": "t", "description": "d", "parameters": {"type": "object", "properties": {}}}}]
    with llm_scope("vision", "describe"):
        await client.chat_structured([{"role": "user", "content": "hi"}], tools)
    assert seen[0].get("reasoning_effort") == "minimal"


async def test_effort_is_top_level_only(_endpoint):
    """必须只在顶层 —— 嵌套 `reasoning={"effort":...}` 会静默失效（10-01 实测）。"""
    seen = []
    client = _client(await _endpoint(_echo(seen)),
                     module_reasoning_effort={"sticker_label": "minimal"})
    with llm_scope("sticker_label", "classify"):
        await client.chat([{"role": "user", "content": "hi"}])
    assert seen[0].get("reasoning_effort") == "minimal"
    assert "reasoning" not in seen[0], "不得下发嵌套 reasoning 字段"
    assert "thinking" not in seen[0], "不得下发 thinking 字段"


async def test_setter_overrides_at_runtime(_endpoint):
    """运行期可整体替换路由表（便于回滚与灰度）。"""
    seen = []
    client = _client(await _endpoint(_echo(seen)),
                     module_reasoning_effort={"sticker_label": "minimal"})
    client.set_reasoning_efforts({})
    with llm_scope("sticker_label", "classify"):
        await client.chat([{"role": "user", "content": "hi"}])
    assert "reasoning_effort" not in seen[0]


async def test_non_gemini_model_gets_no_effort(_endpoint):
    """非 gemini 模型**不得**收到该参数。

    白名单是**消除本次改动自己引入的风险**：`reasoning_effort` 按 gemini 网关标定，
    若模块路由把 `sticker_label`/`vision` 指到别家模型（如 DeepSeek），硬发可能直接
    400 —— 那是「把一次可选的优化变成一次故障」。宁可漏发（不优化），不可多发（报错）。
    """
    seen = []
    client = _client(await _endpoint(_echo(seen)), model="DeepSeek-V4-Flash-AMD",
                     module_reasoning_effort={"sticker_label": "minimal"})
    with llm_scope("sticker_label", "classify"):
        await client.chat([{"role": "user", "content": "hi"}])
    assert "reasoning_effort" not in seen[0], "非 gemini 模型不应收到该参数"
