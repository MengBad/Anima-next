"""A1 欲望深化（队列③）：最强欲望优先选题 + 新鲜事件加权 + 表达冷却。"""
import time

import pytest

from anima.cognitive.desire import Desire, DesireSystem
from anima.core.autonomy import _desire_event_match, _select_desire_topic


NOW = 1_000_000.0


def _mk(content, intensity, age_sec=0.0, scope="group", group=1):
    d = Desire(content, intensity=intensity, expression_scope=scope,
               source_group_id=group)
    d.created_at = NOW - age_sec
    return d


class _FakeDesireSystem:
    """与真实 get_expressible 同语义的最小替身：作用域过滤 + 强度门槛。"""

    def __init__(self, desires):
        self.desires = desires

    def get_expressible(self, group_id, min_intensity=0.55):
        return [
            d for d in self.desires
            if d.intensity >= min_intensity
            and (d.expression_scope == "global"
                 or (d.expression_scope == "group" and d.source_group_id == group_id))
        ]


def test_picks_strongest_desire():
    weaker = _mk("想聊聊新出的动画", 0.6)
    stronger = _mk("想吃火锅", 0.9)
    got = _select_desire_topic(_FakeDesireSystem([weaker, stronger]), 1, [], NOW)
    assert got is stronger


def test_freshness_breaks_tie():
    old = _mk("想聊聊天气", 0.70, age_sec=7200)
    fresh = _mk("想聊聊刚出炉的新闻", 0.70, age_sec=60)
    got = _select_desire_topic(
        _FakeDesireSystem([old, fresh]), 1, [], NOW, freshness_window_sec=1800)
    assert got is fresh


def test_event_match_boost_flips_ranking():
    weaker = _mk("火锅", 0.62)
    stronger = _mk("完全不相关的话题", 0.70)
    history = [{"role": "user", "content": "今晚谁一起去吃火锅啊"}]
    got = _select_desire_topic(
        _FakeDesireSystem([stronger, weaker]), 1, history, NOW,
        event_match_bonus=0.2)
    assert got is weaker


def test_repeat_cooldown_excludes_expressed():
    d = _mk("想安利这首歌", 0.9)
    expressed = [("想安利这首歌", NOW - 600)]
    got = _select_desire_topic(
        _FakeDesireSystem([d]), 1, [], NOW, expressed=expressed)
    assert got is None


def test_cooldown_expires():
    d = _mk("想安利这首歌", 0.9)
    expressed = [("想安利这首歌", NOW - 7200)]
    got = _select_desire_topic(
        _FakeDesireSystem([d]), 1, [], NOW,
        repeat_cooldown_sec=3600, expressed=expressed)
    assert got is d


def test_no_candidates_returns_none():
    assert _select_desire_topic(_FakeDesireSystem([]), 1, [], NOW) is None
    weak = _mk("太弱了", 0.4)
    assert _select_desire_topic(_FakeDesireSystem([weak]), 1, [], NOW) is None


def test_expressible_scope_respected():
    """internal 欲望绝不能被选为群内主动发言选题。"""
    internal = _mk("只放在心里的心事", 0.95, scope="internal")
    got = _select_desire_topic(_FakeDesireSystem([internal]), 1, [], NOW)
    assert got is None


def test_event_match_chinese_2gram():
    assert _desire_event_match("想吃火锅", "今晚去吃火锅吗")
    assert not _desire_event_match("想吃火锅", "今天天气不错")
    assert not _desire_event_match("", "随便什么")


def test_mark_expressed_decays_and_persists(tmp_path):
    ds = DesireSystem(data_dir=str(tmp_path))
    d = ds.add("想去海边", intensity=0.8, source_group_id=1,
               expression_scope="group")
    ds.mark_expressed(d)
    assert d.intensity == pytest.approx(0.8 * 0.75)
    again = DesireSystem(data_dir=str(tmp_path))
    again.restore()
    assert any(abs(x.intensity - 0.6) < 1e-6 for x in again.desires)
