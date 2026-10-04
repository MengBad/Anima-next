# -*- coding: utf-8 -*-
"""P0（2026-10-02）：熔断器必须按「出站 / 背景」分桶，单点故障不许全系统陪葬。

生产实证（10-01 22:48:25，PID 123117）：
    `chat` 连续 3 次 empty_completion → 熔断 30s → **25 秒内 11 条调用全部
    circuit_open**：crossgroup×6 / contradiction×2 / danger×2 / sylann / vision /
    sticker_label。用户侧表现为 2 次「返回安全话术」（= 罐头话）。

根因：`consecutive_failures` 与 `circuit_open_until` 挂在 **LLM 单例**上，
出站正文与后台旁路（跨群提取／矛盾检测／打标…）共用同一个熔断器 ⇒
任一方的抖动都会掐断另一方。

本文件钉死分桶契约：
  * 背景熔断 **不阻断** 出站，出站熔断 **不阻断** 背景；
  * 两桶各自独立计到阈值、独立冷却；
  * 成功只重置自己所在桶；
  * `circuit_open_until` / `consecutive_failures` 仍是可读属性，但**跟随当前 scope**；
  * 健康接口额外暴露两桶明细，可观测性不回退。

⚠️ 基础设施坑（不看必踩）：测试**失败**时 pytest 的 traceback 会持有 frame，
使未 `cleanup()` 的 aiohttp `AppRunner` 存活 ⇒ **pytest 收尾阶段永久挂起**
（表现为只打印 `FFFFFF` 后卡死，看不到任何 traceback）。
故本文件统一用 `_endpoint` fixture 管理 server，收尾用 `wait_for` 限时清理。
"""
import asyncio

import aiohttp.web
import pytest

from anima.llm import LLMClient
from anima.llm.usage import llm_scope


@pytest.fixture
async def _endpoint():
    """起一个本地 OpenAI 兼容端点；yield 一个「注册 handler → 返回 endpoint」的闭包。"""
    app = aiohttp.web.Application()
    runner = None

    async def register(handler):
        # 顺序不可换：`AppRunner.setup()` 会冻结 router，之后再 add_route 必抛
        # "Cannot register a resource into frozen router" ⇒ 必须先注册再 setup。
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
                await asyncio.wait_for(runner.cleanup(), timeout=5)
            except Exception:
                pass


def _always_empty(seen: list):
    """任何请求都返回「200-空」⇒ 判定为失败（未配 fallback，故每次只发一个候选）。"""
    async def handler(request):
        payload = await request.json()
        seen.append(payload["model"])
        return aiohttp.web.json_response(
            {"model": payload["model"], "choices": [{"message": {"content": ""}}]}
        )
    return handler


@pytest.fixture
def _client():
    def build(endpoint, **kw):
        params = dict(model="primary", retries=0, retry_backoff_sec=0,
                      circuit_failure_threshold=3, circuit_cooldown_sec=60.0)
        params.update(kw)
        return LLMClient(endpoint, "key", **params)
    return build


@pytest.mark.asyncio
async def test_background_circuit_does_not_block_outbound(_endpoint, _client):
    """背景桶熔断后，出站调用**必须真的发出请求**，不得被早退。"""
    seen: list[str] = []
    client = _client(await _endpoint(_always_empty(seen)))
    with llm_scope("crossgroup", "extract_facts", outbound=False):
        for _ in range(3):
            await client.chat([{"role": "user", "content": "x"}])
    assert client.circuit_open_until > 0, "背景应已熔断"
    n_bg = len(seen)

    with llm_scope("chat", "chat", outbound=True):
        await client.chat([{"role": "user", "content": "x"}])
    assert len(seen) > n_bg, "出站被背景熔断挡住了（未发出请求）"


@pytest.mark.asyncio
async def test_outbound_circuit_does_not_block_background(_endpoint, _client):
    """反向：出站桶熔断后，背景调用仍可发出。"""
    seen: list[str] = []
    client = _client(await _endpoint(_always_empty(seen)))
    with llm_scope("chat", "chat", outbound=True):
        for _ in range(3):
            await client.chat([{"role": "user", "content": "x"}])
        assert client.circuit_open_until > 0, "出站应已熔断"
    n_out = len(seen)

    with llm_scope("crossgroup", "extract_facts", outbound=False):
        await client.chat([{"role": "user", "content": "x"}])
    assert len(seen) > n_out, "背景被出站熔断挡住了（未发出请求）"


@pytest.mark.asyncio
async def test_buckets_count_and_open_independently(_endpoint, _client):
    """两桶各自独立计数到阈值：背景失败 2 次 + 出站失败 2 次 ⇒ 谁都没熔断。"""
    seen: list[str] = []
    client = _client(await _endpoint(_always_empty(seen)))
    with llm_scope("crossgroup", "extract_facts", outbound=False):
        for _ in range(2):
            await client.chat([{"role": "user", "content": "x"}])
    with llm_scope("chat", "chat", outbound=True):
        for _ in range(2):
            await client.chat([{"role": "user", "content": "x"}])

    with llm_scope("crossgroup", "extract_facts", outbound=False):
        assert client.consecutive_failures == 2
        assert client.circuit_open_until == 0.0
    with llm_scope("chat", "chat", outbound=True):
        assert client.consecutive_failures == 2
        assert client.circuit_open_until == 0.0
    assert len(seen) == 4, f"4 次调用都应真的发出，实际 {len(seen)}"


@pytest.mark.asyncio
async def test_success_resets_only_own_bucket(_endpoint, _client):
    """成功只重置自己所在桶，另一桶的失败计数保留。"""
    state = {"fail": True}

    async def handler(request):
        payload = await request.json()
        if state["fail"]:
            return aiohttp.web.json_response(
                {"model": payload["model"], "choices": [{"message": {"content": ""}}]}
            )
        return aiohttp.web.json_response(
            {"model": payload["model"], "choices": [{"message": {"content": "ok"}}]}
        )

    client = _client(await _endpoint(handler))
    with llm_scope("chat", "chat", outbound=True):
        for _ in range(2):
            await client.chat([{"role": "user", "content": "x"}])
    with llm_scope("crossgroup", "extract_facts", outbound=False):
        for _ in range(2):
            await client.chat([{"role": "user", "content": "x"}])

    state["fail"] = False
    with llm_scope("chat", "chat", outbound=True):
        await client.chat([{"role": "user", "content": "x"}])
        assert client.consecutive_failures == 0, "出站桶成功应清零"
    with llm_scope("crossgroup", "extract_facts", outbound=False):
        assert client.consecutive_failures == 2, "背景桶计数必须保留"


@pytest.mark.asyncio
async def test_health_exposes_both_buckets(_endpoint, _client):
    """健康接口暴露两桶明细 ⇒ WebUI 可观测性不回退。"""
    seen: list[str] = []
    client = _client(await _endpoint(_always_empty(seen)))
    with llm_scope("chat", "chat", outbound=True):
        for _ in range(3):
            await client.chat([{"role": "user", "content": "x"}])

    h = client.health()
    assert "circuits" in h, "健康接口缺少 circuits 明细"
    assert {"outbound", "background"} <= set(h["circuits"])
    assert h["circuits"]["outbound"]["open_until"] > 0
    assert not h["circuits"]["background"]["open_until"]
    assert h["circuits"]["outbound"]["failures"] >= 3


@pytest.mark.asyncio
async def test_no_scope_defaults_to_background_bucket(_endpoint, _client):
    """无 scope 时（默认 outbound=False）落在背景桶，且不抛异常。"""
    seen: list[str] = []
    client = _client(await _endpoint(_always_empty(seen)))
    for _ in range(3):
        await client.chat([{"role": "user", "content": "x"}])
    assert client.circuit_open_until > 0

    n = len(seen)
    with llm_scope("chat", "chat", outbound=True):
        await client.chat([{"role": "user", "content": "x"}])
    assert len(seen) > n

# ── 读取侧契约：外部读取方必须看到**全局**，而不是「调用者所在的那个桶」 ─────
#
# 起因（2026-10-02）：分桶把 `circuit_open_until` 变成了「当前 scope 所属桶」的视图。
# 但有三处消费点在 scope **外**读它，语义会静默漂移：
#   * `core/health_history.py:107` 与 `webui/server.py:1062` 读 `health()` 的**顶层**字段
#     （health() 多由无 scope 的 HTTP 协程调用 ⇒ 落到 background 桶）；
#   * `pipeline/stages/generation.py:933` 的熔断守卫（在 react 的 outbound scope **之外**）；
#   * `agent/react.py` 的 `_patience_wait`（同上，服务于出站正文的等待预算）。
# 不修的后果：出站桶熔断时这三处全读成「未熔断」⇒ 监控漏报 + 降级守卫失效 +
# 白等一次 20s 预算。所以规定：
#   * `health()` 顶层字段 = **跨桶汇总**（任一桶开即为开）；
#   * 需要「出站视角」的代码走显式 API，不靠「恰好包在 scope 里」这种隐式契约。


@pytest.mark.asyncio
async def test_health_top_level_reports_any_bucket_open(_endpoint, _client):
    """`health()` 顶层字段必须是**跨桶汇总**：出站桶熔断 ⇒ 顶层即报熔断。"""
    seen: list[str] = []
    client = _client(await _endpoint(_always_empty(seen)))
    with llm_scope("chat", "chat", outbound=True):
        for _ in range(3):
            await client.chat([{"role": "user", "content": "x"}])

    h = client.health()  # 无 scope —— 与 health_history / webui 的真实调用姿势一致
    assert h["circuit_open_until"], "出站桶已熔断，顶层 circuit_open_until 却为空（漏报）"
    assert h["circuit_open_until_any"]
    assert h["circuit_open_until_any"] == h["circuits"]["outbound"]["open_until"]
    # 背景桶并未熔断，明细里必须仍能区分出来
    assert not h["circuits"]["background"]["open_until"]


@pytest.mark.asyncio
async def test_circuit_open_until_of_reads_named_bucket(_endpoint, _client):
    """显式 API：按桶名读取，不受调用者 scope 影响。"""
    seen: list[str] = []
    client = _client(await _endpoint(_always_empty(seen)))
    with llm_scope("chat", "chat", outbound=True):
        for _ in range(3):
            await client.chat([{"role": "user", "content": "x"}])

    assert client.circuit_open_until_of("outbound") > 0
    assert client.circuit_open_until_of("background") == 0.0


def test_outbound_helper_falls_back_for_legacy_client():
    """`outbound_circuit_open_until()` 对没有新 API 的替身要**兜底**，不能抛。"""
    from anima.llm import outbound_circuit_open_until

    class _Legacy:
        circuit_open_until = 123.0

    class _Bare:
        pass

    assert outbound_circuit_open_until(_Legacy()) == 123.0
    assert outbound_circuit_open_until(_Bare()) == 0.0
    assert outbound_circuit_open_until(None) == 0.0


@pytest.mark.asyncio
async def test_patience_wait_reads_outbound_bucket(_endpoint, _client):
    """`_patience_wait` 服务的是出站正文 ⇒ 必须看**出站桶**，不得被背景桶带偏。

    背景桶熔断后，出站其实完全健康 ⇒ 应当照常有耐心预算（返回 budget），
    而不是读到背景桶的剩余冷却（60s > 20s 预算）⇒ 误判「等不到头」直接出罐头话术。
    """
    from anima.agent.react import ReActLoop

    seen: list[str] = []
    client = _client(await _endpoint(_always_empty(seen)))
    with llm_scope("crossgroup", "extract_facts", outbound=False):
        for _ in range(3):
            await client.chat([{"role": "user", "content": "x"}])

    assert client.circuit_open_until_of("background") > 0, "背景桶应已熔断"
    assert client.circuit_open_until_of("outbound") == 0.0, "出站桶此时是健康的"

    loop = ReActLoop(client, None, upstream_patience_sec=20.0)
    assert loop._patience_wait() == 20.0, "出站桶未熔断却被背景桶判成「等不到头」"
