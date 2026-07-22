"""群氛围分析模块：从群消息流识别交流风格与氛围。

设计要点：
- 纯启发式、零依赖、可离线运行；同时保留 LLM 增强的扩展点。
- ``analyze`` 对一批消息做一次性分类；``merge`` 用指数滑动方式增量更新画像，
  使群氛围随时间演进但仍稳定（避免单条消息抖动）。
"""
from __future__ import annotations

import re
import time
from .profiles import AtmosphereStyle, AtmosphereProfile

# 技术讨论群信号词
TECH_TERMS = {
    "rust", "python", "代码", "算法", "架构", "docker", "k8s", "kubernetes",
    "api", "bug", "编译", "部署", "linux", "git", "数据库", "优化",
    "函数", "服务端", "前端", "后端", "协议", "并发",
}
# 游戏/幽默群信号词
GAME_SLANGS = {
    "上分", "carry", "gg", "队友", "开黑", "氪金", "抽卡", "副本", "boss",
    "菜鸡", "666", "卧槽", "牛逼", "yyds", "awsl", "lol", "骚操作", "老哥",
    "爷", "整", "绝绝子", "栓Q", "emo",
}
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")

DESCRIPTORS = {
    AtmosphereStyle.FORMAL: ["严谨", "专业", "术语密集", "逻辑清晰"],
    AtmosphereStyle.HUMOROUS: ["活泼", "梗多", "emoji 密集", "情绪外放"],
    AtmosphereStyle.CASUAL: ["轻松", "口语化", "生活化"],
    AtmosphereStyle.NEUTRAL: ["中性", "无明显风格倾向"],
}


def _features(messages: list[str]) -> dict:
    total = len(messages) or 1
    text = " ".join(messages)
    tech = sum(text.lower().count(t) for t in TECH_TERMS)
    game = sum(sum(m.count(s) for s in GAME_SLANGS) for m in messages)
    emoji = len(EMOJI_RE.findall(text))
    exc = text.count("！") + text.count("!")
    q = text.count("？") + text.count("?")
    avg_len = sum(len(m) for m in messages) / total
    return {
        "tech": tech, "game": game, "emoji": emoji,
        "exclamation": exc, "question": q,
        "avg_len": round(avg_len, 1), "total": len(messages),
    }


def analyze(messages: list[str]) -> AtmosphereProfile:
    """对一批消息做一次性氛围分类。"""
    f = _features(messages)
    formal_score = f["tech"] * 2 + f["question"]
    humorous_score = f["game"] * 2 + f["emoji"] + f["exclamation"]
    casual_score = 1.0 if f["avg_len"] < 40 else 0.3

    scores = {
        AtmosphereStyle.FORMAL: formal_score,
        AtmosphereStyle.HUMOROUS: humorous_score,
        AtmosphereStyle.CASUAL: casual_score,
    }
    best = max(scores, key=scores.get)
    denom = sum(scores.values()) + 1e-6
    conf = round(max(scores.values()) / denom, 3)
    if best == AtmosphereStyle.CASUAL and conf < 0.25:
        best = AtmosphereStyle.NEUTRAL
        conf = 0.2
    return AtmosphereProfile(
        group_id="", style=best, confidence=conf,
        descriptors=list(DESCRIPTORS[best]), signals=f, last_updated=time.time(),
    )


def merge(existing: AtmosphereProfile, new_messages: list[str],
          weight: float = 0.3) -> AtmosphereProfile:
    """用新观察增量更新既有画像（指数滑动平均）。"""
    fresh = analyze(new_messages)
    combined_conf = round(existing.confidence * (1 - weight) + fresh.confidence * weight, 3)
    # 新风格置信度高且与旧不同才切换，避免抖动
    style = fresh.style if (fresh.confidence > 0.5 and fresh.style != existing.style) else existing.style
    return AtmosphereProfile(
        group_id=existing.group_id, style=style, confidence=combined_conf,
        descriptors=list(DESCRIPTORS[style]), signals=fresh.signals,
        last_updated=time.time(),
    )


class AtmosphereAnalyzer:
    """面向运行时的分析器门面：缓存各群画像，支持 observe/query。"""

    def __init__(self) -> None:
        self._profiles: dict[str, AtmosphereProfile] = {}

    def observe(self, group_id: str, messages: list[str], weight: float = 0.3) -> AtmosphereProfile:
        if group_id in self._profiles:
            prof = merge(self._profiles[group_id], messages, weight)
        else:
            prof = analyze(messages)
            prof.group_id = group_id
        self._profiles[group_id] = prof
        return prof

    def get(self, group_id: str) -> AtmosphereProfile:
        return self._profiles.get(group_id, AtmosphereProfile(group_id=group_id))
