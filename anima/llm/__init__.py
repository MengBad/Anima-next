"""LLM 客户端 —— OpenAI 兼容协议的异步 HTTP 客户端。"""

from __future__ import annotations
import asyncio
import base64
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
        """调用 OpenAI 兼容 images/generations，返回实际图片字节。

        AIClient2API 的 Gemini 路由会错误转换 ``size``，因此这里刻意不传
        尺寸字段，只接受服务端返回的 b64_json。任何非图片响应都视为失败。
        """
        if self._session is None:
            await self.start()
        requested_model = self.model_for(current_call_info().module, model)
        payload = {"model": requested_model, "prompt": prompt[:4000]}
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        self.request_count += 1
        started = time.perf_counter()
        retry_start = self.retry_count
        try:
            async with self._session.post(self.images_endpoint, json=payload, headers=headers) as response:  # type: ignore[union-attr]
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
        except Exception as exc:
            self.last_status = None
            self._set_error(f"image_unexpected:{type(exc).__name__}")
            self.failure_count += 1
            logger.exception("生图请求异常")
            return None
        try:
            item = (data.get("data") or [])[0]
            encoded = item.get("b64_json") if isinstance(item, dict) else None
            if not isinstance(encoded, str):
                raise ValueError("missing_b64_json")
            image = base64.b64decode(encoded, validate=True)
            if not image or (max_bytes > 0 and len(image) > max_bytes):
                raise ValueError("invalid_image_size")
        except (ValueError, TypeError, KeyError, IndexError) as exc:
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
            # 鉴权、参数、载荷等非瞬态问题切模型没有意义，也不能无限尝试。
            if not self._is_failoverable_status(self.last_status):
                break

        self.failure_count += 1
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.circuit_failure_threshold:
            self.circuit_open_until = time.time() + self.circuit_cooldown_sec
            self.circuit_open_count += 1
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
            "last_failover_at": self.last_failover_at or None,
            "last_failover_from": self.last_failover_from,
            "last_failover_to": self.last_failover_to,
            "provider": self.provider,
            "module_routes": dict(self.module_models),
        }
