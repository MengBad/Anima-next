"""轻重双模型前置判断（Gate）—— 参考 astrbot_plugin_angel_heart 的双 AI 核设计。

群消息进入完整 Pipeline 前，用轻量模型做 <500ms 快速判断：
- need_reply: 是否需要回复
- reply_type: 预判类型（chat/question/tool/emotional）
- urgency: 紧急程度 0-1

永远跳过 Gate 的情况（安全阀）：
- @ 自己的消息
- 回复自己的消息
- owner 的消息
- 私聊消息
- 含 !anima 指令的消息
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class GateDecision:
    """Gate 判断结果。"""
    need_reply: bool = True
    reply_type: str = "chat"  # chat | question | tool | emotional
    urgency: float = 0.5
    skipped: bool = False     # 是否跳过了 Gate（安全阀触发）
    latency_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "need_reply": self.need_reply,
            "reply_type": self.reply_type,
            "urgency": self.urgency,
            "skipped": self.skipped,
            "latency_ms": round(self.latency_ms, 1),
        }


# ---------------------------------------------------------------------------
# Gate 系统
# ---------------------------------------------------------------------------

# 判断提示词（极简，让轻量模型快速输出 JSON）
_GATE_PROMPT = """你是一个消息分类器。判断这条群消息是否需要 bot 回复。

规则：
- 如果消息是闲聊、提问、求助、情感表达 → need_reply=true
- 如果消息是纯表情、纯图片、无意义灌水、系统通知 → need_reply=false
- reply_type: chat(闲聊) | question(提问) | tool(需要工具) | emotional(情感)
- urgency: 0(不急) ~ 1(很急，如求助、紧急问题)

只输出 JSON，不要解释：
{"need_reply": true/false, "reply_type": "...", "urgency": 0.0~1.0}

消息内容：
"""


class Gate:
    """轻重双模型前置判断器。"""

    def __init__(
        self,
        llm_client: Any,
        model: str = "",
        timeout_ms: int = 500,
        fallback_to_full: bool = True,
    ):
        self._llm = llm_client
        self._model = model or None  # None 表示继承默认
        self._timeout_sec = timeout_ms / 1000.0
        self._fallback = fallback_to_full
        self._stats = {
            "total": 0,
            "skipped": 0,
            "passed": 0,
            "blocked": 0,
            "errors": 0,
            "total_latency_ms": 0.0,
        }

    def should_skip_gate(
        self,
        event: dict[str, Any],
        text: str,
        user_id: int,
        is_owner: bool,
        self_id: int,
    ) -> bool:
        """判断是否应跳过 Gate（安全阀）。"""
        # 私聊永远不跳过（但 Gate 本身只用于群聊）
        # @ 自己
        if event.get("at_me") or event.get("is_at_me"):
            return True
        # 回复自己
        if event.get("reply_to_self"):
            return True
        # owner
        if is_owner:
            return True
        # 指令
        if text.strip().startswith("!anima"):
            return True
        # 含 @ 段（OneBot 格式）
        message = event.get("message") or []
        if isinstance(message, list):
            for seg in message:
                if isinstance(seg, dict) and seg.get("type") == "at":
                    if str(seg.get("data", {}).get("qq")) == str(self_id):
                        return True
        return False

    async def evaluate(
        self,
        text: str,
        event: dict[str, Any],
        user_id: int,
        is_owner: bool,
        self_id: int,
    ) -> GateDecision:
        """评估群消息是否需要回复。"""
        self._stats["total"] += 1

        # 安全阀检查
        if self.should_skip_gate(event, text, user_id, is_owner, self_id):
            self._stats["skipped"] += 1
            return GateDecision(need_reply=True, skipped=True)

        # 空消息或纯图片
        if not text or not text.strip():
            self._stats["blocked"] += 1
            return GateDecision(need_reply=False, reply_type="chat", urgency=0.0)

        # 调用轻量模型
        start = time.monotonic()
        try:
            decision = await self._call_light_model(text)
            latency = (time.monotonic() - start) * 1000
            decision.latency_ms = latency
            self._stats["total_latency_ms"] += latency

            if decision.need_reply:
                self._stats["passed"] += 1
            else:
                self._stats["blocked"] += 1
            return decision

        except asyncio.TimeoutError:
            self._stats["errors"] += 1
            logger.debug("Gate 超时，fallback=%s", self._fallback)
            return GateDecision(need_reply=self._fallback, skipped=True)
        except Exception:
            self._stats["errors"] += 1
            logger.exception("Gate 评估异常")
            return GateDecision(need_reply=self._fallback, skipped=True)

    async def _call_light_model(self, text: str) -> GateDecision:
        """调用轻量模型做判断。"""
        if self._llm is None:
            return GateDecision(need_reply=True, skipped=True)

        prompt = _GATE_PROMPT + text[:500]  # 截断防溢出

        try:
            # 使用 asyncio.wait_for 实现超时控制
            response = await asyncio.wait_for(
                self._llm.chat(
                    messages=[{"role": "user", "content": prompt}],
                    model=self._model,
                    temperature=0.1,
                    max_tokens=100,
                ),
                timeout=self._timeout_sec,
            )

            # 解析 JSON 响应
            content = response if isinstance(response, str) else str(response)
            # 提取 JSON（可能被包裹在 ```json ... ``` 中）
            if "```" in content:
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            content = content.strip()

            data = json.loads(content)
            return GateDecision(
                need_reply=bool(data.get("need_reply", True)),
                reply_type=str(data.get("reply_type", "chat")),
                urgency=float(data.get("urgency", 0.5)),
            )
        except json.JSONDecodeError:
            # JSON 解析失败，默认通过
            logger.debug("Gate JSON 解析失败: %s", content[:100])
            return GateDecision(need_reply=True, skipped=True)

    def get_stats(self) -> dict[str, Any]:
        """获取统计信息。"""
        total = self._stats["total"]
        return {
            **self._stats,
            "avg_latency_ms": (
                self._stats["total_latency_ms"] / total if total > 0 else 0.0
            ),
            "block_rate": (
                self._stats["blocked"] / total if total > 0 else 0.0
            ),
        }


# ---------------------------------------------------------------------------
# 单例管理
# ---------------------------------------------------------------------------

_gate_instance: Optional[Gate] = None


def get_gate(
    llm_client: Any = None,
    model: str = "",
    timeout_ms: int = 500,
    fallback_to_full: bool = True,
) -> Gate:
    """获取或创建 Gate 单例。"""
    global _gate_instance
    if _gate_instance is None:
        _gate_instance = Gate(
            llm_client=llm_client,
            model=model,
            timeout_ms=timeout_ms,
            fallback_to_full=fallback_to_full,
        )
    return _gate_instance
