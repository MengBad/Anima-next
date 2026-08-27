# -*- coding: utf-8 -*-
"""S1 内在状态可感知蓝图：protected 必注入信道 + inner_state 唯一合成点。"""
import asyncio
import io

from anima.pipeline.context import ContextComposer, PromptFragment
from anima.cognitive.inner_state import (
    build_current_state_fragment,
    current_state_line,
    mood_sentence,
    relation_clause,
    wrap_boundary,
)


def test_protected_fragment_never_dropped_under_tiny_budget():
    composer = ContextComposer(system_token_budget=30)   # 实际钳到下限 1024
    frags = [
        PromptFragment(key="mood", content="普通情绪片段" * 500, priority=90),
        PromptFragment(key="inner_state", content="此刻底色一句话", priority=55,
                       protected=True),
    ]
    content, stats = composer.compose_system("BASE", frags, "SUFFIX")
    assert "inner_state" not in stats.dropped_keys
    assert "此刻底色一句话" in content
    assert "mood" in stats.dropped_keys            # 非 protected 照常被裁


def test_rule_sentences_deterministic_and_bounded():
    assert mood_sentence(0.8, 0.7) != ""
    assert mood_sentence(-0.6, 0.2) != mood_sentence(0.8, 0.7)
    assert mood_sentence(999, -5) == mood_sentence(1.0, 0.0)  # 越界钳制
    assert relation_clause("") == "" and relation_clause("weird") == ""
    assert relation_clause("close") == relation_clause("CLOSE")
    line = current_state_line(0.8, 0.7, stage="FRIEND")
    assert line.endswith("。") and len(line) < 80    # 一句话量感，不刷屏
    assert line != current_state_line(-0.9, 0.2)


def test_boundary_wrap_first_person():
    w = wrap_boundary("现在还算平静，安安稳稳的。")
    assert w.startswith("（你的内心此刻是：") and w.endswith("）")
    assert "不要复述本段" in w
    assert wrap_boundary("") == ""


def test_build_fragment_reads_real_sources(tmp_path):
    frag = build_current_state_fragment(str(tmp_path), user_id=42,
                                        relation_stage="CLOSE")
    assert frag and "你的内心此刻是" in frag      # 永不为空（fail-closed 底色）
    close = build_current_state_fragment(str(tmp_path), relation_stage="CLOSE")
    stranger = build_current_state_fragment(str(tmp_path), relation_stage="STRANGER")
    assert close != stranger                     # 关系阶段进入底色


def test_cognition_wired_to_inner_state():
    """契约：认知阶段必须经唯一合成点注入，不得各自拼装。"""
    src = io.open("anima/pipeline/stages/cognition.py", encoding="utf-8").read()
    # L1/L2/L3 落地后接线升级为 decide_reactive_injection 分流（梦>秘密>自述>意外交心>底色）
    assert "from anima.cognitive.inner_state import decide_reactive_injection" in src
    assert 'add_system_fragment(_is_key, _is_frag, 55, protected=True)' in src
    assert "protected=True" in src
