# -*- coding: utf-8 -*-
"""L6 关系总览只读聚合：甲为主/乙为辅/只读不写/fail-closed。"""
import os
from types import SimpleNamespace

from anima.cognitive.inner_state.relation_overview import (
    current_relation_line,
    overview_fragment,
    people_corner,
)


def test_current_relation_line_fail_closed(tmp_path, monkeypatch):
    import anima.cognitive.relation as rr
    class _Rel:
        def __init__(self, stage):
            self.stage = SimpleNamespace(name=stage)
            self.trust = 0.7
    class _Eng:
        def __init__(self, s):
            self._s = s
        def get(self, uid):
            return _Rel(self._s)
    monkeypatch.setattr(rr, "get_relation_engine", lambda dd: _Eng("CLOSE"))
    line = current_relation_line(str(tmp_path), 7)
    assert line and "亲近" in line
    # 无用户 → 空串 fail-closed
    assert current_relation_line(str(tmp_path), None) == ""
    # 未知 stage → 空串
    monkeypatch.setattr(rr, "get_relation_engine", lambda dd: _Eng("WEIRD"))
    assert current_relation_line(str(tmp_path), 7) == ""


def test_people_corner_only_deep_relation(tmp_path, monkeypatch):
    import anima.cognitive.relation as rr
    import anima.cognitive.inner_state.relation_overview as ro
    class _Rel:
        def __init__(self, stage):
            self.stage = SimpleNamespace(name=stage)
            self.trust = 0.7
    class _Eng:
        def __init__(self, s):
            self._s = s
        def get(self, uid):
            return _Rel(self._s)
    monkeypatch.setattr(rr, "get_relation_engine", lambda dd: _Eng("STRANGER"))
    assert people_corner(str(tmp_path), 7) == ""   # 浅关系不窥他人
    monkeypatch.setattr(rr, "get_relation_engine", lambda dd: _Eng("CLOSE"))
    # stub user_relations 返回一条边
    fake_store = SimpleNamespace(query=lambda name: [{"a": "7", "b": "小明", "relation": "同学", "updated": 1.0}])
    import anima.crossgroup.memory.user_relations as ur
    monkeypatch.setattr(ur, "get_user_relation_store", lambda dd: fake_store)
    corner = people_corner(str(tmp_path), 7)
    assert corner and "小明" in corner and "同学" in corner


def test_overview_primary_only_for_stranger(tmp_path, monkeypatch):
    import anima.cognitive.relation as rr
    class _Rel:
        def __init__(self, stage):
            self.stage = SimpleNamespace(name=stage)
            self.trust = 0.7
    class _Eng:
        def __init__(self, s):
            self._s = s
        def get(self, uid):
            return _Rel(self._s)
    monkeypatch.setattr(rr, "get_relation_engine", lambda dd: _Eng("STRANGER"))
    frag = overview_fragment(str(tmp_path), 7, with_corner=True)
    assert frag and "陌生" in frag
    assert "惦记" not in frag   # STRANGER 只有甲，不带乙


def test_readonly_no_write(tmp_path, monkeypatch):
    import anima.cognitive.relation as rr
    class _Rel:
        def __init__(self, stage):
            self.stage = SimpleNamespace(name=stage)
            self.trust = 0.7
    class _Eng:
        def __init__(self, s):
            self._s = s
        def get(self, uid):
            return _Rel(self._s)
    monkeypatch.setattr(rr, "get_relation_engine", lambda dd: _Eng("CLOSE"))
    overview_fragment(str(tmp_path), 7, with_corner=True)
    # 调用后不产生任何存储文件（只读不写）
    for name in ("relation.json", "bot_relations.json", "user_relations.json",
                "social_graph.json"):
        assert not os.path.exists(os.path.join(str(tmp_path), name)), name


def test_decide_reactive_corner_hooked_deep_only(tmp_path, monkeypatch):
    """拍板2「挂」：深关系 + 偶发 rng → 底色带人际一角；浅关系不带。"""
    import anima.cognitive.inner_state as ii
    import anima.cognitive.relation as rr
    import anima.cognitive.inner_state.relation_overview as ro
    from types import SimpleNamespace
    class _Rel:
        def __init__(self, stage):
            self.stage = SimpleNamespace(name=stage)
            self.trust = 0.7
    class _Eng:
        def __init__(self, s):
            self._s = s
        def get(self, uid):
            return _Rel(self._s)
    fake_store = SimpleNamespace(query=lambda name: [{"a": "7", "b": "小明", "relation": "同学", "updated": 1.0}])
    import anima.crossgroup.memory.user_relations as ur
    monkeypatch.setattr(ur, "get_user_relation_store", lambda dd: fake_store)
    class _Rng:
        def __init__(self, v):
            self.v = v
        def random(self):
            return self.v
    # 深关系 + rng 命中 0.05 → 带「惦记」
    monkeypatch.setattr(rr, "get_relation_engine", lambda dd: _Eng("CLOSE"))
    key, frag, mode = ii.decide_reactive_injection(str(tmp_path), "今天天气不错", 7, rng=_Rng(0.0))
    assert mode == "baseline" and "惦记" in frag and "小明" in frag
    # 浅关系 → 不带（即使 rng 命中）
    monkeypatch.setattr(rr, "get_relation_engine", lambda dd: _Eng("STRANGER"))
    key2, frag2, mode2 = ii.decide_reactive_injection(str(tmp_path), "今天天气不错", 7, rng=_Rng(0.0))
    assert "惦记" not in frag2