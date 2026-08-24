"""表情包检索索引（对标 astrbot_plugin_stealer 的 meme_search_engine，能力对等移植）。

把库里的表情元数据（分类/描述/标签）建成 BM25 全文索引与打分引擎，供选图检索与
嵌入检索兜底使用。纯 Python / 无 astrbot 依赖；签名缓存让语料未变时零重建开销。
"""
from __future__ import annotations

import hashlib
from typing import Any, Iterable, Optional

from .sticker_similarity import (
    BM25,
    _extract_words,
    calculate_hybrid_similarity,
    tokenize_for_bm25,
)

SIMILARITY_THRESHOLD = 0.45


def _parse_tags(raw: Any) -> list[str]:
    """安全解析 tags/scenes：兼容字符串与列表。"""
    if isinstance(raw, str):
        return [t.strip().lower() for t in raw.split(",") if t.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(t).strip().lower() for t in raw if str(t).strip()]
    return []


class StickerSearchEngine:
    """BM25 索引 + 条目打分。键用表情哈希（hash），免于路径归一化。"""

    def __init__(self):
        self._bm25_index: Optional[BM25] = None
        self._bm25_doc_keys: list[str] = []
        self._bm25_dirty: bool = True
        self._bm25_signature: str = ""

    @staticmethod
    def _corpus_signature(entries: dict) -> str:
        """对语料做 sha256 指纹；未变则复用已建索引。"""
        if not entries:
            return "empty"
        hasher = hashlib.sha256()
        for key in sorted(entries):
            data = entries[key]
            if not isinstance(data, dict):
                continue
            category = str(data.get("category") or "")
            desc = str(data.get("desc") or "")
            tags = "\x1e".join(_parse_tags(data.get("tags") or []))
            scenes = "\x1e".join(_parse_tags(data.get("scenes") or []))
            payload = "\x1f".join([str(key), category, desc, tags, scenes])
            hasher.update(payload.encode("utf-8", errors="ignore"))
            hasher.update(b"\x00")
        return hasher.hexdigest()

    def invalidate(self) -> None:
        self._bm25_dirty = True
        self._bm25_index = None
        self._bm25_doc_keys = []

    def _ensure_index(self, entries: dict) -> None:
        """懒构建；语料签名一致则复用，不一致才重建。entries: {hash: meta}。"""
        signature = self._corpus_signature(entries)
        if not self._bm25_dirty and signature == self._bm25_signature:
            return
        documents: list[list[str]] = []
        doc_keys: list[str] = []
        for key, data in entries.items():
            if not isinstance(data, dict):
                continue
            category = str(data.get("category") or "")
            desc = str(data.get("desc") or "")
            tags = _parse_tags(data.get("tags") or [])
            scenes = _parse_tags(data.get("scenes") or [])
            text = " ".join([category, desc] + tags + scenes)
            tokens = tokenize_for_bm25(text)
            if tokens:
                documents.append(list(tokens))
                doc_keys.append(key)
        self._bm25_doc_keys = doc_keys
        self._bm25_index = BM25(documents) if documents else None
        self._bm25_signature = signature
        self._bm25_dirty = False

    def search(self, query: str, entries: dict, top_k: int = 8) -> list[str]:
        """BM25 检索，返回排序后的候选 keys。"""
        self._ensure_index(entries)
        if self._bm25_index is None or not self._bm25_doc_keys:
            return []
        tokens = tokenize_for_bm25(query)
        if not tokens:
            return []
        hits = self._bm25_index.get_top_k(tokens, k=top_k)
        return [self._bm25_doc_keys[idx] for idx, _ in hits if idx < len(self._bm25_doc_keys)]

    def score_entry(self, query: str, category: str, desc: str, tags: list) -> int:
        """单条自上而下打分（精确分类/包含/描述/标签/分词/混合相似度逐级拆）。"""
        q = str(query or "").lower().strip()
        if not q:
            return 0
        cat = str(category or "").lower()
        desc = str(desc or "").lower()
        tags = _parse_tags(tags)
        score = 0
        if q == cat:
            return 20
        if q in cat or cat in q:
            score = 10
        if q == desc:
            score = max(score, 15)
        elif q in desc:
            score = max(score, 12)
        elif tokens := tokenize_for_bm25(q):
            matched = sum(1 for t in tokens if t in desc)
            score = max(score, matched * 3)
        if score >= 15:
            return score
        if tags:
            if q == tags[0]:
                score = max(score, 12)
            elif q in tags[0]:
                score = max(score, 8)
            if tokens := tokenize_for_bm25(q):
                for tag in tags:
                    m = sum(1 for t in tokens if t in tag)
                    score = max(score, m * 2)
                    if score >= 15:
                        return score
        if score < 10:
            qw = _extract_words(q)
            dw = _extract_words(desc)
            overlap = qw & dw
            if overlap:
                bigram = sum(1 for w in overlap if len(w) >= 2)
                uni = len(overlap) - bigram
                score = max(score, bigram * 6 + uni * 3)
            tag_words: frozenset = frozenset()
            for tag in tags:
                tag_words |= _extract_words(tag)
            tov = qw & tag_words
            if tov:
                tb = sum(1 for w in tov if len(w) >= 2)
                tu = len(tov) - tb
                score = max(score, tb * 7 + tu * 3)
        if score < 10:
            for target in [cat, desc] + tags[:2]:
                if len(target) > 1 and len(q) > 1:
                    sim = calculate_hybrid_similarity(q, target)
                    if sim >= SIMILARITY_THRESHOLD:
                        score = max(score, int(4 + (sim - SIMILARITY_THRESHOLD) * 16))
                        break
        return score
