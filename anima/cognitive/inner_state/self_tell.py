# -*- coding: utf-8 -*-
"""S2 会自述的猫（蓝图 2026-08-27）：被问才掀帘，真状态为底 + 有戏派调和。

- 快照 self_narrative_snapshot：只读投影 mood/relation/scar/desire，可单测。
- 翻译+调和 narrative_line：J5 三分寸——越低落越收着说；关系不直白点破、
  用行为体现；一个念头长度，拒绝成段独白。
- detect_self_inquiry：J4 窄词表——明确指它自己心情/看法 + 对象自指（论你），
  寒暄调侃（如「你还知道心情啊」）不算。
"""
from __future__ import annotations

import re
from typing import Optional


def self_narrative_snapshot(data_dir: str, user_id: Optional[int] = None) -> dict:
    """只读快照：各源尽力读取、失败给中性默认值，绝不抛错。"""
    snap = {"valence": 0.0, "arousal": 0.3, "label": "",
            "stage": "", "trust": None, "interactions": 0,
            "scars": 0, "top_desire": "", "top_intense_desire": "",
            "latest_rel_note": ""}
    try:
        from anima.cognitive.mood import get_mood_system
        st = get_mood_system(data_dir).get_state()
        snap["valence"] = float(st.valence)
        snap["arousal"] = float(st.arousal)
        snap["label"] = str(getattr(st, "label", "") or "")
    except Exception:
        pass
    if user_id is not None:
        try:
            from anima.cognitive.relation import get_relation_engine
            rel = get_relation_engine(data_dir).get(int(user_id))
            snap["stage"] = getattr(getattr(rel, "stage", None), "name", "") or ""
            snap["trust"] = getattr(rel, "trust", None)
            snap["interactions"] = int(getattr(rel, "interaction_count", 0) or 0)
        except Exception:
            pass
        try:
            from anima.cognitive.scar import get_scar_system
            scars = get_scar_system(data_dir).get_for_user(int(user_id))
            snap["scars"] = len(scars or [])
        except Exception:
            pass
    try:
        from anima.cognitive.desire import get_desire_system
        ds = get_desire_system(data_dir)
        asp = getattr(ds, "aspirations", []) or []
        if asp:
            snap["top_desire"] = str(getattr(asp[0], "content", "") or "")[:24]
        # L4：top 高强度欲望（非首个，而是 intensity 最高、未 resolved 者）
        desires = getattr(ds, "desires", None) or []
        if desires:
            active = [d for d in desires if not getattr(d, "resolved", False)]
            if active:
                top = max(active, key=lambda d: float(getattr(d, "intensity", 0.0) or 0.0))
                if float(getattr(top, "intensity", 0.0) or 0.0) >= 0.6:
                    snap["top_intense_desire"] = str(getattr(top, "content", "") or "")[:24]
    except Exception:
        pass
    if user_id is not None:
        # L4：最新关系观察（rel.notes 末尾一条，取最后一行）
        try:
            from anima.cognitive.relation import get_relation_engine
            rel = get_relation_engine(data_dir).get(int(user_id))
            notes = str(getattr(rel, "notes", "") or "").strip()
            if notes:
                last = notes.splitlines()[-1].strip()
                if last:
                    snap["latest_rel_note"] = last[:60]
        except Exception:
            pass
    return snap


def _mood_clause(valence: float, arousal: float) -> str:
    """J5-1：情绪强度——低落越深越收着说。"""
    v = max(-1.0, min(1.0, valence))
    a = max(0.0, min(1.0, arousal))
    if v <= -0.55:
        return "……唔，还行吧"
    if v <= -0.25:
        return "有点蔫蔫的……不过没事啦，过会儿就好"
    if v >= 0.45 and a >= 0.5:
        return "挺开心的，现在整个人精神头很足"
    if v >= 0.3:
        return "蛮开心的，心里软软的"
    if a >= 0.6:
        return "还算平静，就是有点闲不住"
    return "就……平静的，没什么波澜"


def _relation_clause(stage: str) -> str:
    """J5-2：关系不直白——用行为/话多体现，热络才轻点。"""
    s = str(stage or "").upper()
    if s == "CLOSE":
        return "跟你说话会比平时多很多，这点你大概早就发现了"
    if s == "FRIEND":
        return "和你聊天挺自在的"
    if s == "ACQUAINTANCE":
        return "还在慢慢熟悉你的节奏"
    return ""


def narrative_line(snapshot: dict) -> str:
    """合成被问自述的一句（可含一个小转折），总量收在一两个短句内。"""
    parts = [_mood_clause(float(snapshot.get("valence", 0.0)),
                          float(snapshot.get("arousal", 0.3)))]
    rel = _relation_clause(str(snapshot.get("stage", "")))
    if rel:
        parts.append(rel)
    line = "；".join(parts)
    desire = str(snapshot.get("top_desire", "") or "").strip()
    if desire and len(line) < 60:
        line += "，最近老想着「" + desire + "」这点事"
    return line[:120]


_HEAD_S2 = "（你被问到你自己的心情/想法了。如实以真状态回答——此刻是："
_TAIL_S2 = "用你自己的口吻自然说出来即可。）"


def _spoken_tail() -> str:
    """FIX-2：延迟取共享口语约束（避免 self_tell↔__init__ 循环 import）。"""
    try:
        from anima.cognitive.inner_state import _SPOKEN_CONSTRAINT
        return "用你自己的口吻自然说出来即可；" + _SPOKEN_CONSTRAINT + "）"
    except Exception:
        return _TAIL_S2


def build_self_narrative_fragment(data_dir: str, user_id: Optional[int] = None):
    snap = self_narrative_snapshot(data_dir, user_id)
    line = narrative_line(snap)
    if not line:
        return None
    return _HEAD_S2 + line + _spoken_tail()


_POS_PATTERNS = [
    re.compile(r"[你您][^。？！]{0,8}(现在|最近|今天)?[^。？！]{0,4}(心情|情绪|感受|状态|心里)"),
    re.compile(r"心情\s*(咋样|怎么样|如何)"),
    re.compile(r"你怎么?(看|想|评价)[^。？]{0,8}我"),
    re.compile(r"(你的|对你)(真实 )?(看法|想法)|你怎么看我"),
    re.compile(r"你在(想|琢磨|寻思)(什么|啥)"),
    re.compile(r"(喜欢|讨厌)我吗|(对我)[^。？]{0,2}什么(感觉|看法)"),
]
_NEG_GUARDS = [
    re.compile(r"你还(知道|记得|懂)(我)?(有)?(心情|情绪)?"),
    re.compile(r"[她它]的心情|他们的心情"),
]


def detect_self_inquiry(text: str) -> bool:
    t = (text or "").strip()
    if not t or len(t) > 60:
        return False
    for pat in _NEG_GUARDS:
        if pat.search(t):
            return False
    if not re.search(r"[你您]|自己|本人", t):
        return False
    return any(pat.search(t) for pat in _POS_PATTERNS)


# ---------------------------------------------------------------------------
# L4 danger「会想的」→ 脊轻读（2026-08-28 点亮）：偶发自然带出 top 高强度欲望 /
# 最新关系观察，默认不每轮塞。
# ---------------------------------------------------------------------------
_INSIGHT_PROB = 0.03   # 偶发概率（低频，避免刷屏）


def occasional_insight_fragment(data_dir, user_id=None, rng=None):
    """脊轻读：存在 top 高强度欲望或最新关系观察时，偶发带出一句；否则 None(fail-closed)。"""
    import random
    rng = rng or random
    if rng.random() >= _INSIGHT_PROB:
        return None
    snap = self_narrative_snapshot(data_dir, user_id)
    desire = (snap.get("top_intense_desire") or "").strip()
    note = (snap.get("latest_rel_note") or "").strip()
    if not desire and not note:
        return None
    bits = []
    if desire:
        bits.append("最近老想着「" + desire + "」这点事")
    if note:
        bits.append("对和你相处的方式，心里有了一点新想法：" + note)
    if not bits:
        return None
    return "（你心里有件事没说出来，只是偶尔流露一下：" + "；".join(bits) + "。）"