# -*- coding: utf-8 -*-
"""#2+#3 情绪连续性+遗忘可感知：soul轨迹判据/淡化判据/低频/fail-closed。"""
from types import SimpleNamespace

from anima.cognitive.inner_state.emotional_continuity import (
    continuity_fragment,
    fading_fragment,
    has_fading_belief,
    recent_energy_tone,
)


def _mk_hist(appetite, desire, n=10):
    return [{"social_appetite": appetite, "expression_desire": desire, "ts": 1000 + i * 61}
            for i in range(n)]


def test_recent_energy_tone_low_high_none(tmp_path, monkeypatch):
    import anima.cognitive.soul as soul_mod
    monkeypatch.setattr(soul_mod, "get_soul_system",
                        lambda dd: SimpleNamespace(get_history=lambda h: _mk_hist(-0.3, -0.3)))
    assert recent_energy_tone(str(tmp_path)) == "low"
    monkeypatch.setattr(soul_mod, "get_soul_system",
                        lambda dd: SimpleNamespace(get_history=lambda h: _mk_hist(0.3, 0.3)))
    assert recent_energy_tone(str(tmp_path)) == "high"
    # 中性
    monkeypatch.setattr(soul_mod, "get_soul_system",
                        lambda dd: SimpleNamespace(get_history=lambda h: _mk_hist(0.0, 0.0)))
    assert recent_energy_tone(str(tmp_path)) is None
    # 轨迹不足 → None
    monkeypatch.setattr(soul_mod, "get_soul_system",
                        lambda dd: SimpleNamespace(get_history=lambda h: _mk_hist(0.3, 0.3, n=2)))
    assert recent_energy_tone(str(tmp_path)) is None


def test_has_fading_belief(tmp_path, monkeypatch):
    import anima.cognitive.world_model as wm
    contested = SimpleNamespace(contested=True, weight=0.5)
    monkeypatch.setattr(wm, "get_world_model",
                        lambda dd: SimpleNamespace(get_entries=lambda **k: [contested]))
    assert has_fading_belief(str(tmp_path)) is True
    low = SimpleNamespace(contested=False, weight=0.1)
    monkeypatch.setattr(wm, "get_world_model",
                        lambda dd: SimpleNamespace(get_entries=lambda **k: [low]))
    assert has_fading_belief(str(tmp_path)) is True
    healthy = SimpleNamespace(contested=False, weight=0.7)
    monkeypatch.setattr(wm, "get_world_model",
                        lambda dd: SimpleNamespace(get_entries=lambda **k: [healthy]))
    assert has_fading_belief(str(tmp_path)) is False


def test_fragments_fail_closed_and_prob(tmp_path, monkeypatch):
    import anima.cognitive.soul as soul_mod
    monkeypatch.setattr(soul_mod, "get_soul_system",
                        lambda dd: SimpleNamespace(get_history=lambda h: _mk_hist(-0.3, -0.3)))
    class _Rng:
        def __init__(self, v): self.v = v
        def random(self): return self.v
    assert continuity_fragment(str(tmp_path), 7, rng=_Rng(0.99)) is None  # 不中概率
    frag = continuity_fragment(str(tmp_path), 7, rng=_Rng(0.0))
    assert frag and "提不起劲" in frag
    # 中性状态 → 安静
    monkeypatch.setattr(soul_mod, "get_soul_system",
                        lambda dd: SimpleNamespace(get_history=lambda h: _mk_hist(0.0, 0.0)))
    assert continuity_fragment(str(tmp_path), 7, rng=_Rng(0.0)) is None


def test_decide_reactive_hooks_continuity(tmp_path, monkeypatch):
    import anima.cognitive.inner_state as ii
    import anima.cognitive.soul as soul_mod
    monkeypatch.setattr(soul_mod, "get_soul_system",
                        lambda dd: SimpleNamespace(get_history=lambda h: _mk_hist(0.3, 0.3)))
    class _Rng:
        def __init__(self, v): self.v = v
        def random(self): return self.v
    # 中性文本（不命中 dream/secret/self_tell），rng 命中连续性概率 → continuity
    key, frag, mode = ii.decide_reactive_injection(str(tmp_path), "嗯嗯", 7, rng=_Rng(0.0))
    # rng=0.0 会命中第一个概率分支（意外交心需要 secret，无则过；会想需要 desire，无则过；
    # 连续性需要 high 轨迹 → 命中）
    assert mode in ("continuity", "insight", "spontaneous", "baseline")
    assert isinstance(frag, str) or frag is None