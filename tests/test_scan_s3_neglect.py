# -*- coding: utf-8 -*-
"""S3 冷落信号：阈值/群门控/冷却/mood 温和低落/不碰信任与硬事实。"""
import io

from anima.cognitive.inner_state.neglect import (
    COOLDOWN_SEC,
    N_STREAK,
    apply_neglect,
    maybe_neglect_dampen,
)


def test_pure_gate_threshold_group_cooldown():
    last = {}
    # 未达阈值(ignored=3, engaged=0, streak=3 < 4)
    assert maybe_neglect_dampen(1, {"engaged": 0, "ignored": 3}, True,
                                now=1000.0, last_applied=last) is False
    # 达阈值且群允许 → True
    assert maybe_neglect_dampen(1, {"engaged": 0, "ignored": 4}, True,
                                now=1000.0, last_applied=last) is True
    # 冷却内不重复施加
    assert maybe_neglect_dampen(1, {"engaged": 0, "ignored": 6}, True,
                                now=1000.0 + COOLDOWN_SEC - 1, last_applied=last) is False
    # 冷却过后可再施加
    assert maybe_neglect_dampen(1, {"engaged": 0, "ignored": 8}, True,
                                now=1000.0 + COOLDOWN_SEC + 1, last_applied=last) is True
    # 已降频/收手的群：即使计数爆表也不施加（子判断3）
    assert maybe_neglect_dampen(9, {"engaged": 0, "ignored": 99}, False,
                                now=2000.0, last_applied={}) is False


def test_apply_neglect_touches_mood_only(tmp_path, monkeypatch):
    from types import SimpleNamespace
    fired = []
    import anima.cognitive.mood as mood_mod
    monkeypatch.setattr(mood_mod, "get_mood_system",
                        lambda dd: SimpleNamespace(
                            trigger=lambda ev, inten: fired.append((ev, inten))))
    autonomy = SimpleNamespace(
        _feedback_stats=lambda g: {"engaged": 0, "ignored": N_STREAK},
        _feedback_allows_group=lambda g: True,
    )
    core = SimpleNamespace(autonomy=autonomy,
                           config=SimpleNamespace(
                               persistence=SimpleNamespace(data_dir=str(tmp_path))),
                           audit=None)
    assert apply_neglect(core, 5, now=3000.0) is True
    assert fired and fired[0][0] == "ignored"        # 只碰 mood
    assert 0 < fired[0][1] <= 0.2                    # 温和强度
    # 不写 hard_facts / 不留 scar 痕迹（目录里没有硬事实文件）
    import os
    assert not os.path.exists(str(tmp_path) + "/hard_facts.json")
    # 冷却内第二次不重复
    assert apply_neglect(core, 5, now=3000.0 + 60) is False


def test_autonomy_wiring_contract():
    src = io.open("anima/core/autonomy.py", encoding="utf-8").read()
    assert "from anima.cognitive.inner_state.neglect import apply_neglect" in src
    assert "apply_neglect(self.core, group_id, now=now)" in src