"""时间线管理：防止 Bot 翻旧账。

超过阈值（默认 30 分钟）的话题不再注入 LLM 上下文，
避免 Bot 把几个小时前的旧对话当成「仍在继续」而自顾自辩护 / 延续。
"""
from __future__ import annotations

import time
from typing import Optional


class TimelineManager:
    """判断话题是否过时，并过滤过时的上下文消息。"""

    STALE_THRESHOLD_MINUTES = 30

    @classmethod
    def is_stale_topic(
        cls,
        current_time: float,
        referenced_time: float,
        threshold_minutes: Optional[float] = None,
    ) -> bool:
        """判断 referenced_time 对应的事件是否已过时（相对 current_time）。"""
        if not referenced_time:
            return False
        thr = (threshold_minutes if threshold_minutes is not None
               else cls.STALE_THRESHOLD_MINUTES) * 60
        return (current_time - referenced_time) > thr

    @classmethod
    def filter_stale_context(
        cls,
        messages: list,
        current_time: float,
        time_key: str = "time",
        referenced_key: str = "is_explicitly_referenced",
        threshold_minutes: Optional[float] = None,
    ) -> list:
        """过滤掉超过时间窗的旧消息，保留新鲜消息与被显式引用的消息。

        messages 中每条应是 dict；时间字段默认取 ``time``，回退 ``ts`` / ``timestamp``。
        """
        if not messages:
            return []
        fresh = []
        for m in messages:
            if not isinstance(m, dict):
                fresh.append(m)
                continue
            t = m.get(time_key) or m.get("ts") or m.get("timestamp")
            if cls.is_stale_topic(current_time, t, threshold_minutes):
                # 旧话题：除非被显式引用（如 reply 指向它），否则丢弃
                if m.get(referenced_key):
                    fresh.append(m)
                continue
            fresh.append(m)
        return fresh
