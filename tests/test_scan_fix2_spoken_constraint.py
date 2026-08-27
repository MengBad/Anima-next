# -*- coding: utf-8 -*-
"""FIX-2 口语化约束测试。"""
import io

from anima.cognitive.inner_state import (
    _SPOKEN_CONSTRAINT,
    _TAIL,
    build_current_state_fragment,
    wrap_boundary,
)


def test_tail_carries_spoken_constraint():
    assert "不要加括号" in _SPOKEN_CONSTRAINT
    assert "不要自言自语" in _SPOKEN_CONSTRAINT
    assert _SPOKEN_CONSTRAINT in _TAIL


def test_wrap_boundary_carries_constraint():
    frag = wrap_boundary("心情不错")
    assert frag and "不要加括号" in frag and "不要自言自语" in frag


def test_s1_fragment_carries_constraint(tmp_path):
    frag = build_current_state_fragment(str(tmp_path), user_id=7)
    assert frag and "不要加括号" in frag


def test_s2_fragment_carries_constraint(tmp_path):
    from anima.cognitive.inner_state.self_tell import build_self_narrative_fragment
    frag = build_self_narrative_fragment(str(tmp_path), user_id=7)
    if frag:
        assert "不要加括号" in frag


def test_filter_inner_monologue_untouched():
    src = io.open("anima/core/reply_splitter.py", encoding="utf-8").read()
    assert "def filter_inner_monologue" in src
    assert "_MONOLOGUE_RATIO_THRESHOLD" in src
    assert "宁少勿错" in src or "绝不误伤" in src
