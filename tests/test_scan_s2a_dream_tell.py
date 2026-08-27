# -*- coding: utf-8 -*-
"""S2A 梦境感知：问梦识别/亲近门槛/最近梦讲述/梦情绪接S1底色。"""
from types import SimpleNamespace

from anima.cognitive.inner_state.dream_tell import (
    build_dream_tell_fragment,
    detect_dream_inquiry,
    relation_is_close,
)


def test_detect_dream_inquiry():
    assert detect_dream_inquiry("你昨晚做了什么梦呀") is True
    assert detect_dream_inquiry("昨晚梦到什么了吗") is True
    assert detect_dream_inquiry("今天吃什么") is False
    assert detect_dream_inquiry("你昨晚干嘛去了") is False


def test_relation_close_gate(tmp_path, monkeypatch):
    import anima.cognitive.relation as rel_mod

    class _Rel:
        def __init__(self, stage):
            self.stage = SimpleNamespace(name=stage)

    class _Eng:
        def __init__(self, stage):
            self._s = stage

        def get(self, uid):
            return _Rel(self._s)

    monkeypatch.setattr(rel_mod, "get_relation_engine", lambda dd: _Eng("CLOSE"))
    assert relation_is_close(str(tmp_path), 1) is True
    monkeypatch.setattr(rel_mod, "get_relation_engine", lambda dd: _Eng("STRANGER"))
    assert relation_is_close(str(tmp_path), 1) is False


def test_build_fragment_requires_dream_and_close(tmp_path, monkeypatch):
    import anima.cognitive.inner_state.dream_tell as dt
    monkeypatch.setattr(dt, "relation_is_close", lambda dd, uid: False)
    assert build_dream_tell_fragment(str(tmp_path), 1) is None
    monkeypatch.setattr(dt, "relation_is_close", lambda dd, uid: True)
    assert build_dream_tell_fragment(str(tmp_path), 1) is None
    fake = {"scene": "梦里在一片会发光的鱼干海洋里游泳", "insight": "醒来有点饿",
            "mood_tag": "happy"}
    monkeypatch.setattr(dt, "get_recent_dream", lambda dd: fake)
    frag = build_dream_tell_fragment(str(tmp_path), 1)
    assert frag and "鱼干海洋" in frag and "醒来有点饿" in frag


def test_apply_dream_sweep_unifies_soul_and_mood(tmp_path, monkeypatch):
    """FIX-1：梦情绪回灌统一——同 mood_tag 一次调用同时动 soul 与 mood。"""
    import anima.cognitive.mood as mood_mod
    from anima.cognitive.dream_engine import DreamEngine
    st = SimpleNamespace(valence=0.0, arousal=0.3)
    ms = SimpleNamespace(get_state=lambda: st)
    monkeypatch.setattr(mood_mod, "get_mood_system", lambda dd: ms)
    eng = DreamEngine(str(tmp_path))
    # soul 注入（不依赖真实 soul.json）
    soul_state = SimpleNamespace(creativity=0.0, social_appetite=0.0,
                                 expression_desire=0.0, memory_depth=0.0)
    soul = SimpleNamespace(state=soul_state, save=lambda: None)
    before = st.valence
    ok = eng.apply_dream_sweep("sad", soul=soul, mood=ms)
    assert ok is True
    assert st.valence < before                       # mood 底色被压低
    assert soul_state.social_appetite < 0.0          # soul 能量也被动（sad 想独处）
    # happy → 抬高
    mid = st.valence
    ok2 = eng.apply_dream_sweep("happy", soul=soul, mood=ms)
    assert ok2 is True and st.valence > mid
    # 未知 tag → False（fail-closed）
    assert eng.apply_dream_sweep("nonsense", soul=soul, mood=ms) is False
