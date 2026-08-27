# -*- coding: utf-8 -*-
"""Anima 内在状态唯一合成点（S1，蓝图 2026-08-27 / T2）。

把散落的真实内在状态（mood / relation [后续：scar/desire/learning]）
读出来、拼成一句第一人称、可承接、绝不被裁的「此刻底色」。

- J1 合成路径：规则拼底（确定性、可单测、永不空）为基础；LLM 润色为可选增强，
  LLM 失败一律落回规则底句（fail-closed）——本切片先落规则底与接线。
- J2 载体：第一人称 + 显式边界（你的内心此刻是…承接为自己的心声）。
- S1 分层约束：每轮必注入但一句话、低强度语气底色；禁止大段刷屏。
各出口（认知注入/被问自述/主动/Q空间）都应 import 本模块，不得各自拼装。
"""
from __future__ import annotations

from typing import Optional


def mood_sentence(valence: float, arousal: float, label: str = "") -> str:
    v = max(-1.0, min(1.0, float(valence or 0.0)))
    a = max(0.0, min(1.0, float(arousal if arousal is not None else 0.3)))
    if v >= 0.5 and a >= 0.55:
        return "我现在心情很好，整个人有点雀跃，忍不住想找点乐子"
    if v >= 0.5:
        return "现在我挺开心的，心里暖暖的、很安定"
    if v >= 0.15:
        return "现在心情还不错，淡淡的舒服"
    if v <= -0.4 and a < 0.45:
        return "……有点蔫蔫的，没什么劲，过会儿应该会好一些"
    if v <= -0.4:
        return "心里有点堵得慌，像被人挠了逆鳞，暂时不太想说话"
    if v <= -0.1:
        return "心情有一点点沉，说不上来为什么，就是有点闷"
    return "现在还算平静，安安稳稳的"


_REL_CLAUSE = {
    "STRANGER": "这个人对我来说还蛮陌生的",
    "ACQUAINTANCE": "跟这个人是点头之交的熟悉感",
    "FRIEND": "和这个人处得挺熟络，说话可以放松些",
    "CLOSE": "这个人在我心里很亲近，我可以更放松撒娇一点",
}


def relation_clause(stage: str = "") -> str:
    key = str(stage or "").upper().strip()
    return _REL_CLAUSE.get(key, "")


def current_state_line(valence: float, arousal: float, label: str = "",
                       stage: str = "") -> str:
    parts = [mood_sentence(valence, arousal, label)]
    rel = relation_clause(stage)
    if rel:
        parts.append(rel)
    return "；".join(parts) + "。"


_HEAD = "（你的内心此刻是："
# FIX-2（2026-08-28）：口语化硬约束——主动避开 filter_inner_monologue 的字面旁白误伤，
# 强调「真猫直接说、不括号旁白、不自言自语」。所有 S 系片段尾句统一携带。
_SPOKEN_CONSTRAINT = "用平时聊天的语气直接说，不要加括号、不要自言自语，像对朋友随口自然讲的那样。"
_TAIL = "——把它当作你自己的心声自然流露即可，不要复述本段；" + _SPOKEN_CONSTRAINT + "）"


def wrap_boundary(sentence: str) -> str:
    sentence = (sentence or "").strip()
    if not sentence:
        return ""
    return _HEAD + sentence + _TAIL


def build_current_state_fragment(data_dir, user_id=None,
                                 relation_stage: str = "",
                                 trust=None) -> Optional[str]:
    """读真实状态源合成 protected 底色片段内容（含边界包装）。只读投影，不写库。"""
    valence, arousal, label = 0.0, 0.3, ""
    try:
        from anima.cognitive.mood import get_mood_system
        st = get_mood_system(data_dir).get_state()
        valence, arousal, label = st.valence, st.arousal, getattr(st, "label", "") or ""
    except Exception:
        pass
    stage = relation_stage
    if not stage and user_id is not None:
        try:
            from anima.cognitive.relation import get_relation_engine
            rel = get_relation_engine(data_dir).get(int(user_id))
            stage = getattr(getattr(rel, "stage", None), "name", "") or ""
        except Exception:
            stage = ""
    return wrap_boundary(current_state_line(valence, arousal, label, stage=stage)) or None
from anima.cognitive.inner_state.self_tell import (  # noqa: E402,F401
    build_self_narrative_fragment,
    detect_self_inquiry,
    narrative_line,
    self_narrative_snapshot,
)


def decide_injection(data_dir, text, user_id=None):
    """S1/S2 分流唯一入口（J2 大小关系）：被问自述压掉此刻底色。

    返回 (mode, fragment)：mode in {"self_tell", "baseline"}；
    自述不可得时安全落回底色(fail-closed)，两者都不会是 None 之外还抛错。
    """
    try:
        from anima.cognitive.inner_state.self_tell import (
            build_self_narrative_fragment,
            detect_self_inquiry,
        )
        if detect_self_inquiry(text or ""):
            frag = build_self_narrative_fragment(data_dir, user_id)
            if frag:
                return "self_tell", frag
    except Exception:
        pass
    return "baseline", build_current_state_fragment(data_dir, user_id=user_id)


def decide_reactive_injection(data_dir, text, user_id=None, rng=None):
    """L1/L3 被问分流协调（2026-08-28 点亮）：梦 > 秘密 > 自述 > 意外交心 > 底色。

    返回 (key, fragment, mode)：
      key     = 注入的系统片段键（inner_state_dream / inner_state_secret /
                inner_state_self_tell / inner_state）
      mode    = dream / secret / self_tell / spontaneous / baseline
    同轮只注入一段（优先级保证互斥），守 inner_state 唯一合成点。
    任一步失败回落底色(fail-closed)，不抛错。
    """
    txt = text or ""
    uid = int(user_id) if user_id is not None else None
    # 1) 被问梦（S2A，亲疏分层）
    try:
        from anima.cognitive.inner_state.dream_tell import (
            build_dream_tell_fragment,
            detect_dream_inquiry,
        )
        if detect_dream_inquiry(txt):
            frag = build_dream_tell_fragment(data_dir, uid, txt)
            if frag:
                return "inner_state_dream", frag, "dream"
    except Exception:
        pass
    # 2) 被问秘密（S7，深关系门槛 + 只无归属轻微）
    try:
        from anima.cognitive.inner_state.secret_feel import build_secret_feel_fragment
        frag = build_secret_feel_fragment(data_dir, uid, txt)
        if frag:
            return "inner_state_secret", frag, "secret"
    except Exception:
        pass
    # 3) 被问自述（S2）压底色 / 否则底色（S1）——沿用已部署 decide_injection
    mode, frag = decide_injection(data_dir, txt, user_id=uid)
    if mode == "self_tell" and frag:
        return "inner_state_self_tell", frag, "self_tell"
    # 4) 未被问 → 意外交心偶发（L1）；不中则 L4 会想偶发（脊轻读）；再落底色
    try:
        from anima.cognitive.inner_state.secret_feel import maybe_spontaneous_confide
        _spont = maybe_spontaneous_confide(data_dir, uid, rng=rng)
        if _spont:
            return "inner_state_secret", _spont, "spontaneous"
    except Exception:
        pass
    try:
        from anima.cognitive.inner_state.self_tell import occasional_insight_fragment
        _insight = occasional_insight_fragment(data_dir, uid, rng=rng)
        if _insight:
            return "inner_state_insight", _insight, "insight"
    except Exception:
        pass
    # 5a) #2+#3 情绪连续性 + 遗忘可感知（2026-08-28 路P+甲 同族）：低频偶带
    #     「最近提不起劲/挺活跃」或「有件事有点淡了」；经脊、fail-closed。
    try:
        from anima.cognitive.inner_state.emotional_continuity import (
            continuity_fragment,
            fading_fragment,
        )
        _cont = continuity_fragment(data_dir, uid, rng=rng)
        if _cont:
            return "inner_state_continuity", _cont, "continuity"
        _fade = fading_fragment(data_dir, uid, rng=rng)
        if _fade:
            return "inner_state_fading", _fade, "fading"
    except Exception:
        pass
    # 5) L6 乙层挂载（owner 拍板 2026-08-28「挂」）：底色落定后，深关系 + 偶发
    #    经脊带出「还惦记着别人」一角；低频受 rng 控制，不每轮塞，fail-closed。
    try:
        from anima.cognitive.inner_state.relation_overview import people_corner
        import random as _random_l6
        _rng_l6 = rng or _random_l6
        if frag and _rng_l6.random() < 0.05:
            corner = people_corner(data_dir, uid)
            if corner:
                frag = frag + corner
    except Exception:
        pass
    return "inner_state", frag, "baseline"