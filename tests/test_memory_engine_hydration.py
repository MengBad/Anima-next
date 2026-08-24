# -*- coding: utf-8 -*-
"""MemoryEngine.hydrate (M1.2) tests —— 从 chat_memory 有界水合, 词汇稳定/fail-soft。"""
from anima.constitution.memory_engine import MemoryEngine


class _FakeCM:
    """注入式 chat_memory 适配(隔离, 不碰全局单例)。"""
    def __init__(self, groups):
        self._groups = groups
    def group_recent_ids(self):
        return set(self._groups)
    def get_recent_group_context(self, gid, limit=12):
        return [
            {"role": "user", "text": "hello group " + str(gid)},
            {"role": "user", "text": "second line"},
            {"role": "user", "text": ""},   # 空文本应跳过
        ]


def test_hydrate_empty_source():
    e = MemoryEngine()
    n = e.hydrate(_FakeCM({}))
    assert n == 0
    assert e.snapshot().episodes == []


def test_hydrate_samples_episodes():
    e = MemoryEngine()
    n = e.hydrate(_FakeCM({1001, 1002}))
    assert n == 4           # 2 群 × 2 条非空
    snap = e.snapshot()
    assert len(snap.episodes) == 4
    assert all(rec.kind == "episode" for rec in snap.episodes)
    assert all(0.0 < rec.salience <= 0.4 for rec in snap.episodes)
    assert all("text" in rec.content and "group_id" in rec.content for rec in snap.episodes)


def test_hydrate_bounded_by_max_episodes():
    e = MemoryEngine()
    n = e.hydrate(_FakeCM({1, 2, 3, 4, 5, 6}), max_episodes=6)
    assert n == 6
    assert len(e.snapshot().episodes) == 6


def test_hydrate_fail_soft_on_bad_source():
    class _Bad:
        def group_recent_ids(self): raise RuntimeError("boom")
        def get_recent_group_context(self, gid, limit=12): return []
    e = MemoryEngine()
    n = e.hydrate(_Bad())
    assert n == 0           # 不抛, 隔离
