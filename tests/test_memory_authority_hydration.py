# -*- coding: utf-8 -*-
"""MemoryAuthority 水合接线 (M1.2) tests —— f(Memory) 读面吃现场经验。"""
from anima.constitution.memory_authority import MemoryAuthority


class _FakeCM:
    def __init__(self, groups):
        self._groups = groups
    def group_recent_ids(self):
        return set(self._groups)
    def get_recent_group_context(self, gid, limit=12):
        return [{"role": "user", "text": "hi group " + str(gid)}, {"role": "user", "text": ""}]


def test_default_no_hydrate_backward_compat():
    a = MemoryAuthority()
    assert a.snapshot_memory().episodes == []


def test_hydrate_now_constructor():
    a = MemoryAuthority(hydrate_now=True, cm=_FakeCM({7, 9}))
    assert len(a.snapshot_memory().episodes) == 2


def test_hydrate_lazy_once():
    a = MemoryAuthority()
    assert a.hydrate_from_chat(cm=_FakeCM({1})) == 1
    assert a.hydrate_from_chat(cm=_FakeCM({1})) == 0


def test_hydrate_force_rehydrate():
    a = MemoryAuthority()
    a.hydrate_from_chat(cm=_FakeCM({1}))
    n = a.hydrate_from_chat(cm=_FakeCM({1}), force=True)
    assert n == 1


def test_hydrate_only_read_surface():
    a = MemoryAuthority()
    a.hydrate_from_chat(cm=_FakeCM({1}))
    eps = a.snapshot_memory().episodes
    assert len(eps) == 1 and eps[0].kind == "episode"
