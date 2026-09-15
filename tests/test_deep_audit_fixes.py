"""批次5/6 深审修复回归（2026-09-12）：记忆可靠性 + 自主性质量。"""
import json
import time

import pytest

from anima.core.persistence import PersistenceProvider, atomic_write_text


# ── 批次5：记忆可靠性 ───────────────────────────────────────────────
def test_read_json_corrupt_leaves_evidence_and_returns_default(tmp_path):
    p = PersistenceProvider(str(tmp_path))
    p.write_json("relation.json", {"1": {"a": 1}})
    target = tmp_path / "relation.json"
    target.write_text("{corrupted!!", encoding="utf-8")
    assert p.read_json("relation.json", {"d": 1}) == {"d": 1}
    backup = list(tmp_path.glob("relation.json.corrupt.bak"))
    assert backup, "损坏文件应改名留证"
    assert "corrupted" in backup[0].read_text(encoding="utf-8")


def test_relation_touch_goes_through_deferred_flush(tmp_path):
    from anima.cognitive.relation import get_relation, _relation_engines
    _relation_engines.clear()
    eng = get_relation(str(tmp_path))
    eng.touch(4242, "你好呀")
    assert eng._persist.has_deferred, "touch 应登记 deferred 脏数据"
    eng.flush_deferred_if_any()
    data = json.loads((tmp_path / "relation.json").read_text(encoding="utf-8"))
    assert "4242" in data and data["4242"]["interaction_count"] >= 1
    assert not eng._persist.has_deferred
    _relation_engines.clear()


def test_atomic_write_text(tmp_path):
    target = tmp_path / "sub" / "t.json"
    atomic_write_text(target, '{"ok": 1}')
    assert target.read_text(encoding="utf-8") == '{"ok": 1}'


def test_chat_memory_save_skips_when_clean(tmp_path):
    from anima.memory.chat_memory import ChatMemory
    cm = ChatMemory(persist_path=str(tmp_path / "cm.json"))
    cm.save()  # 干净：不应产生写盘（save 内脏守卫直接返回）
    assert not (tmp_path / "cm.json").exists() or "hello" not in (tmp_path / "cm.json").read_text(encoding="utf-8")
    cm.add("group", 1, "user", "小测", "hello")
    cm.save()
    assert "hello" in (tmp_path / "cm.json").read_text(encoding="utf-8"), "有脏数据时 save 应落盘"


# ── 批次6：自主性质量 ───────────────────────────────────────────────
def test_intent_time_window_dedup(tmp_path):
    from anima.core.intents import IntentLedger
    from anima.core.persistence import PersistenceProvider
    led = IntentLedger(PersistenceProvider(str(tmp_path)))
    a = led.create(kind="commitment", source="episodes", target_kind="group",
                   target_id=727, content="明天帮小测带一份炒面")
    b = led.create(kind="commitment", source="episodes", target_kind="group",
                   target_id=727, content="明天帮小测带一份炒面哈")
    assert a.id == b.id, "24h 内高相似承诺应去重（换措辞不重复建档）"
    c = led.create(kind="commitment", source="episodes", target_kind="group",
                   target_id=727, content="完全不同的另一件事，帮我把服务器重启一下")
    assert c.id != a.id, "不同事项不应被误去重"


def test_group_presence_singleton_and_active_groups(tmp_path):
    from anima.cognitive import group_presence as gp_mod
    gp_mod._managers.clear()
    gp_mod.set_group_presence_data_dir(str(tmp_path))
    m1 = gp_mod.get_group_presence()
    m2 = gp_mod.get_group_presence()
    assert m1 is m2, "应为进程级单例（防双实例双写 group_presence.json）"
    m1.observe(727)
    m1.observe(727)
    m1.observe(552)
    assert m1.get_active_groups(limit=1) == [727]


def test_promise_flag_skips_episodes_capture():
    """B 批次 6b：generation 建承诺议程后 handler 跳过 episodes 二次建档"""
    from anima.pipeline.stages._shared import _detect_promise_and_schedule
    src = open("anima/pipeline/stages/_shared.py", encoding="utf-8").read()
    assert 'ctx.meta["promise_agenda_created"]' in src
    hsrc = open("anima/llm/handler.py", encoding="utf-8").read()
    assert 'promise_agenda_created' in hsrc and 'episodes.observe' in hsrc
    assert 'getattr' in hsrc or 'ctx.meta.get' in hsrc
