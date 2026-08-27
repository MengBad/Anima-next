# -*- coding: utf-8 -*-
"""L1/L2/L3 点亮暗线：意外交心 / S6接loop / S2A问梦分层。"""
import io
import os

from anima.cognitive.inner_state import decide_reactive_injection
from anima.cognitive.inner_state.secret_feel import (
    maybe_spontaneous_confide,
    pick_unattributed_secret,
)
from anima.cognitive.inner_state.dream_tell import build_dream_tell_fragment


def _fake_secrets(secrets):
    from types import SimpleNamespace
    return SimpleNamespace(
        for_target=lambda uid: [s for s in secrets if getattr(s, "target_uid", "") == uid],
        all=lambda: list(secrets),
    )


# ---------------- L1 意外交心 ----------------

def test_spontaneous_requires_close_relation(tmp_path, monkeypatch):
    import anima.cognitive.inner_state.secret_feel as sf
    monkeypatch.setattr(sf, "_relation_stage", lambda dd, uid: "STRANGER")
    assert maybe_spontaneous_confide(str(tmp_path), 1) is None   # 非亲近绝不触发


def test_spontaneous_respects_probability_and_daily_cap(tmp_path, monkeypatch):
    import anima.cognitive.inner_state.secret_feel as sf
    import anima.memory.secrets as sec_mod
    from types import SimpleNamespace
    sec = SimpleNamespace(content="偷偷在晚上多看了一会儿月亮",
                          revealed=False, influence="subtle", target_uid="")
    monkeypatch.setattr(sec_mod, "SecretsStore", lambda dd: _fake_secrets([sec]))
    monkeypatch.setattr(sf, "_relation_stage", lambda dd, uid: "CLOSE")
    class _Rng:
        def __init__(self):
            self.v = 0.0
        def random(self):
            return self.v
    rng = _Rng()
    rng.v = 0.99   # 高于 2% 阈值 → 不触发
    assert maybe_spontaneous_confide(str(tmp_path), 1, rng=rng) is None
    rng.v = 0.0    # 命中概率 → 触发一次
    assert maybe_spontaneous_confide(str(tmp_path), 1, rng=rng) is not None
    # 日上限内不再重复
    rng.v = 0.0
    assert maybe_spontaneous_confide(str(tmp_path), 1, rng=rng) is None


def test_spontaneous_never_leaks_targeted_secret(tmp_path, monkeypatch):
    import anima.cognitive.inner_state.secret_feel as sf
    import anima.memory.secrets as sec_mod
    from types import SimpleNamespace
    # 只有「针对他人」的秘密：意外交心绝不选
    targeted = SimpleNamespace(content="关于小明的秘密", revealed=False,
                               influence="subtle", target_uid="99")
    monkeypatch.setattr(sec_mod, "SecretsStore", lambda dd: _fake_secrets([targeted]))
    assert pick_unattributed_secret(str(tmp_path), 7) is None
    monkeypatch.setattr(sf, "_relation_stage", lambda dd, uid: "CLOSE")
    class _Rng:
        def random(self):
            return 0.0
    assert maybe_spontaneous_confide(str(tmp_path), 7, rng=_Rng()) is None


# ---------------- L1 被问流露路径（复用） ----------------

def test_inquired_confide_via_coordinator(tmp_path, monkeypatch):
    import anima.cognitive.inner_state.secret_feel as sf
    import anima.memory.secrets as sec_mod
    from types import SimpleNamespace
    sec = SimpleNamespace(content="其实我一直记得你第一次来找我说话",
                          revealed=False, influence="subtle", target_uid="")
    monkeypatch.setattr(sec_mod, "SecretsStore", lambda dd: _fake_secrets([sec]))
    monkeypatch.setattr(sf, "_relation_stage", lambda dd, uid: "CLOSE")
    key, frag, mode = decide_reactive_injection(str(tmp_path), "你有什么秘密吗", 7)
    assert mode == "secret" and key == "inner_state_secret"
    assert "第一次来找我说话" in frag


# ---------------- L3 问梦分层 ----------------

def test_dream_tell_layering(tmp_path, monkeypatch):
    import anima.cognitive.inner_state.dream_tell as dt
    fake = {"scene": "梦里在鱼干海洋游泳", "insight": "醒来有点饿", "mood_tag": "happy"}
    monkeypatch.setattr(dt, "get_recent_dream", lambda dd: fake)
    monkeypatch.setattr(dt, "relation_is_close", lambda dd, uid: True)
    close_frag = build_dream_tell_fragment(str(tmp_path), 1, "你昨晚做了什么梦")
    assert close_frag and "鱼干海洋" in close_frag and "醒来有点饿" in close_frag
    monkeypatch.setattr(dt, "relation_is_close", lambda dd, uid: False)
    stranger_frag = build_dream_tell_fragment(str(tmp_path), 1, "你昨晚做了什么梦")
    assert stranger_frag and "记不清" in stranger_frag and "鱼干海洋" not in stranger_frag


def test_dream_inquiry_coordinator_priority(tmp_path, monkeypatch):
    import anima.cognitive.inner_state.dream_tell as dt
    fake = {"scene": "梦里有只大猫", "insight": "", "mood_tag": "calm"}
    monkeypatch.setattr(dt, "get_recent_dream", lambda dd: fake)
    monkeypatch.setattr(dt, "relation_is_close", lambda dd, uid: True)
    key, frag, mode = decide_reactive_injection(str(tmp_path), "你昨晚做了什么梦呀", 7)
    assert mode == "dream" and key == "inner_state_dream"


# ---------------- L2 S6 接 loop 契约 ----------------

def test_s6_capture_wired_to_cognition():
    src = io.open("anima/pipeline/stages/cognition.py", encoding="utf-8").read()
    assert "SelfLearnIntentRegistry" in src
    assert "capture" in src
    assert "L2 S6 接 loop" in src