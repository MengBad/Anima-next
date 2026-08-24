"""中文自然语言情绪分析（对标 astrbot_plugin_stealer 的 emotion 处理，能力对等移植）。

现有 detect_reply_emotion 只做「关键词∈文本」的包含计数；本模块在它之上叠加：
- 否定前缀反转（复用 _has_negation_prefix）：「不开心」「别笑了」不计票且不给它反向情绪
- 词序/短语：bigram 词干重叠，捕获「裂开大笑」「气到发抖」这类短语
- 多信号归一：把 bigram/关键词/情感基数词加权融合，返回 canonical 情绪 + 强度
- 纯本地零 API（零成本），不触发任何 LLM/网络

供同步被动路径（_build_reply_segments）直接调用，也可作 VLM 情绪判断的本地快径二段。
"""
from __future__ import annotations

from typing import Optional

from .sticker_similarity import _has_negation_prefix, _extract_words

# canonical 情绪 -> 触发词簇（含短语/口语）
_EMOTION_PHRASES: dict[str, list[str]] = {
    "happy": ["开心", "高兴", "哈哈", "牛逼", "爽", "绝了", "裂开大笑", "笑死", "乐了", "棒", "太好了", "耶"],
    "sad": ["难过", "伤心", "委屈", "泪目", "哭", "失落", "沮丧", "蓝瘦", "想哭", "唉", "难受"],
    "angry": ["生气", "愤怒", "气死", "炸毛", "气疯", "恼火", "烦", "滚", "恶心", "火大", "气到发抖"],
    "afraid": ["害怕", "怕", "慌", "恐惧", "怂", "心虚", "担心", "紧张", "瑟瑟发抖"],
    "surprised": ["卧槽", "震惊", "想不到", "没想到", "惊了", "震撼", "离谱", "逆天"],
    "disgust": ["yue", "吐了", "恶心", "恶俗", "膈应", "嫌弃", "油腻"],
    "love": ["喜欢", "爱", "心动", "喜欢得", "超爱", "可爱", "想亲"],
    "shy": ["害羞", "不好意思", "脸红", "羞", "尴尬笑", "好难为情"],
    "confused": ["什么鬼", "懵", "迷惑", "啥子", "一头雾水", "搞不懂"],
    "tired": ["累", "困", "疲惫", "想睡", "撑不住", "虚脱", "倦"],
    "speechless": ["无语", "无话可说", "懒得说", "麻了", "沉默", "呵呵"],
    "troll": ["阴阳", "嘲讽", "激将", "挑事", "拱火", "带节奏"],
    "excited": ["期待", "兴奋", "哇", "冲鸭", "冲冲", "蹦起来", "超激动"],
    "thank": ["谢谢", "感谢", "辛苦了", "麻烦你了", "多谢"],
    "dumb": ["草", "傻", "蠢", "憨", "呆", "宕机", "裂开"],
    "cry": ["爆哭", "哇哇哭", "泪崩", "绷不住了", "破防"],
    "sigh": ["叹气", "唉声叹气", "算了算了", "就这样吧"],
}


def detect_emotion(text: str) -> Optional[tuple[str, float]]:
    """返回 (canonical 情绪, 强度0..1)；无匹配则 None。忽略否定反转的命中。"""
    value = str(text or "").strip()
    if not value:
        return None
    scores: dict[str, float] = {}
    for emo, phrases in _EMOTION_PHRASES.items():
        s = 0.0
        for ph in phrases:
            if ph in value and not _has_negation_prefix(value, ph):
                # 短语长则权重更高；多词短语更能体现主导情绪
                s += 1.0 + min(0.5, len(ph) * 0.1)
        if s:
            scores[emo] = s
    if not scores:
        return None
    best = max(scores.items(), key=lambda kv: kv[1])
    total = sum(scores.values())
    # 强度 = 该情绪占比；单极强情绪趋近 1
    intensity = min(1.0, round(best[1] / total if total else 1.0, 2))
    return best[0], intensity


def detect_emotion_name(text: str) -> Optional[str]:
    """只返回 canonical 情绪名，便于直接喂给 pick_by_emotion。"""
    r = detect_emotion(text)
    return r[0] if r else None


def dominant_terms(text: str, limit: int = 3) -> list[str]:
    """返回主导情绪的命中词（供标签回填/VLM 提示复用）。"""
    if not text:
        return []
    hits: list[tuple[str, str]] = []
    for emo, phrases in _EMOTION_PHRASES.items():
        for ph in phrases:
            if ph in text and not _has_negation_prefix(text, ph):
                hits.append((emo, ph))
    if not hits:
        return []
    # 按情绪簇聚合，取命中短语最多/最长的 top 簇词
    by_emo: dict[str, list[str]] = {}
    for emo, ph in hits:
        by_emo.setdefault(emo, []).append(ph)
    top_emo = max(by_emo, key=lambda e: len(by_emo[e]))
    return by_emo[top_emo][:limit]