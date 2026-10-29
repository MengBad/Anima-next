"""记忆三级可见性（design/private_group_dual_mode_plan.md §2）。"""
from __future__ import annotations

import pytest

from anima.memory import visibility as vis


def test_normalize_unknown_falls_back_to_public():
    assert vis.normalize("private") == vis.PRIVATE
    assert vis.normalize("  SEALED ") == vis.SEALED
    assert vis.normalize("whatever") == vis.PUBLIC
    assert vis.normalize(None) == vis.PUBLIC


def test_channel_of_maps_detail_type():
    assert vis.channel_of("group") == vis.CHANNEL_GROUP
    assert vis.channel_of("private") == vis.CHANNEL_PRIVATE
    assert vis.channel_of(None) == vis.CHANNEL_PRIVATE


@pytest.mark.parametrize("channel,expected", [
    (vis.CHANNEL_GROUP, True),
    (vis.CHANNEL_PRIVATE, True),
])
def test_public_admitted_everywhere(channel, expected):
    assert vis.admit(vis.PUBLIC, channel) is expected


def test_private_only_in_private_channel():
    assert vis.admit(vis.PRIVATE, vis.CHANNEL_PRIVATE) is True
    assert vis.admit(vis.PRIVATE, vis.CHANNEL_GROUP) is False


def test_sealed_never_injected():
    assert vis.admit(vis.SEALED, vis.CHANNEL_PRIVATE) is False
    assert vis.admit(vis.SEALED, vis.CHANNEL_GROUP) is False


def test_missing_visibility_defaults_to_public():
    # 旧记录没有该字段 ⇒ 行为与从前一致（零迁移）
    assert vis.admit(None, vis.CHANNEL_GROUP) is True
    assert vis.admit("", vis.CHANNEL_GROUP) is True


class _StubMemory:
    """最小桩：只验证 search 的可见性过滤，不碰 sqlite。"""

    def __init__(self, rows):
        self._rows = rows

    async def _search_raw(self, *a, **kw):
        return list(self._rows)


@pytest.mark.asyncio
async def test_search_filters_private_in_group_channel():
    from anima.memory.vector_memory import VectorMemory

    rows = [
        {"text": "公开的事", "meta": {"visibility": vis.PUBLIC}, "similarity": 0.9},
        {"text": "私聊的事", "meta": {"visibility": vis.PRIVATE}, "similarity": 0.9},
        {"text": "密码", "meta": {"visibility": vis.SEALED}, "similarity": 0.9},
    ]
    stub = _StubMemory(rows)
    # 直接借用 search 的过滤段（不实例化真库）
    grouped = await VectorMemory.search(stub, "q", channel=vis.CHANNEL_GROUP)
    assert [r["text"] for r in grouped] == ["公开的事"]

    private = await VectorMemory.search(stub, "q", channel=vis.CHANNEL_PRIVATE)
    assert [r["text"] for r in private] == ["公开的事", "私聊的事"]

    unfiltered = await VectorMemory.search(stub, "q")
    assert len(unfiltered) == 3
