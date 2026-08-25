# -*- coding: utf-8 -*-
"""开机宪法记忆水合(boot wiring)—— 只读/有界/fail-soft 机制。"""

from anima.constitution.memory_authority import MemoryAuthority


class _FakeCM:
    def group_recent_ids(self):
        return ['g1', 'g2']

    def get_recent_group_context(self, gid, limit):
        return [{'role': 'user', 'text': '会话消息 %d' % i} for i in range(30)]


def test_boot_hydrate_bounded_and_readonly():
    auth = MemoryAuthority(hydrate_now=True, cm=_FakeCM())
    eps = auth.snapshot_memory().episodes
    assert 0 < len(eps) <= 24
    assert all(e.kind == 'episode' and e.content.get('text') for e in eps)
    # 只读面, 不得暴露写面: snapshot 无 ingest_* 方法
    assert not hasattr(auth.snapshot_memory(), 'ingest_episode')


def test_boot_authority_constructs_even_when_source_raises():
    class _BadCM(_FakeCM):
        def group_recent_ids(self):
            raise RuntimeError('source down')

    auth = MemoryAuthority(hydrate_now=True, cm=_BadCM())
    # fail-soft: 构造不抛, 水合隔离为 0
    assert len(auth.snapshot_memory().episodes) == 0


def test_periodic_dedupe_no_flood():
    # 周期重水合(dedupe=True)同一现场, 二次不再重复入 episode
    detail = []

    class _CM(_FakeCM):
        def group_recent_ids(self):
            return ['g1']

        def get_recent_group_context(self, gid, limit):
            return [{'role': 'user', 'text': '重复的消息'}]

    auth = MemoryAuthority()
    first = auth.hydrate_from_chat(cm=_CM(), dedupe=True)
    second = auth.hydrate_from_chat(cm=_CM(), force=True, dedupe=True)
    assert first == 1 and second == 0
    assert len(auth.snapshot_memory().episodes) == 1
