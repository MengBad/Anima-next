"""霸凌检测器 —— 识别辱骂 / 攻击性消息，输出霸凌强度与连续计数。

设计目标：让 bot 在「被连续攻击」时知道闭嘴，而不是讨好 + 辩解 + 长篇分段。
- is_bullying：当前消息是否命中辱骂模式
- severity：1~5，严重词加权
- recent_count：当前用户「连续」辱骂次数（含本条），供「连骂 3 次完全沉默」判定
"""
from __future__ import annotations

import re
from typing import Optional

# 霸凌关键词 / 模式（高强度辱骂，含组合句式）
_BULLYING_PATTERNS = [
    r"傻逼",
    r"废物",
    r"蠢货",
    r"滚",
    r"闭嘴",
    r"垃圾",
    r"贱",
    r"畜生",
    r"去死",
    r"恶心",
    r"没用的东西",
    r"废物一个",
    r"废物ai",
    r"废物ai就该被狠狠踩头",
    r"你个?垃圾",
    r"狠狠踩头",
]
_BULLYING_RE = re.compile("|".join(f"(?:{p})" for p in _BULLYING_PATTERNS))

# 严重辱骂（提升 severity）
_SEVERE_PATTERNS = ["去死", "踩头", "畜生", "贱", "傻逼", "废物ai", "狠狠踩头"]

# 连续计数回溯上限（只看最近 N 条历史，避免翻旧账造成永久沉默）
_RECENT_LOOKBACK = 8


def detect_bullying(
    user_id: int,
    message_text: str,
    recent_history: Optional[list[str]] = None,
) -> dict:
    """检测一条消息是否为霸凌 / 攻击。

    Args:
        user_id: 消息发送者（目前仅用于日志 / 对齐签名，检测本身不依赖）。
        message_text: 当前消息文本。
        recent_history: 该用户最近的消息文本列表（旧→新），不含当前这条。

    Returns:
        {
            "is_bullying": bool,
            "severity": int,      # 0（非霸凌）或 1~5
            "recent_count": int,  # 连续辱骂次数（含本条）；当前非霸凌时为 0
        }
    """
    text = (message_text or "").strip()
    is_b = bool(_BULLYING_RE.search(text))

    severity = 0
    if is_b:
        severe_hit = sum(1 for p in _SEVERE_PATTERNS if p in text)
        severity = min(5, 1 + severe_hit * 2)

    # 连续计数：从 recent_history 末尾往前数连续的霸凌消息
    recent_count = 0
    if recent_history:
        for h in reversed(recent_history[-_RECENT_LOOKBACK:]):
            if _BULLYING_RE.search(h or ""):
                recent_count += 1
            else:
                break
    # 含当前这条
    if is_b:
        recent_count += 1

    return {
        "is_bullying": is_b,
        "severity": severity,
        "recent_count": recent_count,
    }
