"""个人能力的归一化签名 + 近似匹配（v0.6.1 防爆炸的核心去重逻辑，v0.7.1 调严）。

从 main.py 抽出，无外部依赖，可独立测试。

v0.7.1 修复（基于生产日志观察到 103 个能力 / 0 次使用的现象）：
- 驼峰拆分：`EgoForge` 拆成 `ego` + `forge` 后才能命中同义词表
- 核心语义槽位匹配：含同义词归一化键（_self_/_weapon_/_anchor_/_block_/_rebuild_）
  的两个能力，只要共享 ≥2 个核心槽位就视为同概念合并，不再要求覆盖率 40%
- 1 槽位 + 该槽位是核心同义槽位时也合并（之前要求严格命中）
"""

import re
from typing import List, Set


# 同义词归一化：把 "ego/我/U+6211" 这种近义词映射到同一语义槽位
_SYNONYMS = {
    "ego": "_self_", "self": "_self_", "selfhood": "_self_",
    "u6211": "_self_", "myself": "_self_",
    "我": "_self_", "自我": "_self_",

    "anchor": "_anchor_", "anchoring": "_anchor_", "anchored": "_anchor_",
    "锚": "_anchor_", "锚点": "_anchor_", "锚定": "_anchor_",

    "blade": "_weapon_", "axe": "_weapon_", "weapon": "_weapon_", "weapons": "_weapon_",
    "slicer": "_weapon_", "sentinel": "_weapon_", "forge": "_weapon_",
    "戉": "_weapon_", "兵戈": "_weapon_", "兵刃": "_weapon_", "利刃": "_weapon_",
    "凶器": "_weapon_", "刑器": "_weapon_", "大戉": "_weapon_", "刃": "_weapon_",
    "行刑": "_weapon_", "肢解": "_weapon_",

    "block": "_block_", "blocks": "_block_", "blockwise": "_block_",
    "方块": "_block_", "construct": "_block_",

    "rebuild": "_rebuild_", "reconstruction": "_rebuild_", "reconstruct": "_rebuild_",
    "重构": "_rebuild_", "重塑": "_rebuild_",

    "resonance": "_resonance_", "resonate": "_resonance_",
    "共鸣": "_resonance_", "共振": "_resonance_",

    "alignment": "_align_", "align": "_align_",
    "对齐": "_align_", "对准": "_align_",

    "locator": "_anchor_", "locate": "_anchor_", "locating": "_anchor_",
    "beacon": "_anchor_", "信标": "_anchor_",
}

# 通用停用词
_STOP = {
    "the", "and", "for", "with", "this", "that", "into", "from", "其",
    "一", "了", "的", "和", "我的", "正在",
}

# v0.7.1：核心语义槽位（这些是真正"决定能力是同一概念"的关键词）
# 当两个能力共享 ≥2 个核心槽位时，无视新签名大小直接合并
_CORE_SLOTS = {"_self_", "_weapon_", "_anchor_", "_block_", "_rebuild_", "_resonance_", "_align_"}


def _split_camelcase(s: str) -> str:
    """把 EgoForge / EgoBlockAnchor 拆成 'ego forge' / 'ego block anchor'。
    保留原字符串，只在驼峰边界插空格。"""
    # 处理 "EgoForge" -> "Ego Forge"，并处理连续大写如 "URLParser" -> "URL Parser"
    s = re.sub(r'([a-z])([A-Z])', r'\1 \2', s)
    s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', s)
    return s


def _char_ngrams(text: str, n: int = 2) -> Set[str]:
    """字符级 n-gram 集合（v0.9.4）。对中文长名稳健，不依赖分词。
    剥掉空白与常见标点噪音后再切。"""
    if not text:
        return set()
    cleaned = re.sub(r'[\s\(\)\[\]【】「」『』，。、:：;；,\.\-_/\\]+', '', text.lower())
    if len(cleaned) < n:
        return {cleaned} if cleaned else set()
    return {cleaned[i:i + n] for i in range(len(cleaned) - n + 1)}


def text_similarity(a: str, b: str) -> float:
    """两段文本的字符 2-gram Jaccard 相似度（v0.9.4，0.0–1.0）。
    用于无核心语义槽位的中文长名能力去重兜底。出错返回 0.0（视为不相似）。
    v0.9.6: 完全相等的非空文本短路返回 1.0（含全标点/全空白等 ngram 抽空的情况）。"""
    try:
        if a == b:
            return 1.0 if (a or "").strip() else 0.0
        ga, gb = _char_ngrams(a, 2), _char_ngrams(b, 2)
        if not ga or not gb:
            return 0.0
        inter = len(ga & gb)
        union = len(ga | gb)
        return inter / union if union else 0.0
    except Exception:
        return 0.0


def normalize_capability_signature(name: str, description: str = "") -> Set[str]:
    """把能力名 + 描述归一化成关键词集合，用于近似去重。

    策略：
    - 驼峰拆分：EgoForge -> ego forge（v0.7.1）
    - 抽英文 stem（≥3 字母）
    - 中文用滑动窗口抽 2-字与 3-字短语
    - 同义词归一化为 _self_ / _weapon_ / _anchor_ 等语义槽位
    - 去通用停用词
    - 不在同义词表里的纯中文短片段不进入签名（避免噪音），只有英文 stem 与归一化语义槽位算数
    """
    if not name:
        return set()
    text = (name + " " + description[:200])
    text = _split_camelcase(text).lower()

    en_words = set(re.findall(r'[a-z]{3,}', text))
    # 额外抽出 "u6211" 这种字母+数字混合形式（用于映射到 _self_）
    en_alphanum = set(re.findall(r'[a-z]+\d+', text))
    en_words |= en_alphanum

    cn_pieces: Set[str] = set()
    for run in re.findall(r'[\u4e00-\u9fff]+', text):
        for n in (2, 3):
            for i in range(len(run) - n + 1):
                cn_pieces.add(run[i:i + n])

    words = en_words | cn_pieces
    normalized: Set[str] = set()
    for w in words:
        mapped = _SYNONYMS.get(w)
        if mapped:
            normalized.add(mapped)
        elif len(w) >= 3 and re.fullmatch(r'[a-z]+', w):
            normalized.add(w)
        # 其余短中文片段不进入签名

    # v0.7.1：直接用同义词表 key 做 substring 匹配，捕获滑窗抓不到的单字符
    # 同义词（如"戉"、"刃"、"我"），并避免被 2-3 字滑窗噪音稀释
    for syn_key, slot in _SYNONYMS.items():
        # 只对包含中文字符的同义词 key 做 substring 匹配（英文已被分词覆盖）
        if any('\u4e00' <= ch <= '\u9fff' for ch in syn_key) and syn_key in text:
            normalized.add(slot)

    return normalized - _STOP


def find_similar_capability(
    name: str, description: str, others: List[dict], text_threshold: float = 0.6
) -> int:
    """在已有能力列表里找一个语义近似的，返回索引；没找到返回 -1。

    v0.7.1 调严的分级门槛：
    - **核心槽位优先**：两个能力共享 ≥2 个核心同义槽位（_self_/_weapon_/_anchor_/...）时，
      无论新签名大小直接合并 —— 这是 v0.7.1 的关键修复，专治"戉系/Ego系"能力家族增殖
    - 新签名 ≥ 4 槽位：要求 ≥ 2 个 overlap 且占新签名 ≥ 30%（之前 40%）
    - 新签名 2-3 槽位：要求 ov ≥ 2（之前要求 ov==n）
    - 新签名 1 槽位：仅在该槽位是核心同义槽位时合并

    v0.9.4 泛化：语义槽位匹配未命中时，追加**通用文本相似度兜底**（名+描述的字符
    2-gram Jaccard ≥ text_threshold 即视为同概念）。覆盖无核心槽位的中文长名能力，
    且阈值偏高（默认 0.6）以保证不相关能力不被误合并。
    """
    new_sig = normalize_capability_signature(name, description)
    new_core = new_sig & _CORE_SLOTS
    new_text = (name or "") + " " + (description or "")

    best_idx = -1
    best_overlap = 0
    # 文本相似度兜底的最佳候选（独立于槽位重叠的打分）
    best_text_idx = -1
    best_text_sim = 0.0

    for i, c in enumerate(others):
        c_text = (c.get("name", "") or "") + " " + (c.get("description", "") or "")
        # 通用文本相似度兜底候选（无论是否有签名都算）
        sim = text_similarity(new_text, c_text)
        if sim >= text_threshold and sim > best_text_sim:
            best_text_sim = sim
            best_text_idx = i

        old_sig = normalize_capability_signature(
            c.get("name", ""), c.get("description", "")
        )
        if not new_sig or not old_sig:
            continue
        overlap = new_sig & old_sig
        ov = len(overlap)
        if ov == 0:
            continue

        n = len(new_sig)
        matched = False

        # v0.7.1 新规则：核心槽位重叠 ≥ 2 → 一定合并
        core_overlap = overlap & _CORE_SLOTS
        if len(core_overlap) >= 2:
            matched = True
        elif n >= 4 and ov >= 2 and ov >= max(2, int(n * 0.3)):
            matched = True
        elif 2 <= n <= 3 and ov >= 2:
            matched = True
        elif n == 1:
            only_key = next(iter(new_sig))
            if only_key in _CORE_SLOTS and only_key in old_sig:
                matched = True

        if matched and ov > best_overlap:
            best_overlap = ov
            best_idx = i

    # 语义槽位匹配优先；未命中则用文本相似度兜底
    if best_idx >= 0:
        return best_idx
    return best_text_idx
