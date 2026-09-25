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
    ):
        self.base_endpoint = endpoint.rstrip("/")
        self.endpoint = self.base_endpoint + "/chat/completions"
        self.images_endpoint = self.base_endpoint + "/images/generations"
        self.api_key = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        self.model = model
        self.provider = provider or "default"
        self.module_models = dict(module_models or {})
        self.usage_ledger = usage_ledger
        # 上游地址不可达时，不能让 QQ 消息在 TCP 建连阶段一直占满总超时。
        # 正常已连接的长回复仍可使用完整的 total 时限。
        self.timeout = aiohttp.ClientTimeout(
            total=timeout,
            sock_connect=min(timeout, 10.0),
        )
        self._session: Optional[aiohttp.ClientSession] = None
        self.retries = max(0, min(int(retries), 4))
        self.retry_backoff_sec = max(0.0, min(float(retry_backoff_sec), 5.0))
        self.fallback_models = [
            str(item).strip() for item in (fallback_models or [])
            if str(item).strip() and str(item).strip() != model
        ][:4]
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

    def set_usage_ledger(self, ledger: UsageLedger | None) -> None:
        self.usage_ledger = ledger

    def set_model_routes(self, routes: dict[str, str] | None) -> None:
        self.module_models = {str(k): str(v) for k, v in (routes or {}).items() if str(v).strip()}

    def model_for(self, module: str | None = None, explicit: str | None = None) -> str:
        if explicit:
            return explicit
        info = current_call_info()
        return self.module_models.get(module or info.module) or self.model

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
            payload["model"] = candidate
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
        event = UsageEvent(
            created_at=time.time(), trace_id=info.trace_id or "unscoped", module=info.module,
            operation=info.operation, provider=self.provider,
            requested_model=str(payload.get("model") or self.model), actual_model=actual,
            input_tokens=inp, output_tokens=out, cached_tokens=cached, total_tokens=total,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            retries=max(0, self.retry_count - retry_start), status=status,
            estimated=estimated, error=self.last_error if status != "ok" else "",
        )
        try:
            await self.usage_ledger.arecord(event)
        except Exception:
            logger.exception("LLM 用量账本写入失败")

    async def _try_post(self, payload: dict[str, Any], headers: dict[str, str]) -> dict | None:
        """仅对可恢复故障进行有限重试，避免鉴权错误造成无意义等待。"""
        for attempt in range(self.retries + 1):
            try:
                async with self._session.post(  # type: ignore[union-attr]
                    self.endpoint, json=payload, headers=headers,
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

            if attempt < self.retries:
                self.retry_count += 1
                delay = self.retry_backoff_sec * (2 ** attempt)
                logger.warning("LLM 请求失败，%.1fs 后第 %d/%d 次重试：%s",
                               delay, attempt + 1, self.retries, self.last_error)
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
        circuit_open = self.circuit_open_until > now
        if circuit_open:
            current_state = "circuit_open"
        elif self.last_error and self.consecutive_failures > 0:
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
            "consecutive_failures": self.consecutive_failures,
            "circuit_open_until": self.circuit_open_until or None,
            "circuit_open_count": self.circuit_open_count,
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
