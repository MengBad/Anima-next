"""P1-2 记忆压缩（DESIGN_MEMORY_COMPRESSION.md §5）。

全部用 fake 对象做确定性验证，不依赖真实 LLM / 向量库。
"""
from __future__ import annotations

import asyncio
import os
import time

import pytest

from anima.crossgroup.memory.compression import (
    CompressionEngine,
    cluster_facts,
    decay_user_facts,
    enforce_cap,
)
from anima.crossgroup.memory.manager import CrossGroupManager
from anima.crossgroup.memory.user_memory import UserFact


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
class _FakeUser:
    def __init__(self, facts=None):
        self.facts = facts or []


class _FakeManager:
    """最小可用的 crossgroup manager 替身（只需 _users + flush）。"""

    def __init__(self, users, data_dir, fail_flush=False):
        self._users = users
        self._data_dir = data_dir
        self._fail_flush = fail_flush
        self.flush_calls = 0

    def _users_path(self):
        return os.path.join(self._data_dir, "users.json")

    def flush(self):
        self.flush_calls += 1
        if self._fail_flush:
            raise RuntimeError("disk full")


def _fact(content, conf=1.0, group="g1", ts=None, **kw):
    f = UserFact(content=content, source_group=group, confidence=conf, **kw)
    if ts is not None:
        f.timestamp = ts
    return f


def _sim_always(v):
    return lambda a, b: v


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# 1. confidence 落库
# ---------------------------------------------------------------------------
def test_confidence_propagates_to_add_fact(tmp_path):
    cgm = CrossGroupManager(str(tmp_path / "cg"))
    f = cgm.add_user_fact("u1", "喜欢简短回复", source_group="g1", confidence=0.9)
    assert f is not None
    assert f.confidence == pytest.approx(0.9)
    # 置信度越界自动夹紧
    assert cgm.add_user_fact("u1", "x", source_group="g1", confidence=5).confidence == 1.0
    assert cgm.add_user_fact("u1", "y", source_group="g1").confidence == 1.0


def test_extraction_confidence_wired_in_handler_source():
    """抽取层产出的 confidence 必须真正传下去（此前被丢弃）。"""
    import anima.llm.handler as h

    src = open(h.__file__, encoding="utf-8").read()
    assert "confidence=float(f.get(\"confidence\") or 0.5)" in src


def test_new_fields_survive_persistence(tmp_path):
    d = str(tmp_path / "cg")
    cgm = CrossGroupManager(d)
    cgm.add_user_fact("u1", "在学 Rust", source_group="g1", confidence=0.6)
    u = cgm.get_user("u1")
    u.facts[0].disputed = True
    u.facts[0].merge_count = 4
    u.facts[0].access_count = 7
    cgm.flush()

    cgm2 = CrossGroupManager(d)
    f = cgm2.get_user("u1").facts[0]
    assert f.confidence == pytest.approx(0.6)
    assert f.disputed is True
    assert f.merge_count == 4
    assert f.access_count == 7


def test_old_users_json_without_new_fields_still_loads(tmp_path):
    """老数据缺 confidence/disputed → dataclass 默认值补齐，不炸。"""
    d = str(tmp_path / "cg")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "users.json"), "w", encoding="utf-8") as fh:
        fh.write('{"u1": {"name": "A", "facts": [{"content": "老事实",'
                 ' "source_group": "g1", "timestamp": 1.0, "tags": []}]}}')
    cgm = CrossGroupManager(d)
    f = cgm.get_user("u1").facts[0]
    assert f.confidence == 1.0 and f.disputed is False and f.merge_count == 1


# ---------------------------------------------------------------------------
# 2. 聚类 / 合并
# ---------------------------------------------------------------------------
def test_similar_facts_clustered_and_merged(tmp_path):
    user = _FakeUser([_fact("喜欢弹钢琴", ts=1), _fact("喜欢弹钢琴", ts=2)])
    engine = CompressionEngine(str(tmp_path))
    rep = _run(engine.run(_FakeManager({"u1": user}, str(tmp_path))))
    assert rep["merged"] == 1
    assert len(user.facts) == 1
    assert user.facts[0].compressed is True
    assert user.facts[0].merge_count == 2


def test_dissimilar_facts_kept_separate(tmp_path):
    user = _FakeUser([_fact("喜欢弹钢琴", ts=1), _fact("在写操作系统内核", ts=2)])
    engine = CompressionEngine(str(tmp_path))
    rep = _run(engine.run(
        _FakeManager({"u1": user}, str(tmp_path)),
        vector_memory=type("V", (), {"similarity": staticmethod(_sim_always(0.1))})(),
    ))
    assert rep["merged"] == 0
    assert len(user.facts) == 2


def test_disputed_fact_not_merged(tmp_path):
    a = _fact("住在深圳", ts=1, disputed=True)
    b = _fact("住在深圳", ts=2)
    clusters = cluster_facts([a, b], sim_fn=_sim_always(1.0))
    assert len(clusters) == 2  # disputed 独立成簇
    assert any(c[0] is a for c in clusters)


def test_merge_keeps_source_groups_and_trace(tmp_path):
    user = _FakeUser([_fact("喜欢在深夜写代码", group="g1", ts=1),
                      _fact("喜欢在深夜写代码", group="g2", ts=2, conf=0.5)])
    engine = CompressionEngine(str(tmp_path))
    _run(engine.run(_FakeManager({"u1": user}, str(tmp_path))))
    assert len(user.facts) == 1
    assert set(user.facts[0].source_groups or []) == {"g1", "g2"}
    # 被吞掉的原文保留在 merged_from，可追溯
    assert user.facts[0].merged_from


def test_short_fragments_not_merged():
    """过短碎片不参与合并（防「喜欢猫」与「不喜欢猫」被揉一起）。"""
    clusters = cluster_facts([_fact("猫", ts=1), _fact("狗", ts=2)],
                             sim_fn=_sim_always(1.0))
    assert len(clusters) == 2


# ---------------------------------------------------------------------------
# 3. 衰减
# ---------------------------------------------------------------------------
def test_daily_decay_applied(tmp_path):
    user = _FakeUser([_fact("x", conf=1.0)])
    engine = CompressionEngine(str(tmp_path))
    _run(engine.run(_FakeManager({"u1": user}, str(tmp_path))))
    assert user.facts[0].confidence == pytest.approx(0.99, abs=1e-6)


def test_disputed_immune_to_decay():
    f = _fact("x", conf=0.8, disputed=True)
    decay_user_facts([f], 0.99)
    assert f.confidence == pytest.approx(0.8)


def test_recall_bumps_confidence_capped_at_one():
    from anima.crossgroup.memory.user_memory import UserMemory

    u = UserMemory(user_id="u1")
    f = u.add_fact("x", "g1", confidence=0.99)
    u.recall(k=1)
    assert f.confidence == 1.0  # 回弹但不越界
    assert f.access_count == 1


# ---------------------------------------------------------------------------
# 4. 容量淘汰
# ---------------------------------------------------------------------------
def test_cap_enforces_max_facts(tmp_path):
    engine = CompressionEngine(str(tmp_path))
    user = _FakeUser([_fact(f"低置信事实{i}", conf=0.1 * (i % 9 + 1) / 9, ts=float(i))
                      for i in range(40)])
    user.facts.append(_fact("私聊关键事实", conf=0.05, group="private", ts=0.5))
    user.facts.append(_fact("争议事实", conf=0.05, ts=0.6, disputed=True))
    # 全部互不相识（sim=0）→ 只走衰减 + 淘汰，隔离验证 cap 本身
    _run(engine.run(
        _FakeManager({"u1": user}, str(tmp_path)),
        vector_memory=type("V", (), {"similarity": staticmethod(_sim_always(0.0))})(),
    ))
    assert len(user.facts) == 30  # 默认 max_facts_per_user
    contents = [f.content for f in user.facts]
    assert "私聊关键事实" in contents   # private 保护
    assert "争议事实" in contents       # disputed 保护


def test_cap_protects_high_merge_count():
    facts = [_fact(f"f{i}", conf=0.01, ts=float(i)) for i in range(5)]
    facts[0].merge_count = 3
    enforce_cap(facts, 2)
    assert any(f.merge_count == 3 for f in facts)


def test_cap_never_drops_when_all_protected():
    facts = [_fact("p", group="private", conf=0.01), _fact("d", conf=0.01, disputed=True)]
    assert enforce_cap(facts, 1) == 0
    assert len(facts) == 2


# ---------------------------------------------------------------------------
# 5. 成本控制
# ---------------------------------------------------------------------------
class _RecordingLLM:
    def __init__(self):
        self.calls = 0

    async def chat(self, **kwargs):
        self.calls += 1
        return "合并后的事实"


def test_no_llm_for_small_user(tmp_path):
    llm = _RecordingLLM()
    user = _FakeUser([_fact("喜欢弹钢琴", ts=1), _fact("喜欢弹钢琴", ts=2)])
    engine = CompressionEngine(str(tmp_path))
    _run(engine.run(_FakeManager({"u1": user}, str(tmp_path)), llm=llm))
    assert llm.calls == 0  # 小用户不调 LLM


# ---------------------------------------------------------------------------
# 6. 调度 gate
# ---------------------------------------------------------------------------
def test_daily_gate_limits_runs(tmp_path):
    engine = CompressionEngine(str(tmp_path))
    mgr = _FakeManager({"u1": _FakeUser([_fact("a", ts=1)])}, str(tmp_path))
    now = time.time()
    rep1 = _run(engine.maybe_run(mgr, now=now, force=True))
    assert rep1.get("users") == 1
    rep2 = _run(engine.maybe_run(mgr, now=now + 3600))  # 1 小时后
    assert rep2.get("skipped") == "daily_gate"


def test_disabled_short_circuits(tmp_path):
    engine = CompressionEngine(str(tmp_path), cfg=type("C", (), {"enabled": False})())
    assert _run(engine.maybe_run(_FakeManager({}, str(tmp_path))))["skipped"] == "disabled"


# ---------------------------------------------------------------------------
# 7. 回滚
# ---------------------------------------------------------------------------
def test_backup_created_and_rollback_restores(tmp_path):
    d = str(tmp_path)
    # 造一份真实 users.json
    cgm = CrossGroupManager(d)
    cgm.add_user_fact("u1", "压缩前的事实", source_group="g1")
    before = open(os.path.join(d, "users.json"), encoding="utf-8").read()

    engine = CompressionEngine(d)
    users = {"u1": _FakeUser([_fact("压缩后", ts=1)])}
    _run(engine.run(_FakeManager(users, d)))
    baks = [f for f in os.listdir(d) if f.startswith("users.json.bak-")]
    assert baks, "压缩前必须留快照"

    # 手动覆盖成"压缩后"的错误状态再回滚
    with open(os.path.join(d, "users.json"), "w", encoding="utf-8") as fh:
        fh.write('{"u1": {"name": "", "facts": []}}')
    assert engine.rollback(_FakeManager(users, d)) is True
    assert open(os.path.join(d, "users.json"), encoding="utf-8").read() == before


def test_flush_failure_leaves_memory_unchanged(tmp_path):
    user = _FakeUser([_fact("喜欢弹钢琴", ts=1), _fact("喜欢弹钢琴", ts=2)])
    engine = CompressionEngine(str(tmp_path))
    rep = _run(engine.run(_FakeManager({"u1": user}, str(tmp_path),
                                       fail_flush=True)))
    assert rep.get("error")
    # 落盘失败 → 内存也整体还原，不出现「内存已压缩、磁盘没压缩」的半状态
    assert len(user.facts) == 2
    assert all(not f.compressed for f in user.facts)


def test_per_user_error_isolated(tmp_path):
    """某一个用户压缩抛错 → 跳过该用户，其他用户照常。"""
    class _BoomUser:
        """facts 一读就炸 —— 模拟某用户数据异常。"""

        @property
        def facts(self):
            raise RuntimeError("boom")

    good_user = _FakeUser([_fact("喜欢弹钢琴", ts=1), _fact("喜欢弹钢琴", ts=2)])
    engine = CompressionEngine(str(tmp_path))
    rep = _run(engine.run(_FakeManager(
        {"bad": _BoomUser(), "good": good_user}, str(tmp_path))))
    assert rep["merged"] == 1
    assert len(good_user.facts) == 1
    assert any("bad" in e for e in rep["errors"])
