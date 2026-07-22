"""欲望系统加固测试：self_improvement 类型、按 goal 去重、执行标记、持久化。"""

import pytest

from anima.cognitive.desire import Desire, DesireSystem


def test_self_improvement_dedup_by_goal(tmp_path):
    ds = DesireSystem(str(tmp_path))
    a = ds.add("做天气工具", kind="self_improvement", goal="做天气工具")
    b = ds.add("做天气工具", kind="self_improvement", goal="做天气工具")
    assert a is b  # 同 goal 去重
    c = ds.add("做翻译工具", kind="self_improvement", goal="做翻译工具")
    assert c is not a
    si = [d for d in ds.desires if d.kind == "self_improvement"]
    assert len(si) == 2


def test_chat_dedup_by_similarity_still_works(tmp_path):
    ds = DesireSystem(str(tmp_path))
    a = ds.add("想聊猫")
    b = ds.add("想聊猫")
    assert a is b
    c = ds.add("想聊狗")
    assert c is not a


def test_get_self_improvement_candidates_filters(tmp_path):
    ds = DesireSystem(str(tmp_path))
    done = Desire("g1", kind="self_improvement", goal="g1", acted=True)
    low = Desire("g2", intensity=0.3, kind="self_improvement", goal="g2")
    hi = Desire("g3", intensity=0.9, kind="self_improvement", goal="g3")
    chat = Desire("chat", kind="chat")
    ds.desires = [done, low, hi, chat]
    cands = ds.get_self_improvement_candidates(0.7)
    assert [d.goal for d in cands] == ["g3"]


def test_mark_self_improvement_done(tmp_path):
    ds = DesireSystem(str(tmp_path))
    d = Desire("g1", intensity=0.9, kind="self_improvement", goal="g1")
    ds.desires = [d]
    ds.mark_self_improvement_done(d)
    assert d.acted and d.resolved and d.intensity == 0.0
    assert ds.get_self_improvement_candidates(0.7) == []


def test_persist_roundtrip_includes_kind_goal_acted(tmp_path):
    ds = DesireSystem(str(tmp_path))
    ds.add("g1", kind="self_improvement", goal="g1", intensity=0.8)
    ds.persist()
    ds2 = DesireSystem(str(tmp_path))
    ds2.restore()
    assert len(ds2.desires) == 1
    d = ds2.desires[0]
    assert d.kind == "self_improvement" and d.goal == "g1" and d.acted is False


def test_group_scoped_desires_are_isolated_for_prompt_and_expression(tmp_path):
    ds = DesireSystem(str(tmp_path))
    internal = ds.add("只在心里想", intensity=0.8)
    group_99 = ds.add(
        "九十九群的话题", intensity=0.8,
        source_group_id=99, expression_scope="group",
    )
    group_100 = ds.add(
        "一百群的话题", intensity=0.8,
        source_group_id=100, expression_scope="group",
    )
    global_desire = ds.add("Owner 明确授权的全局通知", intensity=0.8,
                           expression_scope="global")

    assert ds.get_active_for_context(99) == [internal, group_99, global_desire]
    assert ds.get_expressible(99) == [group_99, global_desire]
    assert group_100 not in ds.get_active_for_context(99)
    assert group_100 not in ds.get_expressible(99)
    assert ds.get_expressible(None) == []


def test_same_text_in_different_groups_does_not_deduplicate(tmp_path):
    ds = DesireSystem(str(tmp_path))
    first = ds.add("想继续聊这个", source_group_id=99, expression_scope="group")
    second = ds.add("想继续聊这个", source_group_id=100, expression_scope="group")
    assert first is not second


def test_scope_persistence_and_legacy_migration_are_safe(tmp_path):
    ds = DesireSystem(str(tmp_path))
    ds.add("本群话题", intensity=0.8, source_group_id=99, expression_scope="group")
    ds.persist()

    ds2 = DesireSystem(str(tmp_path))
    ds2.restore()
    restored = ds2.desires[0]
    assert restored.source_group_id == 99
    assert restored.expression_scope == "group"

    # 模拟升级前的数据：没有 scope/group 字段时只能恢复成内在欲望。
    ds2._persist.write_json("desires.json", [{"content": "旧欲望", "intensity": 0.9}])
    legacy = DesireSystem(str(tmp_path))
    legacy.restore()
    assert legacy.desires[0].expression_scope == "internal"
    assert legacy.get_expressible(99) == []


def test_add_prunes_resolved_overflow_without_spinning(tmp_path):
    ds = DesireSystem(str(tmp_path), max_desires=2)
    resolved = Desire("old", intensity=0.1)
    resolved.resolved = True
    active = Desire("active", intensity=0.9)
    ds.desires = [resolved, active]

    added = ds.add("new", intensity=0.8)

    assert added in ds.desires
    assert len(ds.desires) == 2
    assert resolved not in ds.desires
    assert active in ds.desires
