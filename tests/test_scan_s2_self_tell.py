# -*- coding: utf-8 -*-
"""S2 会自述的猫：J4 窄词表识别 / J5 三分寸翻译 / S2 压掉 S1 分流 / 接线契约。"""
from anima.cognitive.inner_state import decide_injection
from anima.cognitive.inner_state.self_tell import (
    build_self_narrative_fragment,
    detect_self_inquiry,
    narrative_line,
    self_narrative_snapshot,
)


def test_j4_detect_positives():
    for t in ("你现在心情怎么样", "你最近有什么感受吗", "你怎么看我的新发型",
              "你在想什么呀", "你对我是什么感觉", "今天你心情如何"):
        assert detect_self_inquiry(t), t


def test_j4_detect_negatives():
    for t in ("你还知道心情啊，天天摆烂",  # 寒暄调侃
              "她的心情怎么样",            # 三人称非对象自指
              "我今天心情很好",            # 论自己不是论它
              "这个功能的状态怎么样",      # 无「你」的对象指涉误报项
              ""):
        assert not detect_self_inquiry(t), t


def test_j5_deep_sadness_is_reserved():
    line = narrative_line({"valence": -0.8, "arousal": 0.2, "stage": ""})
    assert line.startswith("……唔")          # 越低落越收着说
    assert len(line) <= 20                  # 极简不摊开


def test_j5_relation_via_behavior_not_label():
    close = narrative_line({"valence": 0.4, "arousal": 0.4, "stage": "CLOSE"})
    friend = narrative_line({"valence": 0.4, "arousal": 0.4, "stage": "FRIEND"})
    stranger = narrative_line({"valence": 0.4, "arousal": 0.4, "stage": "STRANGER"})
    assert close != friend != stranger
    assert "很喜欢" not in close            # 不直白点破感情，用行为体现


def test_j5_one_thought_length_cap():
    snap = {"valence": 0.6, "arousal": 0.6, "stage": "CLOSE",
            "top_desire": "一个特别特别长的愿望描述超过很多很多字也要被截断才行"}
    line = narrative_line(snap)
    assert len(line) <= 120                 # 一个念头长度，拒绝成段独白


def test_snapshot_fail_closed_and_fragment_shape(tmp_path):
    snap = self_narrative_snapshot(str(tmp_path), user_id=7)
    assert isinstance(snap, dict) and "valence" in snap
    frag = build_self_narrative_fragment(str(tmp_path), user_id=7)
    assert frag and "此刻是：" in frag       # 头边界在（诚实自述框架）


def test_decide_injection_modes(tmp_path):
    mode, frag = decide_injection(str(tmp_path), "你现在心情怎么样", user_id=7)
    assert mode == "self_tell" and frag and "此刻是：" in frag
    mode2, frag2 = decide_injection(str(tmp_path), "帮我查个天气", user_id=7)
    assert mode2 == "baseline" and frag2 and "内心此刻是" in frag2
    # 普通句与问询句拿到的是不同载体（S2 压掉 S1 底色）
    assert frag != frag2


def test_cognition_wired_to_decide_injection():
    import io
    src = io.open("anima/pipeline/stages/cognition.py", encoding="utf-8").read()
    assert "decide_injection" in src
    assert 'add_system_fragment(_is_key, _is_frag, 55, protected=True)' in src
    assert 'ctx.meta["inner_state_mode"]' in src