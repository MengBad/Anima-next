# -*- coding: utf-8 -*-
"""FIX-1 梦回灌统一测试。"""
import io
from types import SimpleNamespace


def test_single_table_sweep_both(tmp_path, monkeypatch):
    from anima.cognitive.dream_engine import DreamEngine, _DREAM_SWEEP
    import anima.cognitive.mood as mood_mod
    st = SimpleNamespace(valence=0.0, arousal=0.3)
    ms = SimpleNamespace(get_state=lambda: st)
    monkeypatch.setattr(mood_mod, "get_mood_system", lambda dd: ms)
    soul_state = SimpleNamespace(creativity=0.0, social_appetite=0.0,
                                 expression_desire=0.0, memory_depth=0.0)
    soul = SimpleNamespace(state=soul_state, save=lambda: None)
    eng = DreamEngine(str(tmp_path))
    assert eng.apply_dream_sweep("happy", soul=soul, mood=ms) is True
    assert st.valence > 0.0
    assert soul_state.social_appetite > 0.0
    assert _DREAM_SWEEP["happy"]["mood"][0] == 0.108


def test_autonomy_uses_sweep_not_old_entry():
    src = io.open("anima/core/autonomy.py", encoding="utf-8").read()
    assert "apply_dream_sweep" in src
    assert "apply_dream_mood_to_soul" not in src


def test_no_residual_double_head():
    dt = io.open("anima/cognitive/inner_state/dream_tell.py", encoding="utf-8").read()
    assert "_DREAM_MOOD_DELTA" not in dt
    assert "def apply_dream_mood" not in dt
    de = io.open("anima/cognitive/dream_engine.py", encoding="utf-8").read()
    assert "_DREAM_SWEEP" in de
    assert "def apply_dream_sweep" in de


def test_apply_dream_mood_to_soul_backward_compat():
    de = io.open("anima/cognitive/dream_engine.py", encoding="utf-8").read()
    assert "def apply_dream_mood_to_soul" in de
    assert "_DREAM_SWEEP" in de