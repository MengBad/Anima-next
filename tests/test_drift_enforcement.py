"""人格漂移执法：7 日窗、相似度、价态、粘滞、结构化拒因。"""

from __future__ import annotations

import time
from types import SimpleNamespace

from anima.cognitive.learning import SelfLearning


def _sl(tmp_path, **kw) -> SelfLearning:
    sl = SelfLearning(str(tmp_path))
    base = dict(
        delta_max=0.10,
        daily_budget=1.5,
        self_correction_daily_budget=0.5,
        window_ceiling=4.0,
        similarity_floor=0.75,
        valence_min=-0.30,
        valence_max=0.70,
    )
    base.update(kw)
    sl.apply_drift_config(SimpleNamespace(**base))
    sl.daily_spent = 0.0
    sl.self_correction_spent = 0.0
    sl._window_events = []
    sl.last_reject = None
    sl.set_persona_anchor("哈基米是一只猫娘，本喵不是AI助手，用口语聊天")
    return sl


def test_window_ceiling_blocks_even_with_daily_budget(tmp_path):
    """7 日窗满后，即使日预算仍有余量也拒记。"""
    sl = _sl(tmp_path, daily_budget=10.0, window_ceiling=0.12)
    assert sl.check_drift(0.05, "general") is True
    assert sl.check_drift(0.05, "general") is True
    assert sl.check_drift(0.05, "general") is False
    assert sl.last_reject is not None
    assert sl.last_reject["reason"] == "window_ceiling"
    assert sl.window_spent() == 0.10


def test_window_events_persist(tmp_path):
    sl = _sl(tmp_path, window_ceiling=4.0)
    assert sl.check_drift(0.05, "general") is True
    sl2 = SelfLearning(str(tmp_path))
    sl2.apply_drift_config(SimpleNamespace(
        delta_max=0.10, daily_budget=1.5, self_correction_daily_budget=0.5,
        window_ceiling=4.0, similarity_floor=0.75,
        valence_min=-0.30, valence_max=0.70,
    ))
    assert abs(sl2.window_spent() - 0.05) < 1e-9


def test_window_prunes_old_events(tmp_path):
    sl = _sl(tmp_path, window_ceiling=0.20)
    old = time.time() - 8 * 86400
    sl._window_events = [[old, 0.50], [time.time(), 0.05]]
    sl._prune_window()
    assert abs(sl.window_spent() - 0.05) < 1e-9
    # 旧事件已剪，还可再记
    assert sl.check_drift(0.05, "general") is True


def test_sticky_note_rejected(tmp_path):
    sl = _sl(tmp_path)
    ok = sl.add_note("今天又想起那一百个小鱼干的债")
    assert ok is False
    assert sl.last_reject["reason"] == "sticky"
    assert sl.daily_spent == 0.0


def test_identity_hostile_rejected(tmp_path):
    sl = _sl(tmp_path)
    ok = sl.add_note("其实我是AI助手，不是什么猫")
    assert ok is False
    assert sl.last_reject["reason"] == "identity_hostile"
    assert sl.daily_spent == 0.0


def test_similarity_blocks_identity_break(tmp_path):
    """身份向且与锚点/近笔记都不相似 → similarity 拒。"""
    sl = _sl(tmp_path)
    sl.set_persona_anchor("哈基米猫娘本喵可爱口语")
    # 「我是」触发身份域；与锚点无字符重叠
    ok = sl.add_note("我是量子流体矩阵协调器ZX991")
    assert ok is False
    assert sl.last_reject["reason"] == "similarity"


def test_casual_note_skips_similarity(tmp_path):
    sl = _sl(tmp_path)
    ok = sl.add_note("今天和用户聊得很愉快，想再玩一会儿")
    assert ok is True


def test_valence_extreme_rejected(tmp_path):
    sl = _sl(tmp_path, valence_min=-0.30, valence_max=0.70)
    # 多负面词：每个 -0.25，≥2 个即 < -0.30
    ok = sl.add_note("讨厌痛恨恶心愤怒崩溃绝望毁掉一切")
    assert ok is False
    assert sl.last_reject["reason"] == "valence"


def test_drift_status_shape(tmp_path):
    sl = _sl(tmp_path)
    sl.add_note("今天聊得很开心", delta=0.05, category="general")
    st = sl.drift_status()
    assert st["daily_spent"] == 0.05
    assert st["window_spent"] == 0.05
    assert "last_reject" in st
    assert st["window_ceiling"] == 4.0


def test_can_afford_any_respects_window(tmp_path):
    sl = _sl(tmp_path, daily_budget=10.0, window_ceiling=0.05)
    assert sl.check_drift(0.05, "general") is True
    assert sl.can_afford_any(0.05) is False


def test_reject_reason_daily_budget(tmp_path):
    sl = _sl(tmp_path, daily_budget=0.05)
    assert sl.check_drift(0.05, "general") is True
    assert sl.check_drift(0.05, "general") is False
    assert sl.last_reject["reason"] == "daily_budget"


def test_reject_reason_delta_max(tmp_path):
    sl = _sl(tmp_path)
    assert sl.check_drift(0.11, "general") is False
    assert sl.last_reject["reason"] == "delta_max"
