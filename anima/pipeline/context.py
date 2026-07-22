"""LLM 上下文预算与裁剪。

这里不依赖某个单一模型的 tokenizer：部署端可能使用 OpenAI、Gemini 或兼容
网关。若安装了 ``tiktoken`` 则使用 ``o200k_base``；否则采用对中文更保守的
Unicode 估算。目标不是精确计费，而是在请求发出前稳定地保留人格、当前输入和
最近上下文，避免旧历史或可选认知片段无界膨胀。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import math
from typing import Any, Iterable


@lru_cache(maxsize=1)
def _token_encoder():
    """优先使用可选 tokenizer；未安装时静默回退。"""
    try:
        import tiktoken  # type: ignore[import-not-found]

        return tiktoken.get_encoding("o200k_base")
    except Exception:
        return None


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0x3040 <= code <= 0x30FF
        or 0xAC00 <= code <= 0xD7AF
    )


def estimate_text_tokens(text: str) -> int:
    """估算文本 token，中文按字符而非旧的 ``len / 4`` 低估。"""
    if not text:
        return 0
    encoder = _token_encoder()
    if encoder is not None:
        try:
            return len(encoder.encode(text, disallowed_special=()))
        except Exception:
            pass

    # 中文在常见 BPE 中通常不是“四个字符一个 token”。保守估算可避免中文群聊
    # 长历史在网关处才被截断；非 CJK 文本则继续使用接近英语/URL 的估算。
    cjk = sum(1 for char in text if _is_cjk(char))
    non_ascii = sum(1 for char in text if not char.isascii() and not _is_cjk(char))
    ascii_count = len(text) - cjk - non_ascii
    return max(1, math.ceil(cjk * 1.45 + non_ascii * 1.2 + ascii_count / 3.2))


def _content_tokens(content: Any, image_token_estimate: int) -> int:
    if isinstance(content, str):
        return estimate_text_tokens(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if not isinstance(part, dict):
                total += estimate_text_tokens(str(part))
                continue
            kind = str(part.get("type", ""))
            if kind in {"image_url", "input_image"}:
                # data URI 可能数 MB，但视觉模型的上下文成本不等于 base64 字符数。
                total += image_token_estimate
            elif kind == "input_audio":
                total += image_token_estimate
            else:
                total += estimate_text_tokens(str(part.get("text", "")))
        return total
    return estimate_text_tokens(str(content or ""))


def estimate_message_tokens(message: dict[str, Any], image_token_estimate: int = 768) -> int:
    """估算单条 Chat Completions 消息（含少量协议开销）。"""
    total = 4 + _content_tokens(message.get("content"), image_token_estimate)
    tool_calls = message.get("tool_calls")
    if tool_calls:
        total += estimate_text_tokens(str(tool_calls))
    return total


@dataclass(frozen=True)
class PromptFragment:
    """可选 system 片段；数值越小，超预算时越先被删除。"""

    key: str
    content: str
    priority: int


@dataclass(frozen=True)
class CompositionStats:
    budget: int
    estimated_tokens: int
    dropped_keys: tuple[str, ...] = ()
    protected_over_budget: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "budget": self.budget,
            "estimated_tokens": self.estimated_tokens,
            "dropped_keys": list(self.dropped_keys),
            "protected_over_budget": self.protected_over_budget,
        }


class ContextComposer:
    """对可选提示片段与聊天历史执行稳定、可解释的 token 预算裁剪。"""

    def __init__(
        self,
        *,
        input_token_budget: int = 12000,
        system_token_budget: int = 6000,
        image_token_estimate: int = 768,
        tool_result_token_budget: int = 1800,
    ) -> None:
        self.input_token_budget = max(1024, int(input_token_budget))
        self.system_token_budget = max(1024, min(int(system_token_budget), self.input_token_budget))
        self.image_token_estimate = max(64, int(image_token_estimate))
        self.tool_result_token_budget = max(128, int(tool_result_token_budget))

    @classmethod
    def from_config(cls, config: Any = None) -> "ContextComposer":
        return cls(
            input_token_budget=getattr(config, "input_token_budget", 12000),
            system_token_budget=getattr(config, "system_token_budget", 6000),
            image_token_estimate=getattr(config, "image_token_estimate", 768),
            tool_result_token_budget=getattr(config, "tool_result_token_budget", 1800),
        )

    def compose_system(
        self,
        base: str,
        fragments: Iterable[PromptFragment],
        suffix: str,
    ) -> tuple[str, CompositionStats]:
        """组装单一 system。

        ``base``（人格模板）与 ``suffix``（身份/角色护栏）不可裁；可选片段仅在
        超预算时按优先级删除，最终仍保持调用方给出的原始相对顺序。
        """
        items = [item for item in fragments if item.content and item.content.strip()]
        required = estimate_text_tokens(base + suffix)
        kept = list(items)
        dropped: list[str] = []
        total = required + sum(estimate_text_tokens(item.content) for item in kept)

        # 同优先级时优先删除更早注入的片段，保留本轮更靠后的/更相关的信息。
        for _, item in sorted(enumerate(items), key=lambda pair: (pair[1].priority, pair[0])):
            if total <= self.system_token_budget:
                break
            kept.remove(item)
            total -= estimate_text_tokens(item.content)
            dropped.append(item.key)

        content = base + "".join(item.content for item in kept) + suffix
        final_tokens = estimate_text_tokens(content)
        return content, CompositionStats(
            budget=self.system_token_budget,
            estimated_tokens=final_tokens,
            dropped_keys=tuple(dropped),
            protected_over_budget=required > self.system_token_budget,
        )

    def compose_messages(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], CompositionStats]:
        """保留所有 system 与最后一条用户输入，按从新到旧的顺序保留历史。"""
        if not messages:
            return [], CompositionStats(self.input_token_budget, 0)

        last_user = next(
            (index for index in range(len(messages) - 1, -1, -1)
             if messages[index].get("role") == "user"),
            None,
        )
        protected = {
            index for index, message in enumerate(messages)
            if message.get("role") == "system"
        }
        if last_user is not None:
            protected.add(last_user)

        costs = [estimate_message_tokens(message, self.image_token_estimate) for message in messages]
        selected = set(protected)
        total = sum(costs[index] for index in selected)
        dropped: list[str] = []

        # 历史必须从近到远退让，不能为了塞入一个很早的片段打断最近语境。
        for index in range(len(messages) - 1, -1, -1):
            if index in protected:
                continue
            if total + costs[index] <= self.input_token_budget:
                selected.add(index)
                total += costs[index]
            else:
                dropped.append(f"history:{index}")

        composed = [dict(message) for index, message in enumerate(messages) if index in selected]
        return composed, CompositionStats(
            budget=self.input_token_budget,
            estimated_tokens=total,
            dropped_keys=tuple(reversed(dropped)),
            protected_over_budget=total > self.input_token_budget and not any(
                index not in protected for index in selected
            ),
        )

    def compact_tool_result(self, result: str) -> str:
        """限制单个工具观察值，防止网页正文把后续 ReAct 轮次撑爆。"""
        if estimate_text_tokens(result) <= self.tool_result_token_budget:
            return result

        # 用二分查找最大前缀，而不是“截一点再追加 marker”的循环：后者在
        # marker 比删除字符还长时会反向增长，极端网页正文下无法收敛。
        marker = "\n[工具结果过长，后文已截断]"
        marker_tokens = estimate_text_tokens(marker)
        if marker_tokens >= self.tool_result_token_budget:
            return marker

        low, high = 0, len(result)
        while low < high:
            middle = (low + high + 1) // 2
            candidate = result[:middle].rstrip() + marker
            if estimate_text_tokens(candidate) <= self.tool_result_token_budget:
                low = middle
            else:
                high = middle - 1
        return result[:low].rstrip() + marker
