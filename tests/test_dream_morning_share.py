"""A3 早晨分享梦境测试。

覆盖：
- _decide_morning_dream_share 纯函数（08:00 命中 / 非 8 点 / 当天已尝试 / 10% 概率分布）
- DreamEngine.get_last_dream 读取最后一条
- DreamEngine.apply_dream_mood_to_soul 把 mood_tag 映射情绪写入 soul.json
"""
import json
import random

import pytest

from anima.core.autonomy import _decide_morning_dream_share
from anima.cognitive.dream_engine import DreamEngine, mood_tag_to_emotion


class _FakeRng:
    """可注入固定 random() 的假 rng（SecretInjector / 决策函数共用）。"""

    def __init__(self, value: float):
        self._value = value

    def random(self) -> float:
        return self._value


def test_decide_share_at_8_with_low_roll():
    # 抽中（<0.1）：应当分享，且未标记「已尝试」
    should, attempted = _decide_morning_dream_share(8, "", "2026-07-30", rng=_FakeRng(0.05))
    assert should is True
    assert attempted is False


def test_decide_no_share_at_8_with_high_roll():
    # 没抽中（>=0.1）：不分享，但标记今天已尝试（避免每 tick 重复抽）
    should, attempted = _decide_morning_dream_share(8, "", "2026-07-30", rng=_FakeRng(0.5))
    assert should is False
    assert attempted is True


def test_decide_no_share_when_not_8():
    # 非 8 点：完全不分享、也不尝试
    should, attempted = _decide_morning_dream_share(10, "", "2026-07-30")
    assert should is False
    assert attempted is False


def test_decide_no_share_when_already_tried_today():
    # 当天已尝试过：即使 8 点也不重复
    should, attempted = _decide_morning_dream_share(8, "2026-07-30", "2026-07-30")
    assert should is False
    assert attempted is False


def test_decide_share_distribution():
    # 10% 概率：大样本下命中率应落在 ~[0.07, 0.13]
    rng = random.Random(20260730)
    n = 4000
    hits = sum(
        1 for _ in range(n)
        if _decide_morning_dream_share(8, "", "2026-07-30", rng=rng)[0]
    )
    ratio = hits / n
    assert 0.07 <= ratio <= 0.13, f"命中率 {ratio} 偏离 10%"


def test_get_last_dream(tmp_path):
    p = tmp_path / "dreams.jsonl"
    p.write_text(
        json.dumps({"dream": "a", "mood_tag": "happy", "timestamp": 1}) + "\n"
        + json.dumps({"dream": "b", "mood_tag": "calm", "timestamp": 2}) + "\n",
        encoding="utf-8",
    )
    eng = DreamEngine(str(tmp_path))
    last = eng.get_last_dream()
    assert last is not None
    assert last["dream"] == "b"
    assert last["mood_tag"] == "calm"


def test_get_last_dream_empty(tmp_path):
    eng = DreamEngine(str(tmp_path))
    assert eng.get_last_dream() is None


def test_apply_dream_mood_to_soul(tmp_path):
    # mood_tag(weird) -> emotion(happy)，写入 soul.json
    (tmp_path / "soul.json").write_text(
        json.dumps({"emotion": "sad"}), encoding="utf-8"
    )
    eng = DreamEngine(str(tmp_path))
    assert eng.apply_dream_mood_to_soul("weird") is True
    data = json.loads((tmp_path / "soul.json").read_text(encoding="utf-8"))
    assert data["emotion"] == mood_tag_to_emotion("weird")


def test_apply_dream_mood_to_soul_no_soul_file(tmp_path):
    # 无 soul.json 时安全返回 False（不创建、不报错）
    eng = DreamEngine(str(tmp_path))
    assert eng.apply_dream_mood_to_soul("calm") is False
    assert not (tmp_path / "soul.json").exists()
