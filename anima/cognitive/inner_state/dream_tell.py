# -*- coding: utf-8 -*-
"""S2A 梦境感知（蓝图 2026-08-27 选A）：会做梦的猫——梦接进可感知出口。

已收敛判断：
- 亲近的人问「昨晚做了啥梦」→ 讲最近梦+感悟（可荒诞）；亲近度门槛复用
  relation 深度（FRIEND/CLOSE 才算「亲近的人」）。
- 梦 mood_tag 另接 S1 mood/情绪底色（mood_tag_to_emotion 映射 →
  MoodSystem 起始情绪微调），不只调 soul 能量。
"""
from __future__ import annotations
import logging
from typing import Optional

logger = logging.getLogger("anima.inner_state.dream_tell")

_CLOSE_STAGES = {"FRIEND", "CLOSE"}

# 梦情绪 → mood 微调（valence, arousal）；S1 底色由此自然带出
_DREAM_MOOD_DELTA = {
    "happy":   (0.18, 0.08),
    "excited": (0.12, 0.15),
    "calm":    (0.05, 0.02),
    "weird":   (0.02, 0.06),
    "anxious": (-0.10, 0.10),
    "sad":     (-0.18, -0.05),
}

_DREAM_ASK_RE = __import__("re").compile(
    r"(你|它)(昨天|昨晚|昨夜|最近)?(做|梦)[^。？]{0,10}(什么梦|啥梦|什么|吗|没有)"
    r"|(昨天|昨晚|昨夜)梦到?(了)?(什么|啥)(梦)?(吗|没有)?")


def detect_dream_inquiry(text: str) -> bool:
    return bool(_DREAM_ASK_RE.search((text or "").strip()[:60]))


def relation_is_close(data_dir: str, user_id: int) -> bool:
    try:
        from anima.cognitive.relation import get_relation_engine
        rel = get_relation_engine(data_dir).get(int(user_id))
        stage = getattr(getattr(rel, "stage", None), "name", "") or ""
        return stage.upper() in _CLOSE_STAGES
    except Exception:
        return False


def get_recent_dream(data_dir: str) -> Optional[dict]:
    try:
        from anima.cognitive.dream_engine import DreamEngine
        return DreamEngine(data_dir).get_last_dream()
    except Exception:
        return None


def build_dream_tell_fragment(data_dir: str, user_id: int, text: str = "") -> Optional[str]:
    """L3 亲疏分层：被问梦 → 亲近(FRIEND/CLOSE)如实讲最近梦+感悟；路人含糊一句带过。

    真判定由调用方 detect_dream_inquiry(text) 完成（不再用占位 detect_dream_inquiry_text）。
    """
    dream = get_recent_dream(data_dir)
    if not dream:
        return None
    scene = str(dream.get("scene") or dream.get("content") or "").strip()
    insight = str(dream.get("insight") or "").strip()
    if not scene:
        return None
    if relation_is_close(data_dir, user_id):
        head = "（亲近的人问你昨晚做了什么梦。如实讲最近这场梦，可以有点荒诞，第一人称讲。）"
        tail = "讲完自然收住，别长篇大论。"
        parts = [f"梦里的画面：{scene[:180]}"]
        if insight:
            parts.append(f"醒来后的感觉：{insight[:80]}")
        return head + "；".join(parts) + tail
    # 路人被问 → 含糊一句带过，不展开
    return "（有人随口问你昨晚做了什么梦。你含糊地一句带过：）唔…梦见吃小鱼干，记不清啦。"


def apply_dream_mood(data_dir: str, dream: Optional[dict] = None) -> bool:
    """S2A：梦 mood_tag 接 S1 mood（不只调 soul 能量）。幂等由调用方节律保证。"""
    d = dream or get_recent_dream(data_dir)
    if not d:
        return False
    tag = str(d.get("mood_tag") or "").lower()
    from anima.cognitive.dream_engine import mood_tag_to_emotion
    emotion = mood_tag_to_emotion(tag)
    dv, da = _DREAM_MOOD_DELTA.get(emotion, (0.0, 0.0))
    if dv == 0.0 and da == 0.0:
        return False
    from anima.cognitive.mood import get_mood_system
    ms = get_mood_system(data_dir)
    st = ms.get_state()
    try:
        st.valence = max(-1.0, min(1.0, st.valence + dv))
        st.arousal = max(0.0, min(1.0, st.arousal + da))
    except Exception:
        pass
    try:
        ms.persist()  # 若有持久化入口则落盘，无则忽略
    except Exception:
        pass
    logger.info("梦情绪接底色: tag=%s emotion=%s dv=%.2f da=%.2f", tag, emotion, dv, da)
    return True