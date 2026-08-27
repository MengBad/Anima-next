# -*- coding: utf-8 -*-
"""#7 霸凌反应链统一：severity 入沉默判据 + 单点分级 + playful 抵消。"""
import io

from anima.pipeline.stages.generation import _resolve_bullying_reply
from anima.cognitive.inner_state.bullying_feel import classify
from anima.pipeline.stages._shared import _detect_hurtful


def test_unified_mute_criterion():
    # 一句重伤（severity>=3 但 recent<3）→ 完全沉默（返回 None）
    assert _resolve_bullying_reply(recent_count=1, severity=4) is None
    # 连骂 >=3 → 沉默
    assert _resolve_bullying_reply(recent_count=3, severity=0) is None
    # 轻（recent<3 且 severity<3）→ 冷回（非 None）
    assert _resolve_bullying_reply(recent_count=1, severity=2) is not None
    # 回避场景（severity=0 默认）→ 冷回
    assert _resolve_bullying_reply(recent_count=0) is not None


def test_classify_unified_level():
    assert classify(4, 1) == "severe"      # 一句重伤 → 重
    assert classify(2, 3) == "severe"      # 连续打击 → 重
    assert classify(1, 1) == "mild"
    assert classify(0, 0) == "none"


def test_playful_offset_suppresses_hurt():
    # 有戏派玩笑（playful 抵消）→ 不算伤害
    assert _detect_hurtful("滚，别烦本喵，略略略") is False
    # 真辱骂（无 playful 抵消）→ 算伤害
    assert _detect_hurtful("你这个傻逼垃圾") is True


def test_handler_single_point_classify_and_playful():
    src = io.open("anima/llm/handler.py", encoding="utf-8").read()
    assert "bullying_level" in src              # 统一分级字段
    assert "_detect_hurtful" in src             # playful 抵消单源复用
    assert "classify as _bully_classify" in src  # 单点分级


def test_generation_reads_unified_criterion():
    src = io.open("anima/pipeline/stages/generation.py", encoding="utf-8").read()
    assert "severity >= 3" in src
    assert "or _bully_sev >= 3" in src
