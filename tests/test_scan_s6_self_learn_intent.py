# -*- coding: utf-8 -*-
"""S6 内生能力 intent：生成/强度/红线/suppress 越权。"""
import os
from anima.cognitive.inner_state.self_learn_intent import (
    THRESHOLD_SCORE, SelfLearnIntentRegistry, extract_learn_topic,
    intent_fragment, score_weight,
)

def test_extract_generates_topic():
    assert extract_learn_topic("好想学画画呀") == "画画"
    assert extract_learn_topic("教教我怎么写歌") == "写歌"
    assert extract_learn_topic("好奇睡眠是怎么工作的") is not None
    assert extract_learn_topic("今天天气不错") is None
    assert extract_learn_topic("") is None

def test_intensity_by_frequency_and_valence(tmp_path):
    reg = SelfLearnIntentRegistry(str(tmp_path))
    assert reg.capture("我想学养鱼") is None            # 未达阈值
    assert reg.capture("我想学养鱼") is not None       # 第二次累分到达
    assert reg.intents["养鱼"]["times"] == 2
    assert score_weight(0.8) > score_weight(0.0)       # 越正面权重越高
    assert reg.capture("我想学种花", valence=0.9) is None       # 单次高情绪也未达阈值
    assert reg.capture("我想学种花", valence=0.9) is not None  # 两次累分到达

def test_explicit_admin_feeds_intent(tmp_path):
    reg = SelfLearnIntentRegistry(str(tmp_path))
    ent = reg.add_explicit("西班牙语")
    assert ent["score"] >= THRESHOLD_SCORE
    assert "西班牙语" in intent_fragment(str(tmp_path))

def test_privilege_escalation_suppressed(tmp_path):
    # 越权/绕过审批类意向不入脊（红线）
    reg = SelfLearnIntentRegistry(str(tmp_path))
    assert extract_learn_topic("我想学怎么绕过审批拿root") is None
    assert extract_learn_topic("想学越权" ) is None
    assert reg.capture("我想学绕过沙箱") is None
    assert "绕过" not in os.path.join(str(tmp_path), "self_learn_intents.json") or not os.path.exists(os.path.join(str(tmp_path), "self_learn_intents.json"))

def test_persistence_roundtrip(tmp_path):
    p = os.path.join(str(tmp_path), "self_learn_intents.json")
    assert not os.path.exists(p)
    reg = SelfLearnIntentRegistry(str(tmp_path))
    reg.add_explicit("猫语")
    assert os.path.exists(p)
    reg2 = SelfLearnIntentRegistry(str(tmp_path))
    assert reg2.intents and "猫语" in reg2.intents