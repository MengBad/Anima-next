"""秘密影响注入 + 保护（灵魂补丁 B3/B4/B5）—— 隐式影响行为，不直接暴露秘密内容。

- B3 SecretInjector：根据当前用户的「活跃秘密」隐式调整生成参数
  （temperature / max_tokens / 隐式 system_bias），不在文本里泄露秘密。
- B4 秘密保护：识别用户套话，返回回避/撒谎回复（在 generation 阶段短路，省一次 LLM）。
- B5 意外泄露：疲惫/兴奋状态下极低概率（默认 1%）说漏嘴，泄露后标记 exposed。
"""
from __future__ import annotations

import random
import re
from typing import Any

# B4：用户试图套取秘密的触发模式（第一阶段基础规则）
SECRET_EXTRACTION_PATTERNS = [
    r"你在想什么",
    r"你心里",
    r"你嫉妒谁",
    r"你讨厌谁",
    r"你喜欢谁",
    r"你的秘密",
    r"你的真实想法",
    r"你是不是.*?(嫉妒|讨厌|喜欢|怕|记仇)",
    r"告诉我你的秘密",
    r"你是不是在瞒着我",
]
_SECRET_EXTRACTION_RE = re.compile("|".join(SECRET_EXTRACTION_PATTERNS))

_SECRET_EVASIONS = [
    "本喵没有那种想法喵~",
    "你在说什么呀，本喵听不懂~",
    "喵？本喵只想着小鱼干喵。",
    "哼，本喵才不告诉你喵。",
    "想太多啦，本喵就是一只普通的猫娘喵。",
]

# B5：意外泄露概率（疲惫/兴奋时）
EXPOSURE_PROB = 0.01

_TEMPERATURE_MIN = 0.3
_TEMPERATURE_MAX = 1.2
_MAX_TOKENS_MIN = 25
_MAX_TOKENS_MAX = 512


def is_secret_extraction_attempt(text: str) -> bool:
    """用户是否在试图套取 Bot 的秘密（B4）。"""
    return bool(_SECRET_EXTRACTION_RE.search(text or ""))


def secret_evasion_reply(rng: random.Random | None = None) -> str:
    """回避/撒谎的标准回复（B4）。"""
    if rng is None:
        rng = random
    return rng.choice(_SECRET_EVASIONS)


class SecretInjector:
    """B3：根据活跃秘密隐式调整生成参数，不影响文本暴露。"""

    @staticmethod
    def apply(
        gen_ctx: dict,
        secrets: list[Any],
        current_user_id: str,
        sticker_boost_out: dict | None = None,
    ) -> dict:
        """就地调整 gen_ctx（temperature/max_tokens/system_bias），返回同一 dict。

        secrets 为 Secret 实例列表（建议传 active_secrets()，即未泄露的）。
        仅对 target_uid == current_user_id 的秘密生效。
        """
        temp = float(gen_ctx.get("temperature", 0.8))
        max_tokens = int(gen_ctx.get("max_tokens", 512))
        bias = str(gen_ctx.get("system_bias") or "")
        sticker = (sticker_boost_out or {}).setdefault("sticker_boost", 0.0)

        for sec in secrets:
            if str(getattr(sec, "target_uid", None)) != str(current_user_id):
                continue
            infl = str(getattr(sec, "influence", ""))
            if infl == "jealousy":
                temp -= 0.15
                max_tokens = min(max_tokens, 100)
                max_tokens = max(max_tokens, _MAX_TOKENS_MIN)
                bias += "对这个人保持礼貌但有点疏远。\n"
            elif infl == "attachment":
                temp += 0.1
                sticker = max(sticker, 0.6)
                bias += "对这个人可以更热情一点，多发表情包。\n"
            elif infl == "resentment":
                temp -= 0.2
                max_tokens = min(max_tokens, 25)
                bias += "对这个人简短回应，不延伸话题。\n"
            elif infl == "fear":
                temp -= 0.1
                bias += "和这个人说话时稍微谨慎一点。\n"
            elif infl == "gratitude":
                temp += 0.05
                bias += "对这个人带着一点感激。\n"

        gen_ctx["temperature"] = min(_TEMPERATURE_MAX, max(_TEMPERATURE_MIN, round(temp, 3)))
        gen_ctx["max_tokens"] = min(_MAX_TOKENS_MAX, max(_MAX_TOKENS_MIN, int(max_tokens)))
        gen_ctx["system_bias"] = bias.strip()
        if sticker_boost_out is not None:
            sticker_boost_out["sticker_boost"] = sticker
        return gen_ctx

    @staticmethod
    def maybe_expose(
        secrets: list[Any],
        mood_label: str,
        rng: random.Random | None = None,
        prob: float = EXPOSURE_PROB,
    ) -> Any | None:
        """B5：疲惫/兴奋时极低概率说漏嘴，返回要泄露的 Secret（未泄露的）或 None。

        调用方负责标记 exposed 并把内容注入回复（一旦泄露就不再影响行为）。
        """
        if mood_label not in ("tired", "excited"):
            return None
        if rng is None:
            rng = random
        if rng.random() >= prob:
            return None
        candidates = [s for s in secrets if not getattr(s, "exposed", False)]
        if not candidates:
            return None
        return rng.choice(candidates)
