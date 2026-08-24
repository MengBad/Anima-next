"""表情包智能选图（对标 astrbot_plugin_stealer 的 meme_selector + smart_select，能力对等移植）。

在 StickerManager 之上叠加「多信号打分 + 加权随机」的选图逻辑：
- 分类/描述/标签/场景、使用次数、收藏、BM25、嵌入语义多路打分融合
- recency 分级惩罚（越新用惩罚越大，但不硬剔除）
- 在 top-k 高分候选中按权重随机，避免每次都挑同一张，并夹带少量多样性 bonus
- 否定词由混合相似度层负责，上层不再重复处理

所有打分函数为纯函数，便于单测。
"""
from __future__ import annotations

import random
from typing import Any, Optional

from .sticker_similarity import _extract_words, calculate_hybrid_similarity

_W_DESC = 0.35
_W_TAG = 0.25
_W_SCENE = 0.20
_CAT_BONUS_EXACT = 0.12
_CAT_BONUS_OTHER = 0.04
_USE_BONUS_CAP = 0.08
_USE_BONUS_RATE = 0.01
_FAVORITE_BONUS = 0.3
_BM25_BONUS_WEIGHT = 0.2
_EMB_BONUS_WEIGHT = 0.20
_MIN_BASE_SCORE = 0.15
_DIVERSITY_MAX = 0.15
_TOP_K = 3
_LOW_SCORE_DESC_MIN = 0.1
_DESC_BOOST_BIGRAM = 0.25
_DESC_BOOST_UNI = 0.1


def _parse_tags(raw):
    if isinstance(raw, str):
        return [t.strip().lower() for t in raw.split(",") if t.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(t).strip().lower() for t in raw if str(t).strip()]
    return []


def _desc_score(query, desc, query_words, desc_words):
    if not desc:
        return 0.0
    s = calculate_hybrid_similarity(query, desc)
    if s < 0.25:
        overlap = query_words & (desc_words or frozenset())
        bigram = sum(1 for w in overlap if len(w) >= 2)
        uni = len(overlap) - bigram
        boost = bigram * _DESC_BOOST_BIGRAM + uni * _DESC_BOOST_UNI
        if boost > 0:
            s = max(s, min(1.0, boost))
    return s


def _tag_score(query_lower, tags):
    if not tags:
        return 0.0
    matched = sum(1 for t in tags if t in query_lower)
    return min(1.0, matched / max(len(tags), 1))


def _scene_score(query_lower, scenes):
    if not scenes:
        return 0.0
    matched = sum(1 for t in scenes if t in query_lower)
    return min(1.0, matched / max(len(scenes), 1))


def score_entry(query, item, target_category="", bm25_tokens_overlap=None, embedding_score=None):
    q = str(query or "").strip()
    if not q:
        return 0.0
    q_lower = q.lower()
    q_words = set(_extract_words(q))

    category = str(item.get("category") or "")
    desc = str(item.get("desc") or "")
    tags = _parse_tags(item.get("tags") or item.get("labels") or [])
    scenes = _parse_tags(item.get("scenes") or [])

    desc_words = _extract_words(desc)
    dsc = _desc_score(q, desc, q_words, desc_words)
    tsc = _tag_score(q_lower, tags)
    ssc = _scene_score(q_lower, scenes)

    category_bonus = _CAT_BONUS_EXACT if category == target_category else _CAT_BONUS_OTHER
    use_count = int(item.get("used") or item.get("use_count") or 0)
    use_bonus = min(_USE_BONUS_CAP, use_count * _USE_BONUS_RATE)
    fav_bonus = _FAVORITE_BONUS if item.get("is_favorite") else 0.0
    bm25_bonus = (bm25_tokens_overlap or 0.0) * _BM25_BONUS_WEIGHT
    emb_bonus = (embedding_score if embedding_score is not None else 0.0) * _EMB_BONUS_WEIGHT

    base = (
        dsc * _W_DESC
        + tsc * _W_TAG
        + ssc * _W_SCENE
        + category_bonus
        + use_bonus
        + fav_bonus
        + bm25_bonus
        + emb_bonus
    )
    if base < _MIN_BASE_SCORE:
        return round(dsc, 4) if dsc > _LOW_SCORE_DESC_MIN else 0.0
    return round(max(0.0, base + random.uniform(0, _DIVERSITY_MAX)), 4)


def choose(scores, top_k=_TOP_K):
    if not scores:
        return None
    ranked = sorted(scores, key=lambda x: -x[2])
    top = ranked[:max(1, min(top_k, len(ranked)))]
    total = sum(t[2] for t in top)
    if total <= 0:
        return random.choice(top)[0]
    r = random.uniform(0, total)
    acc = 0.0
    for (h, _it, s) in top:
        acc += s
        if r <= acc:
            return h
    return top[-1][0]