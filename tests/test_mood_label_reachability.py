"""E1 守卫：情绪 label 可达性（两层判据）。

历史缺陷（2026-10-16 定位）：
    ``_VALENCE_POSITIVE = 0.15`` 是中性边界，但单次最强负向事件的 delta 只有
    ``insulted`` 生产强度 0.4 → ``-0.3 * 0.4 = -0.12``，**差 0.03 永远跨不过去**；
    ``annoyed`` 还要求 ``arousal > 0.5`` 而单次只给 ``+0.06``。
    叠加 ``DECAY_RATE=0.15``（30 分钟向中性回归 15%）、时间调整被 ``* 0.1`` 稀释，
    结果：生产 7 天 452 次判定中 ``neutral`` **410 次（90.7%）**、
    ``annoyed`` / ``tired`` / ``bored`` **各 0 次**。
    而 ``_MOOD_DESCRIPTIONS["neutral"] == ""`` 且 ``prompt_fragment()`` 对空描述
    ``return ""`` ⇒ **91% 的回合情绪片段根本不进 system prompt** ⇒ 情绪系统事实失效。

⇒ 教训：带阈值的分支必须验证「**合法输入下可达**」，光看到分支存在不算数。
   （静默失效第 ④ 类：阈值使分支实际不可达）

两层判据，缺一不可：
  ① 结构性可达 —— **存在**一组 (事件, 强度, 次数) 能到达该 label，防死枚举
  ② 生产强度可达 —— 用调用点真实强度，防「阈值与 delta 量级失衡」（本次 E1）

实现说明：探针复用单个 MoodSystem 实例并禁用落盘（``_save`` 置空），
避免 tempdir/IO 开销 —— 首版每次新建实例导致跑 271s。
"""
from __future__ import annotations

import os
import pathlib
import re
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anima.cognitive.mood import (  # noqa: E402
    _MOOD_DESCRIPTIONS,
    _MOOD_LABELS,
    _VALENCE_POSITIVE,
    _AROUSAL_HIGH,
    MoodSystem,
)

# ── 生产调用点的真实强度（改动时必须回来核对来源行）──
# generation.py:1177  trigger("praised", 0.3)
# generation.py:1179  trigger("insulted", 0.4)
# generation.py:1182  trigger("ignored", 0.15)
# generation.py:1188  trigger("helped", 0.2)
# cognition.py:863    trigger("interesting", 0.15)
# handler.py:954      trigger("overwhelmed", 0.5)
PROD = {
    "praised": 0.3,
    "insulted": 0.4,
    "ignored": 0.15,
    "helped": 0.2,
    "interesting": 0.15,
    "overwhelmed": 0.5,
}

# bored **在 _MOOD_LABELS 里没有区间映射** ⇒ _compute_label() 永不返回它。
# 但它被 4 处生产代码消费（handler.py:2350 "bored":"speechless" 表情映射、
# reply_splitter.py:269 权重 1.3、guardrail.py:847 预算 120、autonomy.py:1482 分支）
# ⇒ 这 4 个消费分支永不可达（"算了却没人用"的反面：消费者在等一个不存在的值）。
# 本轮**不改**（消费者都把 tired/bored 并列处理，tired 已覆盖功能），仅显式声明。
NO_MAPPING = {"bored"}

_ALL_EVENTS = ["praised", "insulted", "interesting", "ignored", "helped",
               "overwhelmed", "failed", "mentioned", "boring"]
_PROBE_INTENSITIES = [0.15, 0.2, 0.3, 0.4, 0.5, 0.7, 1.0]


class _Probe:
    """复用单实例 + 禁用落盘的探针。"""

    def __init__(self):
        self._td = tempfile.TemporaryDirectory()
        self.ms = MoodSystem(self._td.name)
        self.ms._save = lambda: None          # 只做内存计算，避免每步写盘
        self._cache: dict | None = None

    def close(self):
        self._td.cleanup()

    def _reset(self):
        self.ms._state.valence = 0.0
        self.ms._state.arousal = 0.3
        self.ms._state.last_updated = time.time()

    def reaches(self, target, events, repeat=1):
        """指定序列能否到达 target。"""
        self._reset()
        for _ in range(repeat):
            for ev, inten in events:
                self.ms.trigger(ev, inten)
                if self.ms.get_state().label == target:
                    return True
        return False

    def reachable_labels(self):
        """穷举 (事件, 强度, 次数) 三元组，收集**所有**能被到达的 label。"""
        if self._cache is not None:
            return self._cache
        reached = set()
        for ev in _ALL_EVENTS:
            for inten in _PROBE_INTENSITIES:
                for rep in range(1, 5):
                    self._reset()
                    for _ in range(rep):
                        self.ms.trigger(ev, inten)
                    reached.add(self.ms.get_state().label)
        self._cache = reached
        return reached


@pytest.fixture(scope="module")
def probe():
    p = _Probe()
    yield p
    p.close()


def test_neutral_is_not_injected():
    """基线事实：neutral 描述为空 ⇒ 不注入（刻意设计，改它需 owner 重新裁定）。"""
    assert _MOOD_DESCRIPTIONS.get("neutral") == "", (
        "neutral 由空描述变为非空 ⇒ 每回合都会注入情绪噪音"
    )


def test_mood_tables_key_consistent():
    """`_MOOD_LABELS` 的取值集合必须与 `_MOOD_DESCRIPTIONS` 的键一致（白名单除外）。

    历史形态：`bored` 有描述文案、有 4 处消费者，但**区间表里没有它** ⇒ 永不产生。
    """
    produced = set(_MOOD_LABELS.values())
    described = set(_MOOD_DESCRIPTIONS)
    unmapped = described - produced - {"neutral"}
    assert unmapped == NO_MAPPING, (
        "无区间映射的 label 与白名单不符：实际 %s / 声明 %s\n"
        "若是有意为之请更新 NO_MAPPING；若是新漏项请补 _MOOD_LABELS 区间。"
        % (sorted(unmapped), sorted(NO_MAPPING))
    )
    assert produced <= described, (
        "区间表产出 %s 但描述表没有对应文案 ⇒ prompt 注入会取到空串"
        % sorted(produced - described)
    )


def test_every_label_structurally_reachable(probe):
    """① 结构性可达：每个有映射的 label 都必须能被**某一组合法参数**到达。

    防「_MOOD_LABELS 区间」与「_MOOD_DESCRIPTIONS 键」不匹配造成的死枚举。
    """
    expected = set(_MOOD_DESCRIPTIONS) - {"neutral"} - NO_MAPPING
    reached = probe.reachable_labels()
    missing = expected - reached
    assert not missing, (
        "以下 label 用任何 (事件, 强度, 1~4 次) 组合都达不到 ⇒ 区间与枚举不匹配：%s\n"
        "已可达：%s" % (sorted(missing), sorted(reached))
    )


@pytest.mark.parametrize(
    "label,events,repeat",
    [
        # 连续被骂两次就该烦躁 —— "被骂了还不为所动"是缺陷，不是克制。
        # 现状需 4 次（arousal 门槛 0.5 而单次只给 +0.06）⇒ 7 天实测 annoyed 0 次
        ("annoyed", [("insulted", PROD["insulted"])], 2),
        # 被夸够次数应当高兴
        ("happy", [("praised", PROD["praised"])], 2),
    ],
)
def test_label_reachable_at_production_intensity(probe, label, events, repeat):
    """② 生产强度可达：关键 label 必须能被**调用点真实强度**推到。

    历史形态：annoyed 需 valence < -0.15 **且** arousal > 0.5，
    而 insulted(0.4) 只给 (-0.12, +0.06) ⇒ 生产 7 天 0 次命中、
    90.7% 停 neutral（neutral 不注入）。
    """
    assert probe.reaches(label, events, repeat), (
        "label=%s 在 %d 轮生产强度 %s 内不可达 ⇒ 阈值与 delta 量级失衡"
        "（生产实测 90.7%% 回合停在 neutral，而 neutral 不注入）" % (label, repeat, events)
    )


def test_mood_boundaries_positive():
    """边界值必须为正（防手滑改成 0/负导致象限坍缩）。"""
    assert _VALENCE_POSITIVE > 0
    assert _AROUSAL_HIGH > 0
    assert _VALENCE_POSITIVE < _AROUSAL_HIGH, "价态边界不应大于唤醒边界"


def test_boring_event_is_not_wired():
    """bored 无事件源 —— 与 NO_MAPPING 双保险：既无区间、也无事件。

    若将来给 boring 接了线，本用例失败并提醒复核 bored 的可达性设计。
    """
    root = pathlib.Path(__file__).resolve().parents[1]
    hits = []
    for p in (root / "anima").rglob("*.py"):
        if p.name == "mood.py":          # mood.py 内的 on_boring 是定义，不算生产接线
            continue
        src = p.read_text(encoding="utf-8", errors="replace")
        if re.search(r'trigger\(\s*"boring"', src):
            hits.append(str(p.relative_to(root)))
    assert not hits, (
        "anima/ 里已出现 trigger(\"boring\") 调用点（%s）⇒ 请复核 bored："
        "它目前无区间映射，接了事件也仍不可达" % hits
    )
