"""时间感知 —— 提供时间上下文，注入 LLM prompt。

设计：
- 每次对话更新用户最后交互时间
- 生成「当前时间描述」文本注入 system prompt
- 检测「久违用户」（24h+ 没说过话）

持久化：data/time_sense.json，重启恢复。
"""

from __future__ import annotations
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from anima.core.persistence import PersistenceProvider

# 北京时间
TZ = timezone(timedelta(hours=8))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def beijing_now() -> datetime:
    return datetime.now(TZ)


def time_description() -> str:
    """生成当前时间的自然语言描述。"""
    now = beijing_now()
    hour = now.hour
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]

    if hour < 6:
        period = "凌晨"
    elif hour < 9:
        period = "早晨"
    elif hour < 12:
        period = "上午"
    elif hour < 14:
        period = "中午"
    elif hour < 18:
        period = "下午"
    elif hour < 22:
        period = "晚上"
    else:
        period = "深夜"

    return f"{now.strftime('%Y年%m月%d日')} {weekday} {period}{hour}点{now.minute:02d}分"


class TimeSense:
    """记录用户交互时间，检测久违用户。"""

    def __init__(self, data_dir: str = "data"):
        self.last_interaction: dict[int, float] = {}  # user_id → timestamp
        self.interaction_count: dict[int, int] = {}    # user_id → 24h 内次数
        self._last_cleanup = time.time()
        self._persist = PersistenceProvider(data_dir)
        self._path = "time_sense.json"

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def restore(self) -> None:
        data = self._persist.read_json(self._path, {})
        self.last_interaction = {
            int(k): v for k, v in data.get("last_interaction", {}).items()
        }
        self.interaction_count = {
            int(k): v for k, v in data.get("interaction_count", {}).items()
            if int(k) in self.last_interaction
        }

    def persist(self) -> None:
        data = {
            "last_interaction": {str(k): v for k, v in self.last_interaction.items()},
            "interaction_count": {str(k): v for k, v in self.interaction_count.items()},
        }
        self._persist.write_json(self._path, data)

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def touch(self, user_id: int) -> None:
        """记录一次用户交互。"""
        now = time.time()
        self.last_interaction[user_id] = now
        self.interaction_count[user_id] = self.interaction_count.get(user_id, 0) + 1
        self._prune()

    def time_since(self, user_id: int) -> tuple[float, str]:
        """返回与某用户的间隔时间（秒）和自然语言标签。"""
        now = time.time()
        last = self.last_interaction.get(user_id, 0)
        gap = now - last

        if gap < 900:
            label = "刚刚"
        elif gap < 7200:
            label = "刚才"
        elif gap < 86400:
            label = "几小时前"
        elif gap < 86400 * 3:
            label = "昨天/前天"
        else:
            label = "很久没见了"

        return gap, label

    def get_long_lost(self, threshold: float = 86400) -> list[tuple[int, float, str]]:
        """获取超过阈值（默认24h）没交互的用户，按时间倒序。"""
        now = time.time()
        result = []
        for uid, last in self.last_interaction.items():
            gap = now - last
            if gap > threshold:
                _, label = self.time_since(uid)
                result.append((uid, gap, label))
        result.sort(key=lambda x: -x[1])
        return result[:3]  # 最多 3 个

    def _prune(self) -> None:
        """清理 48h 无活动的记录。"""
        now = time.time()
        cutoff = now - 86400 * 2
        stale = [uid for uid, t in self.last_interaction.items() if t < cutoff]
        for uid in stale:
            del self.last_interaction[uid]
            self.interaction_count.pop(uid, None)


# 全局单例
_time_sense: TimeSense | None = None


def get_time_sense(data_dir: str = "data") -> TimeSense:
    global _time_sense
    if _time_sense is None:
        _time_sense = TimeSense(data_dir)
        _time_sense.restore()
    return _time_sense


def get_time_context() -> str:
    """获取当前时间 + 久违用户提示，注入 system prompt。"""
    ts = get_time_sense()
    parts = [f"当前时间：{time_description()}"]
    long_lost = ts.get_long_lost()
    if long_lost:
        names = ", ".join(f"QQ{uid}" for uid, _, _ in long_lost)
        parts.append(f"有一阵子没说话的人：{names}")
    return "\n".join(parts)
