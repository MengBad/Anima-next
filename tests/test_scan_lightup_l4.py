# -*- coding: utf-8 -*-
"""L4 danger 会想→脊轻读：释放两个/锁死三个/snapshot扩展/偶发带出。"""
from types import SimpleNamespace

from anima.cognitive.inner_state.self_tell import (
    occasional_insight_fragment,
    self_narrative_snapshot,
)


def test_danger_config_defaults():
    from anima.core.config import DangerConfig
    c = DangerConfig()
    assert c.active_info_collection is True        # 释放「会想」
    assert c.relationship_inference is True        # 释放「会想」
    # 锁死三个「会改/会发声」仍默认关 + confirm 关
    assert c.stance_propagation is False
    assert c.memory_infection is False
    assert c.core_mutation is False
    assert c.stance_propagation_confirm is False
    assert c.memory_infection_confirm is False
    assert c.core_mutation_confirm is False
    assert c.info_collection_can_speak is False   # 会想不带发声


def test_snapshot_reads_intense_desire_and_rel_note(tmp_path, monkeypatch):
    import anima.cognitive.inner_state.self_tell as st
    # stub mood/relation/desire 返回真实形状
    mood = SimpleNamespace(get_state=lambda: SimpleNamespace(valence=0.4, arousal=0.5, label="ok"))
    rel = SimpleNamespace(stage=SimpleNamespace(name="FRIEND"), trust=0.7,
                          interaction_count=10, notes="\n最近觉得和你聊得来")
    eng = SimpleNamespace(get=lambda uid: rel)
    des = SimpleNamespace(desires=[SimpleNamespace(content="想学烘焙", intensity=0.8, resolved=False)],
                          aspirations=[SimpleNamespace(content="第一asp")])
    import anima.cognitive.mood as m
    import anima.cognitive.relation as rr
    import anima.cognitive.desire as dd
    monkeypatch.setattr(m, "get_mood_system", lambda d: mood)
    monkeypatch.setattr(rr, "get_relation_engine", lambda d: eng)
    monkeypatch.setattr(dd, "get_desire_system", lambda d: des)
    snap = self_narrative_snapshot(str(tmp_path), 7)
    assert snap["top_intense_desire"] == "想学烘焙"
    assert "聊得来" in snap["latest_rel_note"]


def test_occasional_insight_fail_closed_and_prob(tmp_path, monkeypatch):
    import anima.cognitive.inner_state.self_tell as st
    monkeypatch.setattr(st, "self_narrative_snapshot",
                        lambda dd, uid: {"top_intense_desire": "想学烘焙",
                                        "latest_rel_note": ""})
    class _Rng:
        def __init__(self, v):
            self.v = v
        def random(self):
            return self.v
    assert occasional_insight_fragment(str(tmp_path), 7, rng=_Rng(0.99)) is None  # 不中概率
    frag = occasional_insight_fragment(str(tmp_path), 7, rng=_Rng(0.0))
    assert frag and "想学烘焙" in frag
    # 无欲望无关系观察 → 安静
    monkeypatch.setattr(st, "self_narrative_snapshot",
                        lambda dd, uid: {"top_intense_desire": "", "latest_rel_note": ""})
    assert occasional_insight_fragment(str(tmp_path), 7, rng=_Rng(0.0)) is None