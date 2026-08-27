# -*- coding: utf-8 -*-
"""S8 矛盾感知测试。"""
from types import SimpleNamespace

from anima.cognitive.inner_state.contradiction_feel import (
    REL_FLOOR,
    TRUST_PENALTY,
    feel_user_contradiction,
)


def test_perturbation_is_light_and_bounded(tmp_path, monkeypatch):
    import anima.cognitive.relation as rel_mod

    class _Rel:
        def __init__(self):
            self.trust = 0.9

    class _Eng:
        def __init__(self, rel):
            self._rel = rel

        def get(self, uid):
            return self._rel

    rel = _Rel()
    monkeypatch.setattr(rel_mod, "get_relation_engine", lambda dd: _Eng(rel))
    assert feel_user_contradiction(str(tmp_path), 7) is True
    assert rel.trust == 0.9 - TRUST_PENALTY


def test_never_over_penalizes():
    assert 0.0 < TRUST_PENALTY < 0.1
    assert REL_FLOOR >= 0.0


def test_wiring_contract():
    import io
    src = io.open("anima/cognitive/contradiction.py", encoding="utf-8").read()
    assert "feel_user_contradiction" in src
    assert "S8 矛盾感知" in src
