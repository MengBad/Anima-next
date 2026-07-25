"""拟人分段发送 + 拟真延迟 —— 参考 astrbot_plugin_splitter 设计。

将 LLM 回复按标点断句、智能保护、均分策略拆成多段，
每段之间插入与情绪/关系联动的拟真打字延迟，让 bot 回复"像人"。
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from typing import Any

import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class SplitConfig:
    """分段 + 延迟配置。"""
    enabled: bool = True
    max_segments: int = 4
    min_segment_len: int = 40
    max_segment_len: int = 120
    trigger_threshold: int = 80       # 低于此字数不分段
    tail_merge_threshold: int = 15    # 尾段低于此字数合并到前段
    delay_enabled: bool = True
    delay_base: float = 0.4           # 基础延迟（秒）
    delay_factor: float = 0.02        # 每字延迟（秒/字）
    delay_jitter: float = 0.3         # 随机抖动上限（秒）


# ---------------------------------------------------------------------------
# 保护规则：成对符号 / 代码块 / URL 内不切
# ---------------------------------------------------------------------------

# 成对符号（开, 闭）
_PAIRED_SYMBOLS = [
    ("\u201c", "\u201d"),  # ""
    ("\uff08", "\uff09"),  # （）
    ("\u300a", "\u300b"),  # 《》
    ("\u3010", "\u3011"),  # 【】
    ("(", ")"),
    ("[", "]"),
]

# 断句标点（优先级从高到低）
_SENTENCE_ENDINGS = re.compile(r"[。！？～…\n]+")
# 次级断句（逗号/分号，用于过长段二次切分）
_CLAUSE_BREAKS = re.compile(r"[，；,;、]+")

# URL 正则
_URL_RE = re.compile(r"https?://\S+")
# 代码块
_CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```")


def _find_protected_ranges(text: str) -> list[tuple[int, int]]:
    """返回所有不可切割的字符区间。"""
    ranges: list[tuple[int, int]] = []

    # 代码块
    for m in _CODE_BLOCK_RE.finditer(text):
        ranges.append((m.start(), m.end()))

    # URL
    for m in _URL_RE.finditer(text):
        ranges.append((m.start(), m.end()))

    # 成对符号
    for open_ch, close_ch in _PAIRED_SYMBOLS:
        start = 0
        while True:
            i = text.find(open_ch, start)
            if i == -1:
                break
            j = text.find(close_ch, i + 1)
            if j == -1:
                break
            ranges.append((i, j + 1))
            start = j + 1

    # 合并重叠区间
    ranges.sort()
    merged: list[tuple[int, int]] = []
    for s, e in ranges:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _in_protected(pos: int, ranges: list[tuple[int, int]]) -> bool:
    """判断某位置是否在保护区间内。"""
    for s, e in ranges:
        if s < pos < e:
            return True
        if s > pos:
            break
    return False


# ---------------------------------------------------------------------------
# 核心分段逻辑
# ---------------------------------------------------------------------------

def split_text(text: str, cfg: SplitConfig | None = None) -> list[str]:
    """将文本按拟人策略分段。

    返回段落列表；若不需要分段则返回单元素列表。
    """
    cfg = cfg or SplitConfig()
    if not cfg.enabled:
        return [text]

    text = text.strip()
    if len(text) <= cfg.trigger_threshold:
        return [text]

    protected = _find_protected_ranges(text)

    # 第一遍：按句末标点切分
    raw_parts: list[str] = []
    last_end = 0
    for m in _SENTENCE_ENDINGS.finditer(text):
        cut_pos = m.end()
        if _in_protected(cut_pos - 1, protected):
            continue
        part = text[last_end:cut_pos].strip()
        if part:
            raw_parts.append(part)
        last_end = cut_pos
    # 尾部残余
    tail = text[last_end:].strip()
    if tail:
        raw_parts.append(tail)

    if not raw_parts:
        return [text]

    # 第二遍：过长段按次级标点再切
    parts: list[str] = []
    for part in raw_parts:
        if len(part) <= cfg.max_segment_len:
            parts.append(part)
            continue
        # 尝试按逗号/分号切
        sub_parts: list[str] = []
        sub_last = 0
        part_protected = _find_protected_ranges(part)
        for m in _CLAUSE_BREAKS.finditer(part):
            cut_pos = m.end()
            if _in_protected(cut_pos - 1, part_protected):
                continue
            sub = part[sub_last:cut_pos].strip()
            if sub:
                sub_parts.append(sub)
            sub_last = cut_pos
        sub_tail = part[sub_last:].strip()
        if sub_tail:
            sub_parts.append(sub_tail)
        if sub_parts:
            parts.extend(sub_parts)
        else:
            parts.append(part)

    # 第三遍：智能合并 —— 将短段合并到理想段长
    segments = _merge_to_segments(parts, cfg)

    # 尾段过短合并
    if len(segments) > 1 and len(segments[-1]) < cfg.tail_merge_threshold:
        segments[-2] = segments[-2] + segments[-1]
        segments.pop()

    return segments if segments else [text]


def _merge_to_segments(parts: list[str], cfg: SplitConfig) -> list[str]:
    """将碎片合并为不超过 max_segments 的段落。"""
    if len(parts) <= cfg.max_segments:
        # 段数已满足，但可能有过短段需合并
        return _merge_short_parts(parts, cfg)

    # 需要合并：计算理想段长
    total_len = sum(len(p) for p in parts)
    ideal_len = total_len / cfg.max_segments

    segments: list[str] = []
    current = ""
    consumed_idx = 0
    for i, part in enumerate(parts):
        if not current:
            current = part
            consumed_idx = i + 1
        elif len(current) + len(part) + 1 <= ideal_len * 1.3:
            current = current + part
            consumed_idx = i + 1
        else:
            segments.append(current)
            current = part
            consumed_idx = i + 1
            if len(segments) >= cfg.max_segments - 1:
                # 剩余全部合并为最后一段
                break
    # 把剩余 parts 合并到最后
    if current:
        segments.append(current)
    # 合并未消费的 parts
    for j in range(consumed_idx, len(parts)):
        segments[-1] = segments[-1] + parts[j]

    return _merge_short_parts(segments, cfg)


def _greedy_merge(parts: list[str], max_segments: int, ideal_len: float) -> list[str]:
    """贪心合并碎片到 max_segments 段。"""
    segments: list[str] = []
    current = ""
    for part in parts:
        if not current:
            current = part
        elif len(current) + len(part) <= ideal_len * 1.4 and len(segments) < max_segments - 1:
            current = current + part
        else:
            segments.append(current)
            current = part
    if current:
        segments.append(current)
    # 如果仍超出，强制合并最后几段
    while len(segments) > max_segments:
        segments[-2] = segments[-2] + segments[-1]
        segments.pop()
    return segments


def _merge_short_parts(parts: list[str], cfg: SplitConfig) -> list[str]:
    """将过短段（< min_segment_len/2）合并到相邻段。"""
    if len(parts) <= 1:
        return parts
    merged: list[str] = []
    for part in parts:
        if merged and len(merged[-1]) < cfg.min_segment_len // 2:
            merged[-1] = merged[-1] + part
        else:
            merged.append(part)
    # 最后一段过短向前合并
    if len(merged) > 1 and len(merged[-1]) < cfg.min_segment_len // 2:
        merged[-2] = merged[-2] + merged[-1]
        merged.pop()
    return merged


# ---------------------------------------------------------------------------
# 拟真延迟计算（A2）
# ---------------------------------------------------------------------------

# 情绪 → 打字速度因子
_MOOD_SPEED_FACTOR = {
    "excited": 0.6,
    "happy": 0.7,
    "curious": 0.8,
    "neutral": 1.0,
    "bored": 1.3,
    "tired": 1.5,
    "annoyed": 1.2,
    "sad": 1.3,
}

# 关系阶段 → 首段延迟范围（秒）
_RELATION_FIRST_DELAY = {
    "CLOSE": (0.3, 0.8),
    "FRIEND": (0.5, 1.2),
    "ACQUAINTANCE": (0.8, 1.8),
    "STRANGER": (1.5, 3.0),
}


def compute_delay(
    segment_text: str,
    segment_index: int,
    cfg: SplitConfig | None = None,
    mood_label: str = "neutral",
    relation_stage: str = "ACQUAINTANCE",
) -> float:
    """计算某段发送前的拟真延迟（秒）。

    - segment_index=0 为首段，受关系调制（"要不要回"的犹豫）
    - 后续段受情绪调制（打字快慢）
    """
    cfg = cfg or SplitConfig()
    if not cfg.delay_enabled:
        return 0.0

    char_count = len(segment_text)
    speed_factor = _MOOD_SPEED_FACTOR.get(mood_label, 1.0)

    if segment_index == 0:
        # 首段：关系决定"犹豫时间"
        lo, hi = _RELATION_FIRST_DELAY.get(relation_stage, (0.8, 1.8))
        base_delay = random.uniform(lo, hi)
        # annoyed 时额外犹豫
        if mood_label == "annoyed":
            base_delay += random.uniform(0.5, 1.5)
        return base_delay
    else:
        # 后续段：模拟打字时间
        delay = cfg.delay_base + char_count * cfg.delay_factor * speed_factor
        delay += random.uniform(0, cfg.delay_jitter)
        return round(delay, 2)


# ---------------------------------------------------------------------------
# 高层接口：分段 + 延迟一体化
# ---------------------------------------------------------------------------

@dataclass
class SplitResult:
    """分段结果。"""
    segments: list[str] = field(default_factory=list)
    delays: list[float] = field(default_factory=list)  # 每段发送前延迟

    @property
    def is_single(self) -> bool:
        return len(self.segments) <= 1


def split_reply(
    text: str,
    cfg: SplitConfig | None = None,
    mood_label: str = "neutral",
    relation_stage: str = "ACQUAINTANCE",
) -> SplitResult:
    """将回复文本分段并计算每段延迟。"""
    cfg = cfg or SplitConfig()
    segments = split_text(text, cfg)
    delays = [
        compute_delay(seg, i, cfg, mood_label, relation_stage)
        for i, seg in enumerate(segments)
    ]
    return SplitResult(segments=segments, delays=delays)
