"""熟悉度质量闸门回归（R1，2026-10-12）。

🔴 修复前（生产实证，2026-10-11/12 24h）：
    `RelationEngine.touch()` 的 familiarity 增益**无条件 +0.02**，与消息内容
    好坏无关 ⇒ 群里「骂 bot」也算一次交互、照样涨熟度。
    生产关系库 91 条中 **8 个高频用户里 6 个 fam = 1.000（封顶）**，而熟人放行
    门槛（`permissions.group_policies.<gid>.familiar_min` = 0.85）对他们**恒成立**
    ⇒ 只要消息 >12 字、非闲聊短语、非纯图、非 @ 别人，就一律放行
    ⇒ 观感「没叫它也回」，且**骂它反而喂它熟悉度**（正反馈环）。

本测试锁死：关系破裂（信任见底 / 情绪显著负面）时熟悉度**不再上涨**，正常关系行为不变。
"""
from __future__ import annotations

import types

from anima.cognitive import relation as relation_mod
from anima.cognitive.relation import Relation, RelationEngine

import pytest


def _engine(tmp_path, monkeypatch, **overrides):
    data_dir = str(tmp_path)
    monkeypatch.setattr(relation_mod, "_relation_engines", {})
    eng = RelationEngine(data_dir)
    return eng, data_dir


def _rel(monkeypatch, familiarity, trust, valence):
    """构造一个 Relation，绕过持久化的方式：直接放到 engine 的 _relations 里。"""
    r = Relation(4242)
    r.familiarity = familiarity
    r.trust = trust
    r.valence = valence
    return r


# ---------------------------------------------------------------------------
# 🔴 RED：修复前这两条是**失败**的（familiarity 无条件上涨）
# ---------------------------------------------------------------------------

def test_familiarity_does_not_grow_when_trust_broken():
    """信任见底（trust <= 0.3）的用户，交互后熟悉度不得继续上涨。"""
    eng = RelationEngine("data")
    r = _rel(None, familiarity=1.0, trust=0.1, valence=0.2)
    eng._relations[4242] = r
    eng.touch(4242, "你又骗我")
    assert r.familiarity < 1.0, (
        "信任见底的用户熟悉度仍在上涨 ⇒ 「骂它反而喂它熟悉度」的正反馈环未切断"
    )


def test_familiarity_does_not_grow_when_valence_negative():
    """情绪显著负面（valence <= -0.3）的用户，交互后熟悉度不得继续上涨。"""
    eng = RelationEngine("data")
    r = _rel(None, familiarity=0.9, trust=0.8, valence=-0.5)
    eng._relations[4242] = r
    eng.touch(4242, "随便啦")
    assert r.familiarity < 0.9


# ---------------------------------------------------------------------------
# ✅ GREEN：修复后必须仍然成立（回归保护）
# ---------------------------------------------------------------------------

def test_familiarity_still_grows_for_healthy_relationship():
    """正常关系（trust/valence 健康）⇒ 熟悉度照常上涨，行为与修复前一致。"""
    eng = RelationEngine("data")
    r = _rel(None, familiarity=0.5, trust=0.7, valence=0.3)
    eng._relations[4242] = r
    before = r.familiarity
    eng.touch(4242, "今天天气不错")
    assert r.familiarity > before
    # 原逻辑：>0.6 衰减一半 ⇒ 0.02；此处 0.5 <= 0.6 ⇒ +0.02
    assert abs(r.familiarity - (before + 0.02)) < 1e-9


def test_familiarity_never_below_floor():
    """熟悉度有下限保护，不会因长期破裂变成负数。"""
    eng = RelationEngine("data")
    r = _rel(None, familiarity=0.04, trust=0.1, valence=-0.9)
    eng._relations[4242] = r
    for _ in range(20):
        eng.touch(4242, "又是你")
    assert r.familiarity >= 0.03


def test_owner_exempt_from_quality_decay():
    """owner 即使信任/情绪读数差，也**不**被熟悉度衰减（ta 是主人，不是攻击）。"""
    eng = RelationEngine("data")
    r = _rel(None, familiarity=0.9, trust=0.1, valence=-0.9)
    eng._relations[4242] = r
    before = r.familiarity
    eng.touch(4242, "你这个骗子", is_owner=True)
    assert r.familiarity > before


def test_gate_can_be_disabled_via_autonomy_config():
    """`familiarity_quality_gate=False` ⇒ 完全回到修复前行为（应急回滚路径）。"""
    eng = RelationEngine("data", autonomy=types.SimpleNamespace(
        familiarity_quality_gate=False))
    r = _rel(None, familiarity=0.9, trust=0.1, valence=-0.9)
    eng._relations[4242] = r
    before = r.familiarity
    eng.touch(4242, "你又骗我")
    assert r.familiarity > before, "关闭开关后应恢复旧的无条件上涨行为"


def test_familiarity_capped_at_one():
    """熟悉度上限 1.0（原语义保留）。"""
    eng = RelationEngine("data")
    r = _rel(None, familiarity=0.995, trust=0.9, valence=0.5)
    eng._relations[4242] = r
    eng.touch(4242, "嗨")
    assert r.familiarity <= 1.0
