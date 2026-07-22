"""群氛围画像的数据结构定义。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class AtmosphereStyle(str, Enum):
    """群交流风格分类。"""
    FORMAL = "formal"        # 技术讨论群：严谨、专业
    CASUAL = "casual"        # 闲聊群：轻松、口语
    HUMOROUS = "humorous"    # 游戏群：活泼、幽默、玩梗
    NEUTRAL = "neutral"      # 中性、无明显倾向


@dataclass
class AtmosphereProfile:
    """单个群的风格画像。"""
    group_id: str
    style: AtmosphereStyle = AtmosphereStyle.NEUTRAL
    confidence: float = 0.0
    descriptors: list[str] = field(default_factory=list)   # 风格描述词
    signals: dict = field(default_factory=dict)           # 原始信号（词频/密度等）
    sample_phrases: list[str] = field(default_factory=list)
    last_updated: float = 0.0
