# -*- coding: utf-8 -*-
"""L5 霸凌分级测试。"""
from types import SimpleNamespace

from anima.cognitive.inner_state.bullying_feel import (
    apply_bullying_signal,
    classify,
)


def test_classify():
    assert classify(0, 0) == "none"
    assert classify(1, 1) == "mild"
    assert classify(2, 2) == "mild"
    assert classify(3, 1) == "severe"
    assert classify(1, 3) == "severe"
    assert classify(5, 5) == "severe"


def test_mild_only_mood(tmp_path, monkeypatch):
    import anima.cognitive.mood as m
    import anima.cognitive.relation as rr
    import anima.cognitive.scar as sc
    fired = []
    monkeypatch.setattr(m, "get_mood_system",
                        lambda dd: SimpleNamespace(trigger=lambda ev, i: fired.append((ev, i))))
    rel = SimpleNamespace(trust=0.8)
    eng = SimpleNamespace(get=lambda uid: rel)
    monkeypatch.setattr(rr, "get_relation_engine", lambda dd: eng)
    monkeypatch.setattr(sc, "get_scar_system", lambda dd: SimpleNamespace(add=lambda *a, **k: None))
    out = apply_bullying_signal(str(tmp_path), 7, severity=1, recent_count=1)
    assert out["level"] == "mild"
    assert out["mood"] is True
    assert out["relation"] is False
    assert out["scar"] is False
    assert fired and fired[0][0] == "ignored"


def test_severe_touches_all_with_floor(tmp_path, monkeypatch):
    import anima.cognitive.mood as m
    import anima.cognitive.relation as rr
    import anima.cognitive.scar as sc
    monkeypatch.setattr(m, "get_mood_system",
                        lambda dd: SimpleNamespace(trigger=lambda ev, i: None))
    rel = SimpleNamespace(trust=0.21)
    eng = SimpleNamespace(get=lambda uid: rel)
    monkeypatch.setattr(rr, "get_relation_engine", lambda dd: eng)
    scars = []
    monkeypatch.setattr(sc, "get_scar_system",
                        lambda dd: SimpleNamespace(add=lambda u, e, s=0.5, **k: scars.append((u, e, s))))
    out = apply_bullying_signal(str(tmp_path), 7, severity=3, recent_count=1)
    assert out["level"] == "severe"
    assert out["mood"] is True and out["relation"] is True and out["scar"] is True
    assert rel.trust >= 0.2
    assert scars and scars[0][0] == 7 and scars[0][1] == "bullying"


def test_non_bullying_no_change(tmp_path, monkeypatch):
    import anima.cognitive.mood as m
    fired = []
    monkeypatch.setattr(m, "get_mood_system",
                        lambda dd: SimpleNamespace(trigger=lambda ev, i: fired.append(ev)))
    out = apply_bullying_signal(str(tmp_path), 7, severity=0, recent_count=0)
    assert out["level"] == "none" and out["mood"] is False
    assert out["relation"] is False and out["scar"] is False
    assert fired == []


def test_handler_wiring_contract():
    import io
    # 队列④第二刀：布线迁至 prefilter stage
    src = io.open("anima/pipeline/stages/prefilter.py", encoding="utf-8").read()
    assert "apply_bullying_signal" in src
    assert "L5 霸凌分级接内在" in src