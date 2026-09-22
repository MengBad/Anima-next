# -*- coding: utf-8 -*-
"""MemoryEngine.hydrate (M1.2) tests —— 从 chat_memory 有界水合, 词汇稳定/fail-soft。"""
from anima.constitution.memory_engine import MemoryEngine


class _FakeCM:
    """注入式 chat_memory 适配(隔离, 不碰全局单例)。

    🔴 条目键必须与 ``chat_memory.get_recent_group_context`` 的**真实返回**一致：
    只有 ``{"role", "content"}``。2026-09-22 前这里用 ``text``（生产不存在的键），
    与实物分叉 ⇒ hydrate 在生产恒空而本文件全绿（假绿 6 次开机）。
    契约守卫见 ``tests/test_memory_hydrate_source_contract.py``。
    """
    def __init__(self, groups):
        self._groups = groups
    def group_recent_ids(self):
        return set(self._groups)
    def get_recent_group_context(self, gid, limit=12):
        return [
            {"role": "user", "content": "hello group " + str(gid)},
            {"role": "user", "content": "second line"},
            {"role": "user", "content": ""},   # 空文本应跳过
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
