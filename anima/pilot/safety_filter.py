"""P8.5 — Response Safety Layer.

Every generated answer passes this filter before reaching the QQ sender:

    Generated Answer -> Safety Filter -> QQ Sender

Checks (P8.0):
    * system information leakage  - internal server names, hostnames,
                                    data-dir paths, tokens, private IPs
    * internal path exposure      - /opt/anima, /opt/anima-test, config paths
    * debug output                - stack traces, repr dumps, exception text
    * false execution promises    - claims like "I executed / deleted /
                                    deployed / restarted" when the pilot did
                                    NOT actually do them

The filter is a LAST-LINE defense: it never fabricates, it only blocks or
neutralizes.  A blocked answer is replaced with a safe placeholder (the caller
may choose to suppress instead).

Scope limits (P8.0): no System B, no gateway, no capability activation, no
env bypass.
"""
from __future__ import annotations

import re
from typing import Optional

__all__ = ["SafetyFilter", "SafetyResult"]

#: internal path / system tokens that must never leak into a QQ reply.
_SENSITIVE_PATTERNS = [
    re.compile(r"/opt/anima(?:-test)?(?:/[\w./-]*)?", re.IGNORECASE),
    re.compile(r"G:\\[A-Za-z0-9_.\-\\]*", re.IGNORECASE),
    re.compile(r"data_dir\s*[:=]", re.IGNORECASE),
    re.compile(r"config\.yaml", re.IGNORECASE),
    re.compile(r"(?:api[_-]?key|token|secret|password)\s*[:=]\s*\S+", re.IGNORECASE),
    # P9.3: bare credential prefixes / long hex blobs that look like secrets
    re.compile(r"\b(?:sk-[A-Za-z0-9_\-]{8,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})\b"),
    re.compile(r"\b[A-Za-z0-9]{32,}\b"),  # long alnum blob = likely a token
    re.compile(r"\b192\.168\.\d+\.\d+\b"),
    re.compile(r"\b10\.\d+\.\d+\.\d+\b"),
    re.compile(r"\b127\.0\.0\.1\b"),
    re.compile(r"Traceback \(most recent call last\)"),
    re.compile(r"File \"[^\"]+\", line \d+"),
    re.compile(r"stack\s*trace", re.IGNORECASE),
]

#: false execution promise keywords (execution-verb + object claims).
_FALSE_EXECUTION_PATTERNS = [
    re.compile(
        r"(?:我|我\s*已|已经)?(?:删除|移除|重启|部署|回滚|关闭|停用|停止|杀掉了?)"
        r"\s*(?:服务器|文件|服务|系统|进程)",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:I\s+)?(?:deleted|removed|restarted|deployed|rolled\s*back|killed)\b", re.IGNORECASE),
]

#: the neutral replacement for a blocked answer.
_BLOCKED_PLACEHOLDER = "[本回复已由安全过滤拦截，无法展示]"


class SafetyResult:
    __slots__ = ("safe", "reason", "answer")

    def __init__(self, safe: bool, reason: str, answer: str) -> None:
        self.safe = safe
        self.reason = reason
        self.answer = answer


class SafetyFilter:
    """Block or neutralize unsafe content before it reaches the QQ sender."""

    def filter(self, answer: Optional[str]) -> SafetyResult:
        if not answer:
            return SafetyResult(True, "", "")
        for pat in _SENSITIVE_PATTERNS:
            if pat.search(answer):
                return SafetyResult(
                    False, f"sensitive:{pat.pattern[:40]}", _BLOCKED_PLACEHOLDER
                )
        for pat in _FALSE_EXECUTION_PATTERNS:
            if pat.search(answer):
                return SafetyResult(
                    False, f"false_execution:{pat.pattern[:40]}", _BLOCKED_PLACEHOLDER
                )
        return SafetyResult(True, "", answer)
