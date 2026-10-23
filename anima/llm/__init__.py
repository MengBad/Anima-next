"""LLM 客户端 —— OpenAI 兼容协议的异步 HTTP 客户端。"""

from __future__ import annotations
import asyncio
import base64
import binascii
import json
import logging
import time
from typing import Optional, Any

import aiohttp
from pydantic import SecretStr
from .usage import UsageEvent, UsageLedger, current_call_info, estimate_tokens

logger = logging.getLogger("anima.llm")

#: 熔断器桶名 —— **出站正文**（要发给用户的回复生成）。
_CIRCUIT_OUTBOUND = "outbound"
#: 熔断器桶名 —— **后台/旁路**（跨群提取、矛盾检测、表情打标、反刍…）。
_CIRCUIT_BACKGROUND = "background"

#: **结构性失效**模型（2026-10-05）—— 不得进入 `fallback_models` 降级链。
#:
#: 生产 72h 账本实测：
#: ```
#: model                总   成功  empty  成功率
#: gemini-3-flash        36     0     36    0%     ← 全部 empty_completion
#: gemini-3.6-flash     203   201      0   99%
#: gemini-3.8-flash     402   402      0  100%
#: ```
#:
#: 🔴 为什么必须剔除而不是"留着碰运气"：它排在链的**最后一位**，所以只在
#: 「主模型 + 健康备用都失败」时才被调用 —— 而那种情况下它**必然**再失败一次
#: （成功率 0%）。实测 14:28 的完整链路：
#: ```
#: 14:28:38  补全为空 model=gemini-3.8-flash-high   主模型失败
#: 14:28:40  补全为空 model=gemini-3.6-flash         第一级备用也失败
#: 14:28:42  补全为空 model=gemini-3-flash           0% 模型凑第三次
#: 14:28:42  ERROR LLM 请求失败：empty_completion   整链仍报同一个错
#: ```
#: ⇒ 它的**唯一**效果是把"一次失败"变成"两次失败"（多 3–6s），
#: 错误信息与失败模型数都不变 ⇒ **纯开销，不含任何容灾价值**。
#:
#: 🔴 本名单**只作用于 `fallback_models`**，绝不改 `self.model`
#: （主模型是配置决策；误伤会让全站不可用）。
#: 空名单 ⇒ 行为逐字不变，是零影响回滚点。
_DEAD_FALLBACK_MODELS: frozenset[str] = frozenset({"gemini-3-flash"})

#: 按模块下发的 ``reasoning_effort`` 默认值（2026-10-02）。
#:
#: 只收**实测有收益**的模块。生产实测（带 tools 的抽取任务，每档 3 次取中位）：
#:   gemini-3.6-flash：不传 → reasoning 505 / 4.1s / completion 532
#:                     minimal → reasoning 无 / **2.0s** / completion **33**
#:   gemini-3-flash：  不传 → reasoning 235 / 2.0s / completion 287
#:                     minimal → reasoning 无 / **1.6s** / completion **33**
#: 而主模型 gemini-3.8-flash-high 在带 tools 场景下 minimal **未见收益**
#: （reasoning 168 vs 173，延迟反而更慢），与 10-01 纯文本实验结论冲突 ⇒
#: **不列入**，主链路行为保持逐字节不变。
_DEFAULT_REASONING_EFFORT: dict[str, str] = {
    "sticker_label": "minimal",
    "vision": "minimal",
}


def outbound_circuit_open_until(llm: Any) -> float:
    """取「出站正文」熔断桶的截止时间戳；对未知替身**容错回退**到旧字段。

    `circuit_open_until` property 跟随调用 scope。守卫／等待预算这类代码位于
    outbound scope **之外**，直接读它会拿到 background 桶（＝漏判），所以统一走本函数。
    测试里的极简桩件没有新 API，此处按旧字段兜底，避免为了加个读取口就得改一堆替身。
    """
    fn = getattr(llm, "circuit_open_until_of", None)
    if callable(fn):
        try:
            return float(fn(_CIRCUIT_OUTBOUND) or 0.0)
        except Exception:  # noqa: BLE001 - 替身实现千奇百怪，读不到即当「未熔断」
            pass
    try:
        return float(getattr(llm, "circuit_open_until", 0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def outbound_consecutive_failures(llm: Any) -> int:
    """取「出站正文」熔断桶的**连续整链失败次数**；未知替身回退 0（fail-open 到「可等待」）。

    与 `outbound_circuit_open_until` 同因：`consecutive_failures` property 跟随调用
    scope，`react._patience_wait` 落在 outbound scope 之外 ⇒ 直接读会拿到 background
    桶的次数。计数粒度是**整链失败事件**（`_post` 候选循环穷尽后 `+= 1`），不是候选数。
    """
    fn = getattr(llm, "consecutive_failures_of", None)
    if callable(fn):
        try:
            return int(fn(_CIRCUIT_OUTBOUND) or 0)
        except Exception:  # noqa: BLE001 - 替身实现千奇百怪，读不到即当 0
            pass
    try:
        return int(getattr(llm, "consecutive_failures", 0) or 0)
    except (TypeError, ValueError):
        return 0


class LLMClient:
    """轻量 OpenAI 兼容 LLM 客户端，支持 Chat Completions API。"""

    def __init__(
        self,
        endpoint: str,
        api_key: str | SecretStr,
        model: str = "gemini-2.5-flash",
        timeout: float = 60.0,
        retries: int = 2,
        retry_backoff_sec: float = 0.6,
        fallback_models: list[str] | None = None,
        circuit_failure_threshold: int = 3,
        circuit_cooldown_sec: float = 30.0,
        provider: str = "default",
        module_models: dict[str, str] | None = None,
        usage_ledger: UsageLedger | None = None,
        image_fallback_models: list[str] | None = None,
        extra_fallback_models: list[str] | None = None,
        module_reasoning_effort: dict[str, str] | None = None,
        outbound_turn_budget_sec: float = 45.0,
        outbound_retries: int = 1,
    ):
        self.base_endpoint = endpoint.rstrip("/")
        self.endpoint = self.base_endpoint + "/chat/completions"
        self.images_endpoint = self.base_endpoint + "/images/generations"
        self.api_key = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        self.model = model
        self.provider = provider or "default"
        self.module_models = dict(module_models or {})
        # None ⇒ 用默认表；传空 dict ⇒ 整体关闭（回滚/灰度用）。
        self.module_reasoning_effort = (
            dict(_DEFAULT_REASONING_EFFORT) if module_reasoning_effort is None
            else {str(k): str(v) for k, v in module_reasoning_effort.items() if str(v).strip()}
        )
        self.usage_ledger = usage_ledger
        # 上游地址不可达时，不能让 QQ 消息在 TCP 建连阶段一直占满总超时。
        # 正常已连接的长回复仍可使用完整的 total 时限。
        self.timeout = aiohttp.ClientTimeout(
            total=timeout,
            sock_connect=min(timeout, 10.0),
        )
        self._session: Optional[aiohttp.ClientSession] = None
        self.retries = max(0, min(int(retries), 4))
        # 🔴 D-39（2026-10-19）：**出站轮次预算**。<=0 ⇒ 关闭（行为与修复前逐字相同）。
        # 语义是"这一轮出站最多花多久"，与 `timeout`（单次尝试）不是一回事：
        # 生产 p90 = 105s 的根因就是"单次 60s × 2 重试 × 6 候选"没人管总账。
        self.outbound_turn_budget_sec = max(0.0, float(outbound_turn_budget_sec or 0.0))
        self.outbound_retries = max(0, min(int(outbound_retries), 3))
        #: `_post` 进入时按 outbound 计算；非出站恒 None（旁路/后台不受限）。
        self._outbound_deadline: Optional[float] = None
        self.retry_backoff_sec = max(0.0, min(float(retry_backoff_sec), 5.0))
        self.fallback_models = [
            str(item).strip() for item in (fallback_models or [])
            if str(item).strip() and str(item).strip() != model
            and str(item).strip() not in _DEAD_FALLBACK_MODELS   # 2026-10-05：剔除 0% 模型
        ][:4]
        #: 🔴 **异族兜底**（2026-10-06，72h 账本驱动）
        #:
        #: 72h 实测：`gemini-3.8-flash-n` 1952/1952 **100%**、`gemini-3.8-flash` 377/377 100%，
        #: 而降级链两级 `gemini-3.6-flash` 96.5%、`gemini-3-flash` **0%**。
        #: ⇒ 主模型从不出问题，**一旦降级两级都在同一个 provider（google）**，
        #: 同 provider 返空是**上游整体抖动**，换同族模型救不回来
        #: （00:25 用户面事故即此形状：`-high` 返空 → `3.6` 返空 → 兜底话术）。
        #:
        #: CPA `/v1/models` 实测有 73 个可用模型（claude / grok / kimi / deepseek / glm…），
        #: 加**一档**异族是治「同族全灭」的唯一结构性修法。
        #:
        #: 契约：① 排**链尾**（同族优先，异族保底）② 去重且剔除主模型
        #: ③ 空/未传 ⇒ 行为**逐字不变**（零影响回滚点）
        #: ④ **同样过 `_DEAD_FALLBACK_MODELS`** ⇒ 否则 0% 模型能从此处绕过 r19 修复
        #: ⑤ 链长上限 4 → 5（同族 4 + 异族 1），不得变成无限链
        _extra = [
            str(item).strip() for item in (extra_fallback_models or [])
            if str(item).strip() and str(item).strip() != model
            and str(item).strip() not in _DEAD_FALLBACK_MODELS
            and str(item).strip() not in self.fallback_models
        ]
        self.fallback_models = (self.fallback_models + _extra)[:5]
        # 图像模型 fallback（跨模型容灾）：主图像模型遇 429/500 等可重试状态时
        # 依次尝试。与文本 fallback_models 独立——文本模型不能用于生图。
        self.image_fallback_models = [
            str(item).strip() for item in (image_fallback_models or [])
            if str(item).strip() and str(item).strip() != self.model
        ][:3]
        self.circuit_failure_threshold = max(1, min(int(circuit_failure_threshold), 20))
        self.circuit_cooldown_sec = max(1.0, min(float(circuit_cooldown_sec), 600.0))
        self.request_count = 0
        self.retry_count = 0
        self.failure_count = 0
        self.last_error = ""
        self.last_error_at = 0.0
        self.last_status: int | None = None
        self.last_success_at = 0.0
        # “安全裁剪后仍成功”不是失败，但必须可观测；否则 WebUI 只能看到
        # 200 而不知道视觉上下文曾被压缩。
        self.degradation_count = 0
        self.last_degradation_at = 0.0
        self.last_degradation_reason = ""
        self.last_payload_bytes = 0
        self.last_effective_payload_bytes = 0
        # 熔断器**分桶**状态（2026-10-02）：见 `_circuit_bucket()`。
        # 必须在下面三个 property 赋值之前建好——它们只是本字典的视图。
        self._circuits: dict[str, dict[str, float | int]] = {
            _CIRCUIT_OUTBOUND: {"failures": 0, "open_until": 0.0, "open_count": 0},
            _CIRCUIT_BACKGROUND: {"failures": 0, "open_until": 0.0, "open_count": 0},
        }
        self.consecutive_failures = 0
        self.circuit_open_until = 0.0
        self.circuit_open_count = 0
        self.failover_count = 0
        self.last_failover_at = 0.0
        self.last_failover_from = ""
        self.last_failover_to = ""
        # 「HTTP 200 但补全为空」= 上游降级。它**不是**成功（旧行为把它当成功 →
        # fallback_models 永无机会、熔断器不计数 → 用户看到硬编码罐头话术）。
        # 计数独立于账本：账本只在最终成功/失败时记一行，若不单独计数，这种降级
        # 一旦被备用模型接住就从遥测里彻底消失。
        self.empty_completion_count = 0
        self.last_empty_completion_at = 0.0
        self.last_empty_completion_model = ""
        # 「出站正文调用」专用镜像计数 —— **归因判据面**（`react._LLM_FAULT_COUNTERS`）。
        # 上面那三个是全量计数（含旁路），这三个只统计 `llm_scope(..., outbound=True)`
        # 的调用。旁路（跨群提取／表情判定／反刍…）与出站共用本实例，若不区分，
        # 一次旁路的 200-空就会被判成「本 turn 上游静默」⇒ 归因桶翻错 + 白等一次预算
        # （09-24 生产实证：17:04 两轮旁路穷尽恰好落在闸门 STOP 之后）。
        self.outbound_empty_completion_count = 0
        self.outbound_failure_count = 0
        self.outbound_circuit_open_count = 0
        # 本次候选模型是否以「200-空」告终（`_post` 据此放行 failover）。
        self._empty_completion = False

    # ---------------------------------------------------------------- 熔断器分桶

    def _circuit_bucket(self) -> str:
        """返回当前调用所属的熔断器桶名。

        2026-10-02 分桶的由来：熔断器原本挂在 **LLM 单例** 上，出站正文与后台旁路
        共用一个 ⇒ 10-01 22:48:25 一次 `chat` 熔断，让 crossgroup×6 / contradiction×2
        / danger×2 / sylann / vision / sticker_label 共 **11 条调用在 25s 内全部
        circuit_open**；用户侧则是 2 次「返回安全话术」。

        改成按「是否出站」分两桶后，任一方的抖动不再掐断另一方：上游整体故障时，
        另一方也会各自累计到自己的阈值再熔断，不会陪葬。
        """
        try:
            return _CIRCUIT_OUTBOUND if current_call_info().outbound else _CIRCUIT_BACKGROUND
        except Exception:  # pragma: no cover - contextvar 取不到时退到背景桶
            return _CIRCUIT_BACKGROUND

    def _circuit_state(self) -> dict[str, float | int]:
        """当前桶的状态字典（不存在则按桶名建一个）。

        惰性建桶是**必需的**：部分测试/装配路径用 ``object.__new__(LLMClient)``
        绕过 ``__init__``（如 ``test_scan_p2h_llm_failover``），此时 ``_circuits``
        尚未建立，直接读会 AttributeError。
        """
        circuits = getattr(self, "_circuits", None)
        if not isinstance(circuits, dict):
            circuits = {}
            self._circuits = circuits
        return circuits.setdefault(
            self._circuit_bucket(), {"failures": 0, "open_until": 0.0, "open_count": 0}
        )

    @property
    def consecutive_failures(self) -> int:
        """**当前桶**的连续失败数（不是全实例计数）。"""
        return int(self._circuit_state().get("failures", 0))

    @consecutive_failures.setter
    def consecutive_failures(self, value: int) -> None:
        self._circuit_state()["failures"] = int(value)

    @property
    def circuit_open_until(self) -> float:
        """**当前桶**的熔断截止时间戳。

        仍是普通可读属性（``react.py`` 据此判断"等不等得起"），但语义变为
        「当前调用上下文所属桶」——出站代码读到出站桶，后台代码读到后台桶。
        """
        return float(self._circuit_state().get("open_until", 0.0))

    @circuit_open_until.setter
    def circuit_open_until(self, value: float) -> None:
        self._circuit_state()["open_until"] = float(value or 0.0)

    @property
    def circuit_open_count(self) -> int:
        """**当前桶**的累计熔断次数。"""
        return int(self._circuit_state().get("open_count", 0))

    @circuit_open_count.setter
    def circuit_open_count(self, value: int) -> None:
        self._circuit_state()["open_count"] = int(value)

    def circuit_open_until_of(self, bucket: str) -> float:
        """按**桶名**读熔断截止时间戳，**不受当前调用 scope 影响**。

        上面三个 property 是「当前 scope 所属桶」的视图 —— 这对 `_post` 内部判定正好，
        但调用点一旦落在 scope **之外**（`pipeline/stages/generation.py` 的守卫、
        `react._patience_wait` 的等待预算、`health()` 的消费方），就会**静默读到
        background 桶**，把「出站已熔断」看成「没熔断」。凡需要明确视角的地方走本方法。
        """
        circuits = getattr(self, "_circuits", None)
        if not isinstance(circuits, dict):
            return 0.0
        state = circuits.get(bucket) or {}
        try:
            return float(state.get("open_until", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def consecutive_failures_of(self, bucket: str) -> int:
        """按**桶名**读连续失败次数，**不受当前调用 scope 影响**（与 `circuit_open_until_of` 同源）。

        计数按「整链失败事件」计（`_post` 候选穷尽后自增），故 `>= 2` 表示上游已
        **连续两次**整链失败 —— 见 `react._patience_wait` 的判据。
        """
        circuits = getattr(self, "_circuits", None)
        if not isinstance(circuits, dict):
            return 0
        state = circuits.get(bucket) or {}
        try:
            return int(state.get("failures", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def set_usage_ledger(self, ledger: UsageLedger | None) -> None:
        self.usage_ledger = ledger

    def set_model_routes(self, routes: dict[str, str] | None) -> None:
        self.module_models = {str(k): str(v) for k, v in (routes or {}).items() if str(v).strip()}

    def model_for(self, module: str | None = None, explicit: str | None = None) -> str:
        if explicit:
            return explicit
        info = current_call_info()
        return self.module_models.get(module or info.module) or self.model

    def reasoning_effort_for(self, module: str | None = None) -> str | None:
        """按模块取 ``reasoning_effort``；未配置的模块返回 None（即不下发）。"""
        info = current_call_info()
        return self.module_reasoning_effort.get(module or info.module) or None

    def set_reasoning_efforts(self, routes: dict[str, str] | None) -> None:
        """整体替换按模块的 ``reasoning_effort`` 路由表（传空 dict 即全关）。"""
        self.module_reasoning_effort = {
            str(k): str(v) for k, v in (routes or {}).items() if str(v).strip()
        }

    def _apply_reasoning_effort(self, payload: dict[str, Any]) -> None:
        """把 ``reasoning_effort`` 写进 payload —— **只写顶层**。

        顶层之外一律无效（2026-10-01 实测）：嵌套 ``reasoning={"effort": ...}``
        与 ``thinking={"type": "disabled"}`` 都会**静默失效**（不报错、不生效）。

        只对 ``gemini`` 系模型下发：该参数按自建 CPA 网关的 gemini 模型实测标定，
        对不认识的模型硬发可能直接 400。判据取 **payload 里即将发出的那个 model**
        （已过 `model_for` 模块路由），不是构造参数。
        """
        effort = self.reasoning_effort_for()
        if not effort:
            return
        if "gemini" not in str(payload.get("model") or "").lower():
            return
        payload["reasoning_effort"] = effort

    async def start(self) -> None:
        if self._session is None:
            # force_close=True：每次请求后关闭 TCP 连接，避免反代/网关静默回收
            # 空闲 keep-alive 连接后 aiohttp 复用时触发 ServerDisconnectedError。
            # LLM 单次调用 1~3s，连接复用省下的 ~50ms 完全不值一次失败重试。
            connector = aiohttp.TCPConnector(
                limit=16,
                force_close=True,
                enable_cleanup_closed=True,
                ttl_dns_cache=300,
            )
            self._session = aiohttp.ClientSession(
                timeout=self.timeout, connector=connector
            )

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def chat(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.8,
        max_tokens: int = 1024,
        model: str | None = None,
        module: str | None = None,
        operation: str = "chat",
    ) -> str:
        """发送对话请求，返回模型回复文本。"""
        if module:
            from anima.llm.usage import llm_scope
            with llm_scope(module, operation):
                return await self._request(messages, None, temperature, max_tokens, model)
        return await self._request(messages, None, temperature, max_tokens, model)

    async def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict],
        temperature: float = 0.8,
        max_tokens: int = 1024,
        model: str | None = None,
    ) -> str:
        """发送对话请求（带工具 schema），返回模型回复文本。"""
        return await self._request(messages, tools, temperature, max_tokens, model)

    async def chat_structured(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict],
        temperature: float = 0.8,
        max_tokens: int = 1024,
        model: str | None = None,
    ) -> dict:
        """发送对话请求，返回结构化响应 {content, tool_calls}。

        用于 ReAct 循环。
        """
        return await self._request_structured(messages, tools, temperature, max_tokens, model)

    async def generate_image(self, prompt: str, *, model: str, max_bytes: int) -> bytes | None:
        """经 /v1/chat/completions 调用 Gemini 图像模型（gemini-3.1-flash-image）生成图片。

        CLI Proxy 网关的 /images/generations 只认 OpenAI 兼容图像模型（gpt-image/grok-imagine），
        不认 gemini 图像模型；实测 gemini-3.1-flash-image 需走 chat 端点，返回
        choices[].message.images[].image_url.url（base64）。任何非图片响应都视为失败。

        主模型失败（429/500 等可重试状态）时依次尝试 image_fallback_models：
        gemini 类走 chat 端点，imagine 类走 /images/generations 端点。
        """
        return await self._request_image_with_fallback(
            prompt, model=model, max_bytes=max_bytes, input_image_b64=None,
        )

    async def edit_image(self, prompt: str, *, model: str, input_image_b64: str, max_bytes: int) -> bytes | None:
        """经 /v1/chat/completions 用 Gemini 图像模型修改已有图片（多模态输入图 + 指令）。"""
        return await self._request_image_with_fallback(
            prompt, model=model, max_bytes=max_bytes, input_image_b64=input_image_b64,
        )

    async def _request_image_with_fallback(
        self, prompt: str, *, model: str, max_bytes: int, input_image_b64: str | None,
    ) -> bytes | None:
        """生图跨模型容灾：主模型失败且状态可重试时，依次尝试图像 fallback 模型。"""
        requested = self.model_for(current_call_info().module, model)
        candidates = [requested, *[m for m in self.image_fallback_models if m != requested]]
        for pos, cand in enumerate(candidates):
            image = await self._request_image_one(
                prompt, model=cand, max_bytes=max_bytes, input_image_b64=input_image_b64,
            )
            if image is not None:
                if pos:
                    logger.warning("生图模型 %s 失败，已 failover 到 %s", candidates[0], cand)
                    self._record_failover(candidates[0], cand)
                return image
            # 非可重试状态（如 401/400 参数错）切模型没有意义，直接终止。
            if self.last_status is not None and not self._is_failoverable_status(self.last_status):
                break
        return None

    async def _request_image_one(
        self, prompt: str, *, model: str, max_bytes: int, input_image_b64: str | None,
    ) -> bytes | None:
        """按模型类型路由：gemini 类走 chat 端点，imagine/grok 类走 /images/generations。"""
        if "imagine" in model.lower():
            return await self._request_image_via_generations(
                prompt, model=model, max_bytes=max_bytes, input_image_b64=input_image_b64,
            )
        return await self._request_image_via_chat(
            prompt, model=model, max_bytes=max_bytes, input_image_b64=input_image_b64,
        )

    async def _request_image_via_generations(
        self, prompt: str, *, model: str, max_bytes: int, input_image_b64: str | None,
    ) -> bytes | None:
        """经 /images/generations 调用 OpenAI 兼容图像模型（grok-imagine-*），返回 data[].b64_json。

        gemini 图像模型走 chat 端点；grok-imagine 走 images/generations（503 拒绝走 chat）。
        """
        if self._session is None:
            await self.start()
        payload: dict[str, Any] = {"model": model, "prompt": prompt[:4000], "n": 1, "size": "1024x1024"}
        if input_image_b64:
            payload["image"] = input_image_b64  # edits：OpenAI 兼容传 base64 图
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        self.request_count += 1
        started = time.perf_counter()
        retry_start = self.retry_count
        img_timeout = aiohttp.ClientTimeout(total=240, sock_connect=min(60.0, 10.0))
        try:
            async with self._session.post(self.images_endpoint, json=payload, headers=headers, timeout=img_timeout) as response:  # type: ignore[union-attr]
                self.last_status = response.status
                if response.status != 200:
                    body = await response.text()
                    self._set_error(f"image_http_{response.status}:{body[:160]}")
                    self.failure_count += 1
                    await self._record_usage(payload, None, started, retry_start, "error")
                    return None
                data = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            self.last_status = None
            self._set_error(f"image_transport:{type(exc).__name__}")
            self.failure_count += 1
            await self._record_usage(payload, None, started, retry_start, "error")
            return None
        except Exception:
            self.last_status = None
            self._set_error("image_unexpected")
            self.failure_count += 1
            logger.exception("生图请求异常")
            await self._record_usage(payload, None, started, retry_start, "error")
            return None
        try:
            items = (data.get("data") or [])
            b64 = (items[0].get("b64_json") or "") if items else ""
            if not b64:
                raise ValueError("missing_b64_json")
            image = base64.b64decode(b64, validate=True)
            if not image or (max_bytes > 0 and len(image) > max_bytes):
                raise ValueError("invalid_image_size")
        except (ValueError, TypeError, KeyError, IndexError, binascii.Error) as exc:
            self._set_error(f"image_response:{type(exc).__name__}")
            self.failure_count += 1
            await self._record_usage(payload, data, started, retry_start, "error")
            return None
        self.last_error = ""
        self.last_success_at = time.time()
        await self._record_usage(payload, data, started, retry_start, "ok")
        return image

    async def _request_image_via_chat(
        self, prompt: str, *, model: str, max_bytes: int, input_image_b64: str | None,
    ) -> bytes | None:
        """共用实现：Gemini 图像生成/修改都走 chat/completions，解析 message.images[].url(base64)。"""
        if self._session is None:
            await self.start()
        requested_model = self.model_for(current_call_info().module, model)
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt[:4000]}]
        if input_image_b64:
            # 多模态输入图：可带 data URI 前缀（网关透传），缺失时补 image/png
            uri = input_image_b64
            if not uri.startswith("data:"):
                uri = f"data:image/png;base64,{input_image_b64}"
            content.append({"type": "image_url", "image_url": {"url": uri}})
        payload = {"model": requested_model, "messages": [{"role": "user", "content": content}], "max_tokens": 4096}
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        self.request_count += 1
        started = time.perf_counter()
        retry_start = self.retry_count
        # 图像生成/修改耗时明显长于普通文本（带输入图更甚），给图像请求独立更长的超时，
        # 避免被全局 LLM timeout（60s）误杀。
        img_timeout = aiohttp.ClientTimeout(total=240, sock_connect=min(60.0, 10.0))
        try:
            async with self._session.post(self.endpoint, json=payload, headers=headers, timeout=img_timeout) as response:  # type: ignore[union-attr]
                self.last_status = response.status
                if response.status != 200:
                    body = await response.text()
                    self._set_error(f"image_http_{response.status}:{body[:160]}")
                    self.failure_count += 1
                    await self._record_usage(payload, None, started, retry_start, "error")
                    return None
                data = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            self.last_status = None
            self._set_error(f"image_transport:{type(exc).__name__}")
            self.failure_count += 1
            await self._record_usage(payload, None, started, retry_start, "error")
            return None
        except Exception:
            self.last_status = None
            self._set_error("image_unexpected")
            self.failure_count += 1
            logger.exception("生图请求异常")
            return None
        try:
            choices = (data.get("choices") or [])
            message = (choices[0].get("message") or {}) if choices else {}
            images = (message.get("images") or [])
            url = (images[0].get("image_url") or {}).get("url") if images else None
            if not isinstance(url, str) or "," not in url:
                raise ValueError("missing_image_url")
            encoded = url.split(",", 1)[1]
            image = base64.b64decode(encoded, validate=True)
            if not image or (max_bytes > 0 and len(image) > max_bytes):
                raise ValueError("invalid_image_size")
        except (ValueError, TypeError, KeyError, IndexError, binascii.Error) as exc:
            self._set_error(f"image_response:{type(exc).__name__}")
            self.failure_count += 1
            await self._record_usage(payload, data, started, retry_start, "error")
            return None
        self.last_error = ""
        self.last_success_at = time.time()
        await self._record_usage(payload, data, started, retry_start, "ok")
        return image

    async def transcribe_audio(self, audio: bytes, fmt: str, *, model: str | None = None) -> str:
        """以 OpenAI 兼容的 input_audio 内容块请求转写；失败返回空串。"""
        if not audio or len(audio) > 10 * 1024 * 1024:
            return ""
        encoded = base64.b64encode(audio).decode("ascii")
        return await self.chat([
            {"role": "system", "content": "你是语音转写器。只输出听到的中文文本；听不清则输出[听不清]。"},
            {"role": "user", "content": [
                {"type": "text", "text": "请转写这段语音。"},
                {"type": "input_audio", "input_audio": {"data": encoded, "format": fmt}},
            ]},
        ], temperature=0.0, max_tokens=512, model=model,
           module="audio", operation="transcription")

    async def _request(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict]] = None,
        temperature: float = 0.8,
        max_tokens: int = 1024,
        model: str | None = None,
    ) -> str:
        """统一请求方法。"""

        if self._session is None:
            await self.start()

        payload: dict[str, Any] = {
            "model": self.model_for(current_call_info().module, model),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        self._apply_reasoning_effort(payload)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        data = await self._post(payload, headers)
        if not data:
            return ""
        choices = data.get("choices", [])
        if not choices:
            self._set_error("empty_choices")
            return ""
        content = choices[0].get("message", {}).get("content", "")
        return content.strip()

    async def _request_structured(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict],
        temperature: float = 0.8,
        max_tokens: int = 1024,
        model: str | None = None,
    ) -> dict:
        """结构化请求，返回 {content: str, tool_calls: list}。"""
        if self._session is None:
            await self.start()

        payload: dict[str, Any] = {
            "model": self.model_for(current_call_info().module, model),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        self._apply_reasoning_effort(payload)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        data = await self._post(payload, headers)
        if not data:
            return {"content": "", "tool_calls": []}
        choices = data.get("choices", [])
        if not choices:
            self._set_error("empty_choices")
            return {"content": "", "tool_calls": []}
        msg = choices[0].get("message", {})
        return {
            "content": (msg.get("content") or "").strip(),
            "tool_calls": msg.get("tool_calls", []),
        }

    # 网关硬限制 10MB；预留 2MB 余量，超过即触发降级裁剪重试。
    _SAFE_MAX_BYTES = 8 * 1024 * 1024
    _TRIM_KEEP_MESSAGES = 12

    @staticmethod
    def _estimate_bytes(payload: dict[str, Any]) -> int:
        try:
            return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        except Exception:
            return 0

    @staticmethod
    def _estimate_has_images(messages: list[dict[str, Any]]) -> bool:
        """请求体里是否含图片块（用于「目标模型不支持视觉」时判断是否需要剥离）。"""
        try:
            for msg in messages or []:
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        return True
        except Exception:
            return False
        return False

    def _trim_messages(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
        """请求体过大时的降级裁剪：先剥离图片块（base64 是体积主因），
        再截断历史（保留 system + 末尾 N 条）。返回 (裁剪后消息, 是否发生裁剪)。"""
        dropped = False
        out: list[dict[str, Any]] = []
        for m in messages:
            content = m.get("content")
            if isinstance(content, list):
                kept = [
                    p for p in content
                    if not (isinstance(p, dict) and p.get("type") == "image_url")
                ]
                if not kept:
                    kept = [{"type": "text", "text": ""}]
                nm = dict(m)
                nm["content"] = kept
                out.append(nm)
                if len(kept) != len(content):
                    dropped = True
            else:
                out.append(m)
        if len(out) > self._TRIM_KEEP_MESSAGES:
            sys_msgs = [m for m in out if m.get("role") == "system"]
            rest = [m for m in out if m.get("role") != "system"]
            keep = max(0, self._TRIM_KEEP_MESSAGES - len(sys_msgs))
            rest = rest[-keep:] if keep else []
            out = sys_msgs + rest
            dropped = True
        return out, dropped

    def _shrink_message_images(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
        """优先把图片再压小，尽量保留视觉理解；失败时返回原消息。"""
        try:
            from anima.media import shrink_message_images
            return shrink_message_images(
                messages,
                max_bytes_per_image=512 * 1024,
                max_dimension=1024,
            )
        except Exception:
            logger.debug("LLM 图片再压缩失败，将走剥离降级", exc_info=True)
            return messages, False

    #: 🔴 2026-10-24：已知**不支持图片输入**的模型名特征（小写匹配）。
    #:   生产实证：主模型 `gemini-3-flash` 正常，降级到异族兜底
    #:   `DeepSeek-V4-Flash` 时上游返回
    #:   `400 Model ... does not support image input`，且**不可重试**
    #:   ⇒ 用户面直接拿到兜底话术（带图消息整轮丢失）。
    #:   名单式判定是保守的：只把**确证不支持**的挡掉，未知模型按支持处理。
    _NON_VISION_MODEL_HINTS = ("deepseek", "kimi", "qwen", "glm")

    def _supports_vision(self, model: str) -> bool:
        """该模型是否支持图片输入（保守判定：只挡确证不支持的）。"""
        m = str(model or "").lower()
        if not m:
            return True
        return not any(h in m for h in self._NON_VISION_MODEL_HINTS)

    def _strip_message_images(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
        """把多模态消息里的图片**完全剥离**，退化为纯文本。

        与 `_shrink_message_images`（压缩但保留视觉）不同：这里是
        「目标模型根本不支持视觉」时的**唯一可行降级**。
        返回 (新消息, 是否真的剥离了)。
        """
        changed = False
        out: list[dict[str, Any]] = []
        for msg in messages or []:
            if not isinstance(msg, dict):
                out.append(msg)
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                out.append(msg)
                continue
            text_parts: list[str] = []
            has_image = False
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    has_image = True
                    continue
                if isinstance(part, dict) and part.get("type") == "text":
                    text_parts.append(str(part.get("text") or ""))
                elif isinstance(part, str):
                    text_parts.append(part)
            if has_image:
                changed = True
                new_msg = dict(msg)
                new_msg["content"] = "\n".join(t for t in text_parts if t) or "[图片]"
                out.append(new_msg)
            else:
                out.append(msg)
        return out, changed

    def _record_degradation(self, reason: str, before_bytes: int, after_bytes: int) -> None:
        self.degradation_count += 1
        self.last_degradation_at = time.time()
        self.last_degradation_reason = reason
        self.last_payload_bytes = max(0, int(before_bytes))
        self.last_effective_payload_bytes = max(0, int(after_bytes))

    def _record_failover(self, previous: str, current: str) -> None:
        self.failover_count += 1
        self.last_failover_at = time.time()
        self.last_failover_from = previous
        self.last_failover_to = current
        logger.warning("LLM 主模型 %s 不可用，已切换本次请求到备用模型 %s", previous, current)

    @staticmethod
    def _is_failoverable_status(status: int | None) -> bool:
        return status is None or status in {408, 409, 425, 429, 500, 502, 503, 504}

    @staticmethod
    def _is_empty_completion(data: Any) -> bool:
        """判定「HTTP 200 但补全为空」——上游降级，必须可 failover。

        判定为空的形态（全部实测自生产 09-17 的 25 次零输出）：
          * 无 ``choices`` 字段，或 ``choices`` 为空列表；
          * 首个 choice 的 ``message`` 里 ``content`` 缺失/``None``/纯空白，
            **且**没有任何 ``tool_calls`` / ``function_call``；
          * 只有 ``reasoning_content`` 而 ``content`` 为空 —— 调用方（``_request`` /
            ``_request_structured``）只读 ``content``，对用户而言照样是空回复。

        刻意**不**判为空（fail-open，保持原行为）：
          * ``data`` 非 dict、``choices[0]`` 非 dict、``message`` 非 dict —— 协议变体
            无从判定，宁可当成功也不误伤成失败；
          * 带 ``tool_calls`` 的空 ``content`` —— 这是**合法**的（模型在本轮只发工具调用）。

        注意：本方法**只**看响应体形状，不改 ``last_status`` —— 遥测里的 HTTP 码
        必须保持真实（200 就是 200），降级由 ``empty_completion_count`` 单独承载。
        """
        if not isinstance(data, dict):
            return False
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return True
        first = choices[0]
        if not isinstance(first, dict):
            return False
        message = first.get("message")
        if not isinstance(message, dict):
            return False
        if message.get("tool_calls") or message.get("function_call"):
            return False
        content = message.get("content")
        if isinstance(content, list):  # 多模态/分段 content
            text = "".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict)
            )
        elif content is None:
            text = ""
        else:
            text = str(content)
        return not text.strip()

    async def _post(self, payload: dict[str, Any], headers: dict[str, str]) -> dict | None:
        """发送 LLM 请求。请求体超限(413)或预检超安全上限时，先剥离图片/截断历史
        降级重试一次，避免整条回复被静默丢弃。"""
        self.request_count += 1
        started = time.perf_counter()
        retry_start = self.retry_count
        # 🔴 D-39（2026-10-19）：**出站轮次预算**。只在 outbound scope 内生效；
        # 0 = 关闭（行为与修复前逐字相同）。不 try/finally 清：下次 _post 重新赋值，
        # 非出站一律 None（旁路/后台不该被 QQ 的体验预算牵连）。
        self._outbound_deadline = (
            started + self.outbound_turn_budget_sec
            if current_call_info().outbound and self.outbound_turn_budget_sec > 0
            else None
        )
        now = time.time()
        if self.circuit_open_until > now:
            self.last_status = None
            self._set_error("circuit_open")
            await self._record_usage(payload, None, started, retry_start, "error")
            return None
        est = self._estimate_bytes(payload)
        self.last_payload_bytes = est
        if est > self._SAFE_MAX_BYTES:
            # 先再压图片尽量保留视觉；仍超限再剥离图片/截断历史。
            shrunk, shrink_changed = self._shrink_message_images(payload.get("messages", []))
            if shrink_changed:
                payload["messages"] = shrunk
                after_shrink = self._estimate_bytes(payload)
                self._record_degradation("payload_preflight_reencode", est, after_shrink)
                logger.warning(
                    "LLM 请求体 %d 字节超安全上限，已再压缩图片为 %d 字节",
                    est, after_shrink,
                )
                est = after_shrink
            if est > self._SAFE_MAX_BYTES:
                trimmed, changed = self._trim_messages(payload.get("messages", []))
                payload["messages"] = trimmed
                after = self._estimate_bytes(payload)
                if changed:
                    self._record_degradation("payload_preflight_trim", est, after)
                    logger.warning("LLM 请求体 %d 字节仍超安全上限，已降级裁剪为 %d 字节后发送", est, after)
                else:
                    logger.warning("LLM 请求体 %d 字节超安全上限，但无可裁剪图片/历史", est)
                self.last_effective_payload_bytes = after
            else:
                self.last_effective_payload_bytes = est
        else:
            self.last_effective_payload_bytes = est
        requested_model = str(payload.get("model") or self.model)
        candidates = [requested_model, *[item for item in self.fallback_models if item != requested_model]]
        data: dict | None = None
        for position, candidate in enumerate(candidates):
            if (self._outbound_deadline is not None
                    and time.perf_counter() >= self._outbound_deadline):
                self._set_error("turn_budget_exhausted")
                logger.warning(
                    "出站轮次预算 %.0fs 耗尽，放弃剩余 %d 个候选（已试 %d 个：%s）",
                    self.outbound_turn_budget_sec, len(candidates) - position,
                    position, ",".join(candidates[:position]) or "-",
                )
                break
            payload["model"] = candidate
            # 🔴 2026-10-24：切到**不支持图片输入**的候选模型时，先剥离图片。
            #   生产实证：带图请求从 gemini 降级到 DeepSeek-V4-Flash 直接
            #   `400 does not support image input`，且不可重试 ⇒ 整轮丢失。
            #   剥离后请求合法，视觉理解降级为「用户发来一张图」的文本说明，
            #   **总比整轮失败好**（fail-soft，不是 fail-open）。
            if (position > 0 and not self._supports_vision(candidate)
                    and self._estimate_has_images(payload.get("messages", []))):
                before = self._estimate_bytes(payload)
                stripped, strip_changed = self._strip_message_images(payload.get("messages", []))
                if strip_changed:
                    payload["messages"] = stripped
                    after = self._estimate_bytes(payload)
                    self._record_degradation("non_vision_model_strip_images", before, after)
                    logger.warning(
                        "候选模型 %s 不支持图片输入，已剥离 %d 字节图片降级为纯文本重试",
                        candidate, before - after if before > after else 0)
            self._empty_completion = False  # 逐候选重置，避免上一位的降级污染本位的判定
            data = await self._try_post(payload, headers)
            if data is None and self.last_status == 413:
                # 413：先再压图片保留视觉，再不行才剥离图片/截断历史。
                before = self._estimate_bytes(payload)
                shrunk, shrink_changed = self._shrink_message_images(payload.get("messages", []))
                if shrink_changed:
                    payload["messages"] = shrunk
                    after = self._estimate_bytes(payload)
                    self._record_degradation("http_413_reencode_retry", before, after)
                    self.last_effective_payload_bytes = after
                    logger.warning("LLM 413 请求体过大，已再压缩图片为 %d 字节后重试", after)
                    data = await self._try_post(payload, headers)
                    before = after
                if data is None and self.last_status == 413:
                    trimmed, changed = self._trim_messages(payload.get("messages", []))
                    payload["messages"] = trimmed
                    after = self._estimate_bytes(payload)
                    if changed:
                        self._record_degradation("http_413_trim_retry", before, after)
                        self.last_effective_payload_bytes = after
                        logger.warning("LLM 413 请求体过大，已裁剪为 %d 字节后重试一次", after)
                        data = await self._try_post(payload, headers)
                    else:
                        logger.error("LLM 413 请求体过大且无可裁剪内容，不进行重复请求")
            if data is not None:
                if position:
                    self._record_failover(candidates[position - 1], candidate)
                self.consecutive_failures = 0
                self.circuit_open_until = 0.0
                await self._record_usage(payload, data, started, retry_start, "ok")
                return data
            # 200-空：`last_status` 保持真实的 200，但补全为空属上游降级 → 必须换候选
            # 模型。`_is_failoverable_status()` 只看 HTTP 码（200 不在可重试集合里），
            # 在这里显式放行，否则 `fallback_models` 永远等不到机会。
            if self._empty_completion:
                continue
            # 鉴权、参数、载荷等非瞬态问题切模型没有意义，也不能无限尝试。
            if not self._is_failoverable_status(self.last_status):
                break

        self.failure_count += 1
        if current_call_info().outbound:
            self.outbound_failure_count += 1
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.circuit_failure_threshold:
            self.circuit_open_until = time.time() + self.circuit_cooldown_sec
            self.circuit_open_count += 1
            if current_call_info().outbound:
                self.outbound_circuit_open_count += 1
            logger.error("LLM 连续失败 %d 次，熔断 %.0fs", self.consecutive_failures, self.circuit_cooldown_sec)
        else:
            logger.error("LLM 请求失败：%s", self.last_error)
        await self._record_usage(payload, None, started, retry_start, "error")
        return None

    @staticmethod
    def _reasoning_from_response(data: dict[str, Any] | None) -> int:
        """取上游的「思考」token 数（2026-10-04）。

        🔴 **刻意独立于 `_usage_from_response`**：后者的 5-tuple 返回值被既有测试**严格等值**
        断言（`tests/test_llm_usage.py`），改元数会连带打破它 —— 与其改签名，不如加一个
        纯读取口（同一个「不动既有调用签名」原则的又一例）。

        两种上游形态：
        - OpenAI 兼容（自建 CPA 实测）：`usage.completion_tokens_details.reasoning_tokens`
        - Gemini 原生：`usageMetadata.thoughtsTokenCount`

        缺字段一律 0（「没有」≠「有」）。
        实测波动极大（49–2129），是归因「这次为什么慢」的关键分母。
        """
        if not isinstance(data, dict):
            return 0
        usage = data.get("usage") or data.get("usage_metadata") or data.get("usageMetadata") or {}
        if not isinstance(usage, dict):
            return 0
        details = usage.get("completion_tokens_details")
        if isinstance(details, dict):
            for key in ("reasoning_tokens", "reasoningTokens"):
                value = details.get(key)
                if isinstance(value, (int, float)):
                    return max(0, int(value))
        for key in ("reasoning_tokens", "thoughtsTokenCount"):
            value = usage.get(key)
            if isinstance(value, (int, float)):
                return max(0, int(value))
        return 0

    @staticmethod
    def _usage_from_response(data: dict[str, Any] | None) -> tuple[int, int, int, int, bool]:
        if not isinstance(data, dict):
            return 0, 0, 0, 0, True
        usage = data.get("usage") or data.get("usage_metadata") or data.get("usageMetadata") or {}
        if not isinstance(usage, dict):
            usage = {}
        def integer(*keys: str) -> int:
            for key in keys:
                value = usage.get(key)
                if isinstance(value, (int, float)):
                    return max(0, int(value))
            return 0
        inp = integer("prompt_tokens", "input_tokens", "promptTokenCount")
        out = integer("completion_tokens", "output_tokens", "candidatesTokenCount")
        cached = integer("cached_tokens", "cache_read_input_tokens", "cachedContentTokenCount")
        total = integer("total_tokens", "totalTokenCount") or inp + out
        return inp, out, cached, total, not bool(usage)

    async def _record_usage(self, payload: dict[str, Any], data: dict[str, Any] | None,
                            started: float, retry_start: int, status: str) -> None:
        if self.usage_ledger is None:
            return
        info = current_call_info()
        inp, out, cached, total, estimated = self._usage_from_response(data)
        if estimated:
            inp = estimate_tokens(payload.get("messages", payload.get("prompt", "")))
            out = estimate_tokens(((data or {}).get("choices") or [{}])[0].get("message", {}).get("content", "")) if data else 0
            total = inp + out
        actual = str((data or {}).get("model") or payload.get("model") or self.model)
        # 🔴 2026-10-16（A2）：读本回合的上下文预算 / 召回命中采样（pipeline 侧写入）。
        # 无采样时为空 dict ⇒ 两项按 0 落库（「没有」≠「有」）。
        try:
            from anima.llm.usage import current_ctx_sample

            _sample = current_ctx_sample() or {}
        except Exception:  # noqa: BLE001 - 观测绝不阻断
            _sample = {}
        event = UsageEvent(
            created_at=time.time(), trace_id=info.trace_id or "unscoped", module=info.module,
            operation=info.operation, provider=self.provider,
            requested_model=str(payload.get("model") or self.model), actual_model=actual,
            input_tokens=inp, output_tokens=out, cached_tokens=cached, total_tokens=total,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            retries=max(0, self.retry_count - retry_start), status=status,
            estimated=estimated, error=self.last_error if status != "ok" else "",
            reasoning_tokens=self._reasoning_from_response(data),
            ctx_tokens=int(_sample.get("ctx_tokens") or 0),
            recall_hits=int(_sample.get("recall_hits") or 0),
        )
        try:
            await self.usage_ledger.arecord(event)
        except Exception:
            logger.exception("LLM 用量账本写入失败")

    async def _try_post(self, payload: dict[str, Any], headers: dict[str, str]) -> dict | None:
        """仅对可恢复故障进行有限重试，避免鉴权错误造成无意义等待。"""
        _outbound = bool(current_call_info().outbound)
        _retries = self.outbound_retries if _outbound else self.retries
        for attempt in range(_retries + 1):
            _kwargs: dict[str, Any] = {}
            if _outbound and self._outbound_deadline is not None:
                _remain = self._outbound_deadline - time.perf_counter()
                if _remain <= 1.0:
                    # 预算不够一次像样的尝试：立刻交回，让 `_post` 停候选循环、
                    # 上层据此落兜底 —— 而不是眼睁睁再等 60s。
                    self._set_error("turn_budget_exhausted")
                    logger.warning("出站轮次预算耗尽（剩余 %.1fs），停止尝试 model=%s",
                                   max(0.0, _remain), payload.get("model"))
                    return None
                _kwargs["timeout"] = aiohttp.ClientTimeout(
                    total=_remain, sock_connect=min(10.0, _remain))
            try:
                async with self._session.post(  # type: ignore[union-attr]
                    self.endpoint, json=payload, headers=headers, **_kwargs,
                ) as response:
                    self.last_status = response.status
                    if response.status == 200:
                        try:
                            data = await response.json()
                        except (aiohttp.ContentTypeError, ValueError) as exc:
                            self._set_error(f"invalid_json:{type(exc).__name__}")
                            return None
                        if self._is_empty_completion(data):
                            # 200-空 = 上游降级，**不是**成功。刻意不在此消耗原地重试：
                            # 同一模型 + 同一 prompt 复现同一退化的概率高，代价却是整轮
                            # 延迟（retries × (调用 + 退避)）；直接判本次候选失败，交给
                            # `_post` 换下一个候选模型——成本上界 = +1 次调用/候选。
                            self._empty_completion = True
                            self.empty_completion_count += 1
                            if current_call_info().outbound:
                                self.outbound_empty_completion_count += 1
                            self.last_empty_completion_at = time.time()
                            self.last_empty_completion_model = str(payload.get("model") or "")
                            self._set_error("empty_completion")
                            logger.warning(
                                "LLM 返回 HTTP 200 但补全为空（无 content 且无 tool_calls）："
                                "model=%s，判定为上游降级，本次候选不作为成功，改试下一个候选模型",
                                payload.get("model"),
                            )
                            return None
                        self.last_error = ""
                        self.last_success_at = time.time()
                        return data
                    body = await response.text()
                    self._set_error(f"http_{response.status}:{body[:160]}")
                    retryable = response.status in {408, 409, 425, 429, 500, 502, 503, 504}
                    if not retryable:
                        logger.error("LLM API 不可重试错误 %d: %.300s", response.status, body)
                        return None
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                self.last_status = None
                self._set_error(f"transport:{type(exc).__name__}")
            except Exception as exc:
                self.last_status = None
                self._set_error(f"unexpected:{type(exc).__name__}")
                logger.exception("LLM 请求异常")
                return None

            if attempt < _retries:
                if (self._outbound_deadline is not None
                        and self._outbound_deadline - time.perf_counter()
                        <= self.retry_backoff_sec * (2 ** attempt) + 2.0):
                    self._set_error("turn_budget_exhausted")
                    logger.warning("出站轮次预算不足以再重试一次 model=%s",
                                   payload.get("model"))
                    return None
                self.retry_count += 1
                delay = self.retry_backoff_sec * (2 ** attempt)
                logger.warning("LLM 请求失败，%.1fs 后第 %d/%d 次重试：%s",
                               delay, attempt + 1, _retries, self.last_error)
                await asyncio.sleep(delay)

        return None

    def _set_error(self, detail: str) -> None:
        # 脱敏：移除可能泄漏到日志/WebUI 的 API key 片段
        import re as _re
        detail = _re.sub(r'(Bearer\s+|sk-|key[=:]\s*)[A-Za-z0-9_\-]{8,}', '[REDACTED]', detail, flags=_re.I)
        self.last_error = detail[:240]
        self.last_error_at = time.time()

    def health(self) -> dict[str, Any]:
        now = time.time()
        # 顶层三项是**跨桶汇总**（2026-10-02）。原因：health() 的消费方
        # `core/health_history.py:107`、`webui/server.py:1062` 都在 scope **外**调用，
        # 若顶层沿用「当前 scope 所属桶」的 property 视图，就会在无 scope 时固定读到
        # background 桶 ⇒ **出站熔断被静默漏报**（监控/告警看不到用户可感的那一半）。
        # 分桶明细另见 `circuits`；需要「只看出站」的代码走 `circuit_open_until_of()`。
        raw = getattr(self, "_circuits", None)
        if not isinstance(raw, dict):
            raw = {}
        circuits = {
            name: {
                "failures": int(state.get("failures", 0)),
                "open_until": float(state.get("open_until", 0.0)) or None,
                "open": float(state.get("open_until", 0.0)) > now,
                "open_count": int(state.get("open_count", 0)),
            }
            for name, state in raw.items()
        }
        any_open_until = max(
            (float(s.get("open_until", 0.0)) for s in raw.values()), default=0.0
        )
        any_failures = max((int(s.get("failures", 0)) for s in raw.values()), default=0)
        total_open_count = sum(int(s.get("open_count", 0)) for s in raw.values())
        circuit_open = any_open_until > now
        if circuit_open:
            current_state = "circuit_open"
        elif self.last_error and any_failures > 0:
            current_state = "degraded"
        elif self.last_success_at:
            current_state = "healthy"
        else:
            current_state = "ready"
        return {
            "model": self.model,
            "request_count": self.request_count,
            "retry_count": self.retry_count,
            "failure_count": self.failure_count,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at or None,
            "last_success_at": self.last_success_at or None,
            "current_state": current_state,
            "recovered": bool(
                self.last_error_at and self.last_success_at > self.last_error_at
                and not self.last_error and not circuit_open
            ),
            "degradation_count": self.degradation_count,
            "last_degradation_at": self.last_degradation_at or None,
            "last_degradation_reason": self.last_degradation_reason,
            "last_payload_bytes": self.last_payload_bytes,
            "last_effective_payload_bytes": self.last_effective_payload_bytes,
            "consecutive_failures": any_failures,
            "circuit_open_until": any_open_until or None,
            "circuit_open_count": total_open_count,
            # 分桶明细（2026-10-02）：outbound / background 各自独立熔断。
            "circuits": circuits,
            # 跨桶汇总：任一桶熔断即为真，供 WebUI 一眼看到「上游正在熔断」。
            "circuit_open_until_any": any_open_until or None,
            "fallback_models": list(self.fallback_models),
            "failover_count": self.failover_count,
            "empty_completion_count": self.empty_completion_count,
            # 出站镜像计数（归因判据面）：与上面三项同源，但**只含** outbound 调用。
            "outbound_empty_completion_count": self.outbound_empty_completion_count,
            "outbound_failure_count": self.outbound_failure_count,
            "outbound_circuit_open_count": self.outbound_circuit_open_count,
            "last_empty_completion_at": self.last_empty_completion_at or None,
            "last_empty_completion_model": self.last_empty_completion_model,
            "last_failover_at": self.last_failover_at or None,
            "last_failover_from": self.last_failover_from,
            "last_failover_to": self.last_failover_to,
            "provider": self.provider,
            "module_routes": dict(self.module_models),
        }
