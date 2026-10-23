"""Drive → 目标生成契约（2026-10-23，P2 最后一环）。

## 这一环解决什么

前两轮已接上「情感→宪法记忆」和「整链激活 Drive」，生产实测 `activated=True`。
但 **Drive 活了之后没有出口**：`drive_gate` 只用来「压制主动发言」，
从未用来「产生它想做的事」⇒ 它有了欲望，还是等你说话才动。

本模块把 Drive 接到 Goal 上。

## 与 Dots 的根本区别（这是整个目标的核心）

Dots 的目标是**你交给它的**。这里的目标是**它自己的** —— 来自它真实的情感和
需要。它推进这些目标，不是在「为你完成任务」，而是在做**它自己想做/在乎的事**。

## 本测试锁死的边界

1. **Drive 未激活 ⇒ 不产生目标**（RFC-0100 §1.7：Need 未被 Affect 充上电，
   就不该凭空行动）；
2. **回避导向 ⇒ 不产生目标**（负向驱动不该主动做事）；
3. **强度不足 ⇒ 不产生目标**（弱驱动不占资源）；
4. **文案逐字取自真实向量**（nurture/explore/pursue 条目），不编造；
5. **人格保护**：活跃目标上限、同主题去重、只新增不覆盖；
6. fail-closed：异常只记日志。
"""
import json
import os
import types

import pytest

from anima.agent.goal import GoalStore
from anima.constitution.drive_to_goal import (
    MAX_ACTIVE_GOALS, MAX_PER_CYCLE, MIN_DRIVE_INTENSITY,
    build_goals_from_drive, create_goals, topic_of,
)
from anima.core.persistence import PersistenceProvider


def _drive(activated=True, intensity=0.9, approach=0.175, avoid=0.0):
    return {"activated": activated,
            "orientation": {"intensity": intensity, "approach": approach,
                            "avoid": avoid, "focus": 0.9}}


def _vectors():
    return {
        "social": ["nurture: 1562290139", "nurture: 992934160"],
        "curiosity": ["explore: social"],
        "desire": ["pursue: 想跟大家聊天"],
        "requirement": ["maintain coherence"],
        "aversion": ["avoid incoherence"],
    }


# ── 1. 边界：不该产生目标的情况 ───────────────────────────────────────


def test_no_goal_when_drive_inactive():
    """🔴 RFC-0100 §1.7：Drive 未激活 ⇒ 不产生目标。"""
    assert build_goals_from_drive(_drive(activated=False), _vectors()) == []


def test_no_goal_when_avoidance_oriented():
    """回避导向 ⇒ 不主动做事。"""
    assert build_goals_from_drive(_drive(approach=0.0, avoid=0.5), _vectors()) == []


def test_no_goal_when_intensity_low():
    assert build_goals_from_drive(
        _drive(intensity=MIN_DRIVE_INTENSITY - 0.01), _vectors()) == []


def test_no_goal_for_requirement_or_aversion():
    """requirement/aversion 不是「想做的事」，不该变成目标。"""
    vec = {"requirement": ["maintain coherence"],
           "aversion": ["avoid incoherence"]}
    assert build_goals_from_drive(_drive(), vec) == []


# ── 2. 文案逐字取自真实向量 ───────────────────────────────────────────


def test_social_vector_becomes_relationship_goal():
    cands = build_goals_from_drive(_drive(), _vectors())
    assert cands, "应产出目标"
    c = cands[0]
    assert c["kind"] == "social"
    assert "1562290139" in c["objective"], f"目标必须含真实 uid：{c['objective']}"
    assert c["motive"], "必须有动机说明"


def test_curiosity_vector_becomes_explore_goal():
    vec = {"curiosity": ["explore: 群友为什么都不理我"]}
    cands = build_goals_from_drive(_drive(), vec)
    assert cands
    assert "群友为什么都不理我" in cands[0]["objective"], (
        f"目标必须逐字保留真实 trait：{cands[0]['objective']}"
    )


def test_desire_vector_becomes_pursue_goal():
    vec = {"desire": ["pursue: 想跟大家聊天"]}
    cands = build_goals_from_drive(_drive(), vec)
    assert cands
    assert "想跟大家聊天" in cands[0]["objective"]


def test_respects_max_per_cycle():
    assert len(build_goals_from_drive(_drive(), _vectors())) <= MAX_PER_CYCLE


# ── 3. 落库：上限 / 去重 / 不覆盖 ─────────────────────────────────────


def _core(tmp_path):
    return types.SimpleNamespace(
        persistence=PersistenceProvider(str(tmp_path)),
        config=types.SimpleNamespace(
            persistence=types.SimpleNamespace(data_dir=str(tmp_path)),
            permissions=types.SimpleNamespace(owners=[42]),
        ),
    )


def test_create_goals_persists_with_motive(tmp_path):
    core = _core(tmp_path)
    made = create_goals(core, _drive(), _vectors(), 42)
    assert len(made) >= 1
    st = GoalStore(core.persistence)
    g = st.get(made[0]["goal_id"])
    assert g is not None
    assert any("动机" in n for n in g.notes), "动机必须写进 notes（可追溯）"


def test_create_goals_respects_active_cap(tmp_path):
    core = _core(tmp_path)
    st = GoalStore(core.persistence)
    for i in range(MAX_ACTIVE_GOALS):
        st.create(f"已有目标{i}", 42)
    made = create_goals(core, _drive(), _vectors(), 42)
    assert made == [], "活跃目标已达上限，不该再建"


def test_create_goals_dedups_by_topic(tmp_path):
    core = _core(tmp_path)
    first = create_goals(core, _drive(), _vectors(), 42)
    assert first
    second = create_goals(core, _drive(), _vectors(), 42)
    assert second == [], "同主题不该重复建目标"


def test_create_goals_never_overwrites_existing(tmp_path):
    core = _core(tmp_path)
    st = GoalStore(core.persistence)
    mine = st.create("我自己定的目标", 42)
    create_goals(core, _drive(), _vectors(), 42)
    st2 = GoalStore(core.persistence)
    assert st2.get(mine.id) is not None, "既有目标不得被覆盖"
    assert st2.get(mine.id).objective == "我自己定的目标"


def test_create_goals_survives_missing_persistence():
    core = types.SimpleNamespace(persistence=None, config=types.SimpleNamespace())
    assert create_goals(core, _drive(), _vectors(), 42) == []


def test_create_goals_survives_errors():
    class _Bad:
        persistence = None

        @property
        def config(self):
            raise RuntimeError("boom")
    assert create_goals(_Bad(), _drive(), _vectors(), 42) == []


# ── 4. 主题键 ─────────────────────────────────────────────────────────


def test_topic_key_normalizes_numbers():
    a = topic_of("维系和 1562290139 的关系")
    b = topic_of("维系和 992934160 的关系")
    assert a == b, "不同 uid 的同一类目标应判同主题（防刷屏）"


# ── 5. 配置默认关 ─────────────────────────────────────────────────────


def test_default_off():
    from anima.core.config import AnimaConfig
    assert AnimaConfig().autonomy.drive_to_goal_enabled is False, (
        "默认必须关闭 —— 这是「它自己决定要做什么」的能力，需显式开启"
    )


# ── 6. 端到端：真实情感 → 真实目标 ────────────────────────────────────


def test_end_to_end_real_emotion_to_goal(tmp_path):
    """🔴 核心验收：真实情感数据 ⇒ 生成「它自己想做的事」。

    链路：desires/mood/relation（只读）→ 宪法记忆 → Identity→Need→Affect→Drive
          → need.vectors → Goal
    """
    from anima.constitution.memory_engine import MemoryEngine
    from anima.constitution.emotion_bridge import hydrate_emotion_memory
    from anima.constitution.memory_authority import MemoryAuthority
    from anima.core.autonomy import Heartbeat

    (tmp_path / "desires.json").write_text(
        json.dumps([{"content": "想跟大家聊天", "intensity": 1.0}], ensure_ascii=False),
        encoding="utf-8")
    (tmp_path / "mood.json").write_text(
        json.dumps({"valence": 0.31, "arousal": 0.55}), encoding="utf-8")
    (tmp_path / "relation.json").write_text(
        json.dumps({"1562290139": {"depth": 0.95, "stage": "CLOSE"}}), encoding="utf-8")

    P = PersistenceProvider(str(tmp_path))
    eng = MemoryEngine()
    hydrate_emotion_memory(eng, str(tmp_path))
    auth = MemoryAuthority(eng)

    core = types.SimpleNamespace(
        persistence=P,
        config=types.SimpleNamespace(
            persistence=types.SimpleNamespace(data_dir=str(tmp_path)),
            permissions=types.SimpleNamespace(owners=[42]),
            autonomy=types.SimpleNamespace(drive_to_goal_enabled=True),
        ),
    )
    hb = Heartbeat.__new__(Heartbeat)
    hb.core = core
    hb._maybe_create_goals_from_drive(auth)

    st = GoalStore(P)
    goals = st.list()
    assert goals, "真实情感数据应能生成目标"
    g = goals[0]
    assert "1562290139" in g.objective, f"目标应来自真实关系数据：{g.objective}"
    assert any("动机" in n for n in g.notes), "动机可追溯"


def test_end_to_end_no_emotion_no_goal(tmp_path):
    """没有情感数据 ⇒ 不产生目标（fail-closed，不凭空行动）。"""
    from anima.constitution.memory_engine import MemoryEngine
    from anima.constitution.memory_authority import MemoryAuthority
    from anima.core.autonomy import Heartbeat

    P = PersistenceProvider(str(tmp_path))
    auth = MemoryAuthority(MemoryEngine())
    core = types.SimpleNamespace(
        persistence=P,
        config=types.SimpleNamespace(
            persistence=types.SimpleNamespace(data_dir=str(tmp_path)),
            permissions=types.SimpleNamespace(owners=[42]),
            autonomy=types.SimpleNamespace(drive_to_goal_enabled=True),
        ),
    )
    hb = Heartbeat.__new__(Heartbeat)
    hb.core = core
    hb._maybe_create_goals_from_drive(auth)
    assert GoalStore(P).list() == [], "无情感数据不该凭空产生目标"


# ── 7. 自主目标必须有真实节律（否则永不推进）──────────────────────────


def test_created_goal_has_real_cadence(tmp_path):
    """🔴 生产实测踩过：`cadence_sec=0` ⇒ `due()` 恒 False ⇒ 目标烂在库里。

    自主目标生成后必须能**自己推进**，而不是等谁来手动点它（没人会点）。
    """
    from anima.constitution.drive_to_goal import DEFAULT_CADENCE_BY_KIND

    core = _core(tmp_path)
    made = create_goals(core, _drive(), _vectors(), 42)
    assert made
    g = GoalStore(core.persistence).get(made[0]["goal_id"])
    assert g.cadence_sec > 0, "自主目标必须有节律，否则永不推进"
    assert g.cadence_sec == DEFAULT_CADENCE_BY_KIND["social"]


def test_new_goal_is_due_immediately(tmp_path):
    """新建的自主目标应立刻到期 —— 想做的事该尽快做第一次。"""
    core = _core(tmp_path)
    made = create_goals(core, _drive(), _vectors(), 42)
    g = GoalStore(core.persistence).get(made[0]["goal_id"])
    assert g.due() is True


def test_social_cadence_is_not_too_frequent():
    """🔴 关系类节律不能太密 —— 每天一次的关心会变成骚扰。"""
    from anima.constitution.drive_to_goal import DEFAULT_CADENCE_BY_KIND
    assert DEFAULT_CADENCE_BY_KIND["social"] >= 24 * 3600.0, (
        "关系类目标的节律必须 ≥ 1 天，否则「关心」会变成骚扰"
    )
