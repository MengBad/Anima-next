# -*- coding: utf-8 -*-
"""L5 霸凌分级接内在（2026-08-28 点亮 乙·分级）：bullying_detector 输出接 mood/relation/scar。

已定判断（乙·分级）：
- 轻度（单次、severity<3）：只 mood 温和低落（走既有衰减），不碰 relation、不记 scar。
- 严重/连续（severity>=3 或 recent_count>=3）：mood 掉 + relation trust 轻微降
  （≤一档、REL_FLOOR 保护）+ 记该用户「bullying」同类第一条 scar（add 会合并）。
- 界限：对施暴者的不悦，不触发报复/外泄。
"""
from __future__ import annotations
import logging

logger = logging.getLogger("anima.inner_state.bullying_feel")

REL_FLOOR = 0.2
TRUST_PENALTY = 0.05
SEVERE_SEVERITY = 3
SEVERE_RECENT = 3


def classify(severity: int, recent_count: int) -> str:
    """分级：mild / severe / none。纯函数可单测。"""
    if severity <= 0:
        return "none"
    if severity >= SEVERE_SEVERITY or recent_count >= SEVERE_RECENT:
        return "severe"
    return "mild"


def apply_bullying_signal(data_dir: str, user_id: int, severity: int,
                         recent_count: int) -> dict:
    """施加分级内在；返回 {level, mood, relation, scar} 供测试与审计。"""
    out = {"level": classify(severity, recent_count),
           "mood": False, "relation": False, "scar": False}
    if out["level"] == "none":
        return out
    try:
        from anima.cognitive.mood import get_mood_system
        get_mood_system(data_dir).trigger("ignored", 0.18)   # 温和低落
        out["mood"] = True
    except Exception:
        logger.debug("霸凌 mood 施加失败", exc_info=True)
    if out["level"] == "severe":
        try:
            from anima.cognitive.relation import get_relation_engine
            rel = get_relation_engine(data_dir).get(int(user_id))
            trust = float(getattr(rel, "trust", 0.5) or 0.5)
            rel.trust = max(REL_FLOOR, trust - TRUST_PENALTY)
            out["relation"] = True
        except Exception:
            logger.debug("霸凌 relation 施加失败", exc_info=True)
        try:
            from anima.cognitive.scar import get_scar_system
            get_scar_system(data_dir).add(int(user_id), "bullying", 0.5)
            out["scar"] = True
        except Exception:
            logger.debug("霸凌 scar 记录失败", exc_info=True)
    logger.info("霸凌分级: user=%s level=%s sev=%d recent=%d",
                user_id, out["level"], severity, recent_count)
    return out