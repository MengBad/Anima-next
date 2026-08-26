"""表情包文本相似度与检索原语（对标 astrbot_plugin_stealer 的 text_similarity.py，能力对等移植）。

纯 Python / 无 astrbot 依赖，供 StickerManager 的检索与选图复用：
- 中文分词（unigram+bigram）与 BM25 tokenizer
- 多策略混合相似度（ngram Jaccard / 余弦 / 子串 / 中文字符 / 编辑距离），含否定词语义反转
- BM25 索引（稠密打分，配合可持久化缓存）

所有字符串函数都带 lru_cache，适合热路径重复调用。
"""
from __future__ import annotations

import heapq
import math
import re
from collections import Counter
from functools import lru_cache
from typing import Iterable, Optional

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_PUNCT_RE = re.compile(r"[^\w\s]")
_EN_WORD_RE = re.compile(r"^[a-zA-Z]+$")
_REPEAT_CHAR_RE = re.compile(r"([\u4e00-\u9fffA-Za-z])\1{2,}")

# 混合相似度权重（默认与 stealer 行为一致；运行时可按配置覆盖）。
_SIM_WEIGHTS: dict[str, float] = {
    "ngram": 0.28,
    "cosine": 0.25,
    "substring": 0.12,
    "char": 0.08,
    "edit": 0.27,
}
_NEGATION_PENALTY: float = 0.25
_BM25_K1: float = 1.5
_BM25_B: float = 0.75
_EPSILON: float = 0.25

# 否定词集合（语义反转检测）。
# 否定字符集单源：取自 stickers._NEGATION_CHARS（M3 归并重复字面量）
from anima.cognitive.stickers import _NEGATION_CHARS as _NEGATION_WORDS


def configure_similarity(
    weights: Optional[dict[str, float]] = None,
    negation_penalty: Optional[float] = None,
) -> None:
    """覆盖混合相似度权重 / 否定惩罚（仅合法更新），并清空缓存保持一致。"""
    global _NEGATION_PENALTY
    changed = False
    if weights:
        for key, value in weights.items():
            if key in _SIM_WEIGHTS and isinstance(value, (int, float)) and value >= 0:
                _SIM_WEIGHTS[key] = float(value)
                changed = True
    if isinstance(negation_penalty, (int, float)) and 0 <= negation_penalty <= 1:
        _NEGATION_PENALTY = float(negation_penalty)
        changed = True
    if changed:
        _calculate_hybrid_similarity_cached.cache_clear()


class BM25:
    """经典 BM25 稠密打分索引（k1=1.5, b=0.75），EPFLSION 平滑与 stealer 一致。"""

    def __init__(self, corpus: Iterable[Iterable[str]],
                 k1: Optional[float] = None, b: Optional[float] = None):
        self.k1 = k1 if k1 is not None else _BM25_K1
        self.b = b if b is not None else _BM25_B
        self.corpus_size = 0
        self.avgdl = 1.0
        self.doc_freqs: dict[str, int] = {}
        self.idf: dict[str, float] = {}
        self.doc_len: list[int] = []
        self.doc_term_freqs: list[dict[str, int]] = []
        self._inverted_index: dict[str, set[int]] = {}
        self._initialize(corpus)

    def _initialize(self, corpus: Iterable[Iterable[str]]) -> None:
        nd: dict[str, list[int]] = {}
        self.doc_len = []
        self.doc_term_freqs = []
        self._inverted_index = {}
        for doc_idx, document in enumerate(corpus):
            doc_len = 0
            freq: dict[str, int] = {}
            # 一次性物化，避免对生成器二次消费
            for word in document:
                w = str(word).lower()
                doc_len += 1
                freq[w] = freq.get(w, 0) + 1
                self._inverted_index.setdefault(w, set()).add(doc_idx)
            self.doc_len.append(doc_len)
            self.doc_term_freqs.append(freq)
            for w, c in freq.items():
                nd.setdefault(w, []).append(c)
            self.corpus_size += 1
        self.avgdl = sum(self.doc_len) / self.corpus_size if self.corpus_size else 1.0
        for w, freq_list in nd.items():
            df = len(freq_list)
            self.doc_freqs[w] = df
            self.idf[w] = math.log((self.corpus_size - df + 0.5) / (df + 0.5) + 1)

    def get_top_k(self, query: Iterable[str], k: int = 10) -> list[tuple[int, float]]:
        query_counts = Counter(str(t).lower() for t in query)
        candidates: set[int] = set()
        for term in query_counts:
            if term in self._inverted_index:
                candidates.update(self._inverted_index[term])
        if not candidates:
            return []
        heap: list[tuple[float, int]] = []  # (-score, idx)
        for doc_idx in candidates:
            score = self._calculate_score(query_counts, doc_idx)
            if len(heap) < k:
                heapq.heappush(heap, (-score, doc_idx))
            elif -heap[0][0] < score:
                heapq.heapreplace(heap, (-score, doc_idx))
        result = [(doc_idx, -score) for score, doc_idx in heap]
        result.sort(key=lambda x: x[1], reverse=True)
        return result

    def _calculate_score(self, query_counts: Counter, doc_idx: int) -> float:
        score = 0.0
        doc_tf = self.doc_term_freqs[doc_idx]
        doc_len = self.doc_len[doc_idx]
        k1, b = self.k1, self.b
        for term, qc in query_counts.items():
            df = self.doc_freqs.get(term)
            if not df:
                continue
            tf = doc_tf.get(term, 0)
            if tf == 0:
                continue
            idf = self.idf[term]
            denom = tf + k1 * (1 - b + b * doc_len / self.avgdl)
            score += qc * idf * tf * (k1 + 1) / (denom + _EPSILON)
        return score


@lru_cache(maxsize=8192)
def _normalize_text(text: str) -> str:
    if not str(text or "").strip():
        return ""
    return _REPEAT_CHAR_RE.sub(r"\1\1", str(text or "").lower().strip())


@lru_cache(maxsize=8192)
def tokenize_for_bm25(text: str) -> tuple[str, ...]:
    """中文 unigram+bigram+trigram、英文整词。供 BM25 语料与查询使用。"""
    cleaned = _PUNCT_RE.sub(" ", str(text or "").lower())
    tokens: list[str] = []
    cn_buffer: list[str] = []
    en_buffer: list[str] = []
    for ch in cleaned:
        if _CJK_RE.match(ch):
            if en_buffer:
                tokens.append("".join(en_buffer))
                en_buffer = []
            cn_buffer.append(ch)
        elif ch.isalnum():
            en_buffer.append(ch)
        else:
            if en_buffer:
                tokens.append("".join(en_buffer))
                en_buffer = []
            _flush_cn(cn_buffer, tokens)
            cn_buffer = []
    if en_buffer:
        tokens.append("".join(en_buffer))
    _flush_cn(cn_buffer, tokens)
    return tuple(tokens)


def _flush_cn(chars: list, out: list) -> None:
    n = len(chars)
    for i in range(n):
        out.append(chars[i])
        if i + 1 < n:
            out.append(chars[i] + chars[i + 1])
        if i + 2 < n:
            out.append(chars[i] + chars[i + 1] + chars[i + 2])
    chars.clear()


def _has_negation_prefix(text: str, keyword: str) -> bool:
    """检测 keyword 是否被否定词前缀反转（如「不开心」→「开心」）。"""
    value = str(text or "").lower()
    key = str(keyword or "").strip().lower()
    if not key or len(key) < 2:
        return False
    idx = value.find(key)
    if idx <= 0:
        return False
    return value[idx - 1] in _NEGATION_WORDS


@lru_cache(maxsize=8192)
def _extract_words(text: str) -> frozenset[str]:
    """特征词集合：中文 unigram+bigram、英文整词。"""
    words: set[str] = set()
    cleaned = _PUNCT_RE.sub(" ", str(text or "").lower())
    cn_chars: list[str] = []
    for ch in cleaned:
        if _CJK_RE.match(ch):
            cn_chars.append(ch)
        else:
            if cn_chars:
                _add_chinese_ngrams(cn_chars, words)
                cn_chars = []
    if cn_chars:
        _add_chinese_ngrams(cn_chars, words)
    for token in cleaned.split():
        if _EN_WORD_RE.match(token) and len(token) > 1:
            words.add(token)
    return frozenset(words)


def _add_chinese_ngrams(chars: list, words: set) -> None:
    n = len(chars)
    for i in range(n):
        words.add(chars[i])
        if i + 1 < n:
            words.add(chars[i] + chars[i + 1])


def _extract_chinese_chars(text: str) -> frozenset[str]:
    return frozenset(c for c in str(text or "") if _CJK_RE.match(c))


@lru_cache(maxsize=8192)
def _collect_phrase_words(items: tuple[str, ...]) -> frozenset[str]:
    out: set[str] = set()
    for item in items:
        out.update(_extract_words(item))
    return frozenset(out)


def calculate_simple_similarity(text1: str, text2: str) -> float:
    t1, t2 = _normalize_text(text1), _normalize_text(text2)
    if not t1 or not t2:
        return 0.0
    w1, w2 = _extract_words(t1), _extract_words(t2)
    if not w1 or not w2:
        return 0.0
    inter = w1 & w2
    union = w1 | w2
    return len(inter) / len(union) if union else 0.0


def _levenshtein_distance(s1: str, s2: str) -> int:
    if len(s1) < len(s2):
        s1, s2 = s2, s1
    if not s2:
        return len(s1)
    previous = list(range(len(s2) + 1))
    for i, ch1 in enumerate(s1):
        current = [i + 1]
        for j, ch2 in enumerate(s2):
            current.append(min(
                previous[j + 1] + 1,
                current[j] + 1,
                previous[j] + (ch1 != ch2),
            ))
        previous = current
    return previous[-1]


def calculate_levenshtein_similarity(text1: str, text2: str) -> float:
    t1, t2 = _normalize_text(text1), _normalize_text(text2)
    if not t1 or not t2:
        return 0.0
    distance = _levenshtein_distance(t1, t2)
    max_len = max(len(t1), len(t2))
    return 1.0 - distance / max_len if max_len else 1.0


def calculate_cosine_similarity(text1: str, text2: str) -> float:
    t1, t2 = _normalize_text(text1), _normalize_text(text2)
    if not t1 or not t2:
        return 0.0
    if t1 == t2:
        return 1.0
    c1, c2 = Counter(tokenize_for_bm25(t1)), Counter(tokenize_for_bm25(t2))
    if not c1 or not c2:
        return 0.0
    dot = sum(v * c2.get(t, 0) for t, v in c1.items())
    if dot <= 0:
        return 0.0
    n1 = math.sqrt(sum(v * v for v in c1.values()))
    n2 = math.sqrt(sum(v * v for v in c2.values()))
    return min(1.0, dot / (n1 * n2)) if n1 and n2 else 0.0


def _substring_similarity(text1: str, text2: str) -> float:
    t1, t2 = _normalize_text(text1), _normalize_text(text2)
    if not t1 or not t2:
        return 0.0
    short, long = (t1, t2) if len(t1) <= len(t2) else (t2, t1)
    if short in long:
        return max(0.7, len(short) / len(long))
    lcs = _longest_common_substring_length(short, long)
    return (lcs / len(short)) * 0.6 if lcs else 0.0


def _longest_common_substring_length(s1: str, s2: str) -> int:
    if len(s1) > len(s2):
        s1, s2 = s2, s1
    prev = [0] * (len(s2) + 1)
    best = 0
    for i in range(len(s1)):
        curr = [0] * (len(s2) + 1)
        for j in range(len(s2)):
            if s1[i] == s2[j]:
                curr[j + 1] = prev[j] + 1
                if curr[j + 1] > best:
                    best = curr[j + 1]
        prev = curr
    return best


def _chinese_char_similarity(text1: str, text2: str) -> float:
    c1, c2 = _extract_chinese_chars(_normalize_text(text1)), _extract_chinese_chars(_normalize_text(text2))
    if not c1 or not c2:
        return 0.0
    inter, union = c1 & c2, c1 | c2
    return len(inter) / len(union) if union else 0.0


def calculate_hybrid_similarity(text1: str, text2: str) -> float:
    """多策略融合相似度（含量否定惩罚）。"""
    if not text1 or not text2:
        return 0.0
    t1, t2 = _normalize_text(text1), _normalize_text(text2)
    if not t1 or not t2:
        return 0.0
    if t1 == t2:
        return 1.0
    if t1 > t2:
        t1, t2 = t2, t1
    return _calculate_hybrid_similarity_cached(t1, t2)


@lru_cache(maxsize=8192)
def _calculate_hybrid_similarity_cached(t1: str, t2: str) -> float:
    short, long = (t1, t2) if len(t1) <= len(t2) else (t2, t1)
    has_negation = _has_negation_prefix(long, short)

    ngram_sim = calculate_simple_similarity(t1, t2)
    cosine_sim = calculate_cosine_similarity(t1, t2)
    substr_sim = _substring_similarity(t1, t2)
    char_sim = _chinese_char_similarity(t1, t2)
    edit_sim = 0.0
    max_len = max(len(t1), len(t2))
    if max_len <= 20:
        edit_sim = calculate_levenshtein_similarity(t1, t2)
        if max_len <= 6:
            edit_sim *= 1.2

    score = (
        ngram_sim * _SIM_WEIGHTS["ngram"]
        + cosine_sim * _SIM_WEIGHTS["cosine"]
        + substr_sim * _SIM_WEIGHTS["substring"]
        + char_sim * _SIM_WEIGHTS["char"]
        + edit_sim * _SIM_WEIGHTS["edit"]
    )

    w1, w2 = _extract_words(t1), _extract_words(t2)
    overlap = w1 & w2
    if overlap:
        bigram = sum(1 for w in overlap if len(w) >= 2)
        unigram = len(overlap) - bigram
        score = min(1.0, score + min(0.10, bigram * 0.03 + unigram * 0.01))

    if edit_sim > 0.8 and max_len <= 6:
        score = max(score, edit_sim * 0.9)

    best_single = max(ngram_sim, cosine_sim, substr_sim, char_sim, edit_sim)
    if best_single > score:
        score = score * 0.5 + best_single * 0.5

    if has_negation:
        score *= _NEGATION_PENALTY
    return min(1.0, score)


def find_best_category_match(query: str, categories: Iterable[str],
                             threshold: float = 0.4) -> Optional[str]:
    """返回与 query 混合相似度最高的分类（>threshold），否则 None。"""
    best, best_score = None, 0.0
    for cat in categories:
        s = calculate_hybrid_similarity(query, cat)
        if s > best_score and s > threshold:
            best, best_score = cat, s
    return best


def find_similar_categories(query: str, categories: Iterable[str], top_n: int = 3) -> list[str]:
    scored = sorted(
        ((calculate_hybrid_similarity(query, cat), cat) for cat in categories),
        key=lambda x: -x[0],
    )
    return [cat for _, cat in scored[:top_n]]
