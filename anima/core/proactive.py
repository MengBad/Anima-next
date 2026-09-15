"""P1-4 主动优先级队列：让「有用」的主动发言排在「闲聊」前面。

背景（先 grounding 再改）：优先级此前**散落在各调用点硬编码**——
``scheduler.py`` 订阅通知 0.95、``autonomy.py`` 日历 0.9 / 债务 0.6 /
闲聊接话 0.35 / bot 社交 0.3，没有单一事实来源，新增来源只能再拍一个数。
更实际的缺口是：订阅**连续查询失败**只写审计、从不进主动队列，用户永远
看不到「订阅挂了」。

本模块提供：
- ``SOURCE_PRIORITY``：单一事实来源（按来源名取优先级，``subscription:xxx``
  这类带 ID 的来源取冒号前的族名）。
- ``rank_agenda()``：把候选议程按优先级降序排好，并顺带做「本轮不可发」过滤，
  供 ``autonomy`` 逐个尝试——避免队首一条被挡就整轮什么都不做（head-of-line）。
- ``URGENT_THRESHOLD``：>= 该值视为"要事"，日志与审计可据此分流。

不做什么：不绕过 quiet_hours / 全局冷却（计划明确要求遵守）。
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Optional

# 单一事实来源：数值越大越先说。
SOURCE_PRIORITY: dict[str, float] = {
    "subscription": 0.95,           # 订阅状态变化（既有 0.95，保持）
    "calendar": 0.9,                # 用户显式配置的日程（既有 0.9，保持）
    "subscription_anomaly": 0.85,   # P1-4 新增：订阅连续失败，得让人知道
    "debt_due": 0.8,                # 自己答应过的事到期（既有 0.6，提上来）
    "health_alert": 0.75,           # 健康/稳定性异常
    "promise": 0.6,                 # 承诺跟进议程
    "workflow": 0.5,
    "exploration": 0.4,
    "bot_social": 0.3,              # 主动找别的 bot 说话（既有 0.3，保持）
    "heartbeat": 0.25,              # 闲聊式主动接话（既有 0.35，压下去）
    "dream_share": 0.2,             # 早晨分享梦境
}

DEFAULT_PRIORITY = 0.5
URGENT_THRESHOLD = 0.7


def _cfg(cfg: Any, name: str, default: Any) -> Any:
    v = getattr(cfg, name, None)
    return default if v is None else v


def family_of(source: str) -> str:
    """``subscription:abc123`` → ``subscription``。"""
    return str(source or "").split(":", 1)[0].strip().lower()


def priority_for(source: str, cfg: Any = None) -> float:
    """取来源优先级；未知来源回落到 ``DEFAULT_PRIORITY``，绝不抛。"""
    overrides = _cfg(cfg, "source_priority", None)
    fam = family_of(source)
    if isinstance(overrides, dict):
        for key in (source, fam):
            if key in overrides:
                try:
                    return max(0.0, min(1.0, float(overrides[key])))
                except (TypeError, ValueError):
                    continue
    return SOURCE_PRIORITY.get(fam, DEFAULT_PRIORITY)


def is_urgent(source_or_priority, cfg: Any = None) -> bool:
    """是否"要事"（>= URGENT_THRESHOLD）。可传来源名或数值。"""
    if isinstance(source_or_priority, (int, float)):
        value = float(source_or_priority)
    else:
        value = priority_for(str(source_or_priority), cfg)
    return value >= float(_cfg(cfg, "urgent_threshold", URGENT_THRESHOLD))


def rank_agenda(items: Iterable[Any], now: float,
                group_allowed: Optional[Callable[[int], bool]] = None,
                limit: int = 5) -> list[Any]:
    """把待发言议程按优先级降序返回（同优先级先到先得）。

    ``group_allowed`` 返回 False 的群会被剔除（群 autonomy=false 等），
    调用方拿到的是**已过滤、已排序**的候选，可以逐个尝试而不被队首堵死。
    """
    candidates = []
    for it in items or []:
        try:
            if str(getattr(it, "status", "pending")) != "pending":
                continue
            if float(getattr(it, "not_before_at", 0) or 0) > now:
                continue
            gid = int(getattr(it, "group_id", 0) or 0)
            if gid <= 0:
                continue
            if group_allowed is not None and not group_allowed(gid):
                continue
        except Exception:
            continue
        candidates.append(it)
    candidates.sort(
        key=lambda it: (-float(getattr(it, "priority", 0.5) or 0.5),
                        float(getattr(it, "created_at", 0) or 0)),
    )
    return candidates[:max(1, int(limit))]
