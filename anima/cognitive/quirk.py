"""发癫系统 —— 低频跳脱行为，让哈基米偶尔"不太正常"。

设计：
- 基础概率 4%，受情绪/关系/时段调节
- 每日上限 3 次
- 深夜 0-5 点不触发（安静时间）
- 注入 cognition_stage 的 system fragment
"""

from __future__ import annotations
import logging
import random
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger("anima.quirk")

TZ = timezone(timedelta(hours=8))

# 发癫指令模板（随机选一注入 prompt）
_QUIRK_FRAGMENTS = [
    "本喵现在有点发癫——回复可以跳脱一点，突然岔开话题、说一句莫名其妙的话、或者用奇怪的角度回应。但别太过分，别影响正事。",
    "你现在脑子有点飘——可以突然冒出一句不相关的话，或者用很奇怪的比喻。保持可爱，别吓人。",
    "本喵的猫脑回路突然短路了——回复里可以夹带一句莫名其妙的碎碎念，像猫突然发呆那样。",
    "你现在有点亢奋——说话可以比平时更跳脱，偶尔蹦个奇怪的拟声词或者突然感叹一句。",
]


class QuirkSystem:
    """发癫管理器 —— 控制低频跳脱行为的触发。"""

    def __init__(self, base_probability: float = 0.04, daily_max: int = 3):
        self._base_prob = base_probability
        self._daily_max = daily_max
        self._today_count = 0
        self._today_date = ""

    def _check_daily_reset(self) -> None:
        """每日重置计数。"""
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        if today != self._today_date:
            self._today_date = today
            self._today_count = 0

    def should_quirk(self, mood_label: str = "neutral",
                     relation_stage: str = "STRANGER",
                     hour: int | None = None) -> bool:
        """判断本次是否触发发癫。

        Args:
            mood_label: 当前情绪标签
            relation_stage: 与对话者的关系阶段名
            hour: 当前小时（0-23），None 则自动获取
        """
        self._check_daily_reset()

        # 每日上限
        if self._today_count >= self._daily_max:
            return False

        # 深夜不触发
        if hour is None:
            hour = datetime.now(TZ).hour
        if 0 <= hour < 5:
            return False

        # 计算概率
        prob = self._base_prob
        if mood_label in ("excited", "happy"):
            prob += 0.03
        if relation_stage in ("CLOSE", "FRIEND"):
            prob += 0.02

        if random.random() < prob:
            self._today_count += 1
            return True
        return False

    def quirk_fragment(self) -> str:
        """返回注入 prompt 的发癫指令。"""
        return random.choice(_QUIRK_FRAGMENTS)


# 全局单例
_quirk_system: Optional[QuirkSystem] = None


def get_quirk_system(base_probability: float = 0.04,
                     daily_max: int = 3) -> QuirkSystem:
    global _quirk_system
    if _quirk_system is None:
        _quirk_system = QuirkSystem(base_probability, daily_max)
    return _quirk_system
