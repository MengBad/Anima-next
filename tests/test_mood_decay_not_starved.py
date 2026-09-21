"""情绪衰减被心跳饿死 —— 修复回归（2026-09-21）。

背景（上线后由新增的「情绪触发：」日志当场暴露）
------------------------------------------------
重启 1 分钟内日志即打出，且**无任何消息**：

    情绪触发：mentioned intensity=0.05 dv=+0.000 da=+0.005 -> label=annoyed

根因链（全部源码可证，**既有 bug**）：

1. `core/autonomy.py` 的 `_life_checkpoints()` 每心跳 tick 跑一次，
   `autonomy.heartbeat_tick_sec` 默认 **30.0s**；
2. 其中 `mood.trigger("mentioned", 0.05)` —— 注释写「轻微的时间影响」，
   却借用了 `mentioned`（语义 = 被@）这个事件名；
3. `cognitive/mood.py` 的 `trigger()` 在每次调用后**无条件**
   `self._state.last_updated = time.time()`；
4. `_apply_decay()` 要求 `elapsed >= DECAY_INTERVAL(1800s)` 才回归。

⇒ 心跳每 30s 重置时钟 ⇒ `elapsed` 恒 ≈30s ⇒ **30 分钟衰减从未执行**。
实测 `now - last_updated = 16.0s`（阈值 1800）。后果：`valence` 永不回归
（`-0.754` 够不到 `|v|>0.8` 的饱和分支），情绪永久钉死在 `annoyed`。

修复：心跳不再借 `trigger("mentioned")`，改调 `mood.touch_time()` ——
只推进衰减时钟，**不重置 `last_updated`、不注入 `mentioned` 唤起**。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

from anima.cognitive import mood as mood_mod
from anima.cognitive.mood import MoodState, MoodSystem, _compute_label

_ROOT = Path(__file__).resolve().parents[1]
_AUTONOMY_SRC = (_ROOT / "anima" / "core" / "autonomy.py").read_text(encoding="utf-8-sig")
_MOOD_SRC = (_ROOT / "anima" / "cognitive" / "mood.py").read_text(encoding="utf-8-sig")


def _code(src: str) -> str:
    """剥注释后再断言 —— 解释性注释会把源码守卫变成假红/假绿。"""
    return "\n".join(ln.split("#")[0] for ln in src.splitlines())


@pytest.fixture(autouse=True)
def _neutralize_time_of_day(monkeypatch):
    """把昼夜节律调整钉成 0 —— 否则本文件会**随真实墙钟红绿**。

    `_time_based_adjustment()` 读的是真实时间（`datetime.now(TZ)`），**不是**
    `time.time()`；而 `trigger()` 每次都会把它叠加到 valence 上
    （`valence_delta += time_v * 0.1`）。下午（12–14 点）是 `(0.05, -0.15)`，
    即每次 trigger 白送 `+0.005 valence` ⇒ 60 次心跳 = **+0.3**。

    首次运行（11:5x，hour=11 → delta 0）通过，第二轮跨过正午边界后
    `-0.754 → -0.454` 才红 —— 典型的"随墙钟漂移"型假绿/假红。测试必须解耦墙钟。
    """
    monkeypatch.setattr(mood_mod, "_time_based_adjustment", lambda: (0.0, 0.0))


def _make(tmp_path, *, valence: float, arousal: float, last_updated: float) -> MoodSystem:
    ms = MoodSystem(str(tmp_path))
    ms._state = MoodState(
        valence=valence,
        arousal=arousal,
        label=_compute_label(valence, arousal),
        last_updated=last_updated,
    )
    return ms


# ----------------------------------------------------------------------
# 1. 接口存在
# ----------------------------------------------------------------------


def test_touch_time_exists():
    assert hasattr(MoodSystem, "touch_time"), "心跳需要一个不重置衰减时钟的入口"


# ----------------------------------------------------------------------
# 2. touch_time 不重置时钟 —— 这正是修复的核心
# ----------------------------------------------------------------------


def test_touch_time_does_not_reset_clock_before_interval(tmp_path):
    """间隔未到：什么都不该发生，尤其**不能**把 last_updated 刷成 now。"""
    now = time.time()
    past = now - 100.0  # < 1800
    ms = _make(tmp_path, valence=-0.5, arousal=0.9, last_updated=past)

    ms.touch_time()

    # 🔴 必须显式给 abs：Unix 时间戳用 approx 默认 rel=1e-6 会得到 ~1790s 容差，
    #    600s 的漂移照样通过 ⇒ 断言形同虚设（2026-09-21 RED 阶段实测发现）。
    assert ms._state.last_updated == pytest.approx(past, abs=1.0), "touch_time 不得重置衰减时钟"
    assert ms._state.valence == pytest.approx(-0.5), "间隔未到不应回归"


def test_touch_time_never_adds_arousal(tmp_path):
    """旧实现借 `mentioned` 调 `trigger`，每 30s 白送 arousal +0.005。"""
    now = time.time()
    ms = _make(tmp_path, valence=-0.2, arousal=0.5, last_updated=now)

    for _ in range(60):  # 30 分钟的心跳
        ms.touch_time()

    assert ms._state.arousal == pytest.approx(0.5), "touch_time 不应注入 mentioned 唤起"


# ----------------------------------------------------------------------
# 3. 衰减真的能跑起来
# ----------------------------------------------------------------------


def test_touch_time_lets_decay_run_after_interval(tmp_path):
    now = time.time()
    ms = _make(tmp_path, valence=-0.754, arousal=0.92, last_updated=now - 1801.0)

    ms.touch_time()

    # DECAY_RATE = 0.15 ⇒ valence *= 0.85
    assert ms._state.valence == pytest.approx(-0.754 * 0.85, abs=1e-6), "间隔到点应回归一次"
    assert ms._state.last_updated == pytest.approx(now, abs=2.0)
    assert ms._state.label == _compute_label(ms._state.valence, ms._state.arousal)


def test_decay_is_persisted(tmp_path):
    """回归后要落盘，否则 mood.json 永远显示旧值（可观测性失效）。"""
    now = time.time()
    ms = _make(tmp_path, valence=-0.754, arousal=0.92, last_updated=now - 1801.0)

    ms.touch_time()

    raw = (tmp_path / "mood.json").read_text(encoding="utf-8")
    assert "-0.641" in raw or "-0.64" in raw, f"衰减结果未落盘: {raw}"


# ----------------------------------------------------------------------
# 4. 🔴 回归守卫：心跳 30s × 60 次，valence 必须动
# ----------------------------------------------------------------------


def test_heartbeat_does_not_starve_decay(tmp_path, monkeypatch):
    """模拟 30 分钟心跳：旧实现 valence 纹丝不动，新实现至少回归一次。"""
    clock = {"t": 1_800_000_000.0}
    monkeypatch.setattr(mood_mod.time, "time", lambda: clock["t"])

    ms = _make(tmp_path, valence=-0.754, arousal=0.92, last_updated=clock["t"])
    for _ in range(60):  # 60 tick × 30s = 1800s
        clock["t"] += 30.0
        ms.touch_time()

    assert abs(ms._state.valence) < 0.754, (
        f"30 分钟后 valence 仍为 {ms._state.valence} ⇒ 衰减被饿死"
    )


def test_event_trigger_no_longer_starves_decay(tmp_path, monkeypatch):
    """🔴 Round 6 · 第二重饿死：**真实事件**密集时，衰减仍必须执行。

    2026-09-21 实测：重启后 12:29:44 → 12:58:11 = **1707s < 1800**，衰减一次都
    没跑到。根因不是心跳（那条已在 Round 5 修掉），而是 `trigger()` 末尾仍
    **无条件**重置衰减时钟 ⇒ 任何事件（哪怕 intensity=0.05）都把 30 分钟窗口清零。

    ⚠️ 本用例原为 `test_old_heartbeat_path_is_the_starvation`，当时断言
    valence 恒为 -0.754（**钉住 bug**）；Round 6 修复后断言必须反转。
    """
    clock = {"t": 1_800_000_000.0}
    monkeypatch.setattr(mood_mod.time, "time", lambda: clock["t"])

    ms = _make(tmp_path, valence=-0.754, arousal=0.92, last_updated=clock["t"])
    for _ in range(120):  # 60 分钟，每 30s 一次真实事件
        clock["t"] += 30.0
        ms.trigger("mentioned", 0.05)

    # `mentioned` 不注入 valence ⇒ 只要衰减跑过，valence 必已回归。
    # 60 分钟 = 2 个衰减周期 ⇒ -0.754 × 0.85² = -0.545
    assert ms._state.valence == pytest.approx(-0.754 * 0.85 ** 2, abs=1e-3), (
        f"60 分钟 / 2 周期后应为 -0.545，实为 {ms._state.valence} ⇒ 衰减被事件饿死"
    )


# ----------------------------------------------------------------------
# 5. 源码级守卫：心跳接线必须改道
# ----------------------------------------------------------------------


def test_autonomy_uses_touch_time_not_trigger_mentioned():
    code = _code(_AUTONOMY_SRC)
    assert 'trigger("mentioned"' not in code, "心跳仍在借 mentioned 事件（会饿死衰减）"
    assert "touch_time()" in code, "心跳未接到 touch_time()"


def test_decay_interval_is_still_thirty_minutes():
    assert MoodSystem.DECAY_INTERVAL == 1800
    assert MoodSystem.DECAY_RATE == 0.15


# ----------------------------------------------------------------------
# 6. 🔴 Round 6：衰减时钟与事件时钟解耦（第二重饿死）
# ----------------------------------------------------------------------


def test_trigger_does_not_touch_decay_clock(tmp_path):
    """`trigger()` 不得再写衰减时钟 —— 事件只改 valence/arousal。"""
    now = time.time()
    past = now - 600.0  # < 1800：间隔未到
    ms = _make(tmp_path, valence=-0.3, arousal=0.4, last_updated=past)

    ms.trigger("praised", 1.0)

    # abs 必须显式给 —— 见上文 approx 相对容差陷阱。
    assert ms._state.last_updated == pytest.approx(past, abs=1.0), (
        "trigger 仍在重置衰减时钟 ⇒ 事件会把 30 分钟窗口清零（第二重饿死）"
    )


def test_only_apply_decay_writes_the_clock():
    """源码级：衰减时钟的写入点必须唯一（只在 `_apply_decay` 内）。"""
    n = _code(_MOOD_SRC).count("self._state.last_updated =")
    assert n == 1, f"衰减时钟应有且仅有 1 个写入点，实为 {n} 处"


def test_decay_is_logged(tmp_path, caplog):
    """衰减必须可观测 —— 上轮教训：无日志就等于不可见。"""
    now = time.time()
    ms = _make(tmp_path, valence=-0.754, arousal=0.92, last_updated=now - 1801.0)

    with caplog.at_level(logging.INFO, logger="anima.cognitive.mood"):
        ms.touch_time()

    assert "情绪衰减" in caplog.text, f"衰减未打日志: {caplog.text!r}"


def test_restore_persists_decay(tmp_path):
    """启动时的补衰减必须**落盘** —— 否则 mood.json 显示旧值，可观测性失效。

    （本轮诊断就被这一点限制过：只能读 mood.json 快照，而快照可能滞后。）
    """
    import json

    now = time.time()
    (tmp_path / "mood.json").write_text(
        json.dumps({
            "valence": -0.754, "arousal": 0.92, "label": "annoyed",
            "last_updated": now - 1801.0,
        }),
        encoding="utf-8",
    )

    ms = MoodSystem(str(tmp_path))

    assert ms._state.valence == pytest.approx(-0.754 * 0.85, abs=1e-6), "启动补衰减未执行"
    raw = (tmp_path / "mood.json").read_text(encoding="utf-8")
    assert "-0.641" in raw or "-0.64" in raw, f"启动补衰减未落盘: {raw}"
