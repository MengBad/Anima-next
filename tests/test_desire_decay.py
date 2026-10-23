"""欲望衰减契约（2026-10-23）。

## 修的是什么 bug

原实现：**每 60 秒把 intensity ×= 0.95**，即「按调用次数」衰减，不是按时间。

同一条 bug 造成**两种相反的症状**：

| 情形 | 后果 |
|---|---|
| `get_active()` 高频调用 | 一天内衰减到 0.05 以下（0.95^1440 ≈ 0.0485）<br>而门槛是 0.3 ⇒ **欲望永远无法激活 Drive** |
| 调用频率低 | 几乎不衰减 ⇒ 生产实证「想跟大家聊天」强度 **0.95 挂了 41 天** |

**要么全灭，要么僵尸不灭。** 而「欲望」正是「它想做什么」的源头 ——
衰减错了，整条 `Need → Drive → Goal` 链就喂的是死数据。

## 修法

按**真实经过时间**做指数衰减，半衰期 24 小时：
- 与调用频率**完全无关**（这是关键 —— 原来最致命的地方）；
- 强烈欲望能活几天；弱欲望自然消退；
- 7 天封顶（进程长停机后不一次性抹平，也给它「还记得点什么」的余地）；
- 下限 0.05（低于此不再计算，已是僵尸）。

## 本测试锁死

1. 半衰期语义正确（24h ⇒ 减半）；
2. **调用频率无关**（高频调用不导致归零 —— 这是原 bug 的核心）；
3. 7 天封顶；
4. 已 resolved 的不动；
5. 下限保护。
"""
import time

import pytest

from anima.cognitive.desire import Desire, DesireSystem


def _sys_with(tmp_path, intensity=0.95, content="想跟大家聊天"):
    ds = DesireSystem(str(tmp_path))
    ds.desires = [Desire(content, intensity=intensity)]
    return ds, ds.desires[0]


# ── 1. 半衰期语义 ─────────────────────────────────────────────────────


def test_half_life_24h(tmp_path):
    """24 小时 ⇒ 强度减半。"""
    ds, d = _sys_with(tmp_path)
    ds._last_decay = time.time() - 24 * 3600
    ds.decay()
    assert abs(d.intensity - 0.475) < 0.01, f"24h 应减半，实得 {d.intensity}"


def test_six_hours_decay(tmp_path):
    ds, d = _sys_with(tmp_path)
    ds._last_decay = time.time() - 6 * 3600
    ds.decay()
    # 0.5^(6/24) ≈ 0.8409 ⇒ 0.95 × 0.8409 ≈ 0.799
    assert 0.78 < d.intensity < 0.82, f"6h 应约 0.80，实得 {d.intensity}"


def test_48h_decay(tmp_path):
    ds, d = _sys_with(tmp_path)
    ds._last_decay = time.time() - 48 * 3600
    ds.decay()
    assert abs(d.intensity - 0.2375) < 0.01


# ── 2. 与调用频率无关（原 bug 的核心）────────────────────────────────


def test_decay_independent_of_call_frequency(tmp_path):
    """🔴 高频调用**不得**导致归零 —— 这正是原 bug 最致命的地方。

    原实现每次调用 ×0.95 ⇒ 一天内 1440 次调用把 0.95 打到 0.0485，
    低于 `get_active(0.3)` 门槛 ⇒ 欲望永远无法激活 Drive。
    """
    ds, d = _sys_with(tmp_path)
    # 2000 次 × 1 秒 = 2000 秒 ≈ 33 分钟。按半衰期 24h 算应约 0.935。
    # 关键：**不能归零**（原 bug 会把它打到 0.0485 以下）。
    for _ in range(2000):
        ds._last_decay = time.time() - 1.0
        ds.decay()
    assert d.intensity > 0.90, (
        f"2000 次 1 秒间隔（累计 33 分钟）后强度应仍 >0.90，实得 {d.intensity}"
    )
    assert d.intensity != pytest.approx(ds._DECAY_FLOOR), "不得被 2000 次调用打到下限"


def test_many_calls_equal_one_call_for_same_elapsed(tmp_path, monkeypatch):
    """同样的**总经过时间**，调用 1 次和 100 次结果应一致。

    用 monkeypatch 控制 `time.time()` 推进，避免「反复把 _last_decay 拨回去」
    造成 elapsed 被重复计算（那样测的就不是「累计时间」了）。
    """
    import anima.cognitive.desire as _mod

    base = time.time()
    # 单次：直接跨 12 小时
    monkeypatch.setattr(_mod.time, "time", lambda: base)
    ds1, d1 = _sys_with(tmp_path / "a")
    monkeypatch.setattr(_mod.time, "time", lambda: base + 12 * 3600)
    ds1.decay()
    v1 = d1.intensity

    # 分 100 次，每次前进 432 秒（累计正好 12 小时）
    monkeypatch.setattr(_mod.time, "time", lambda: base)
    ds2, d2 = _sys_with(tmp_path / "b")
    for i in range(1, 101):
        _t = base + i * 432
        monkeypatch.setattr(_mod.time, "time", lambda t=_t: t)
        ds2.decay()
    v2 = d2.intensity

    assert abs(v1 - v2) < 0.02, f"总时间相同结果应一致：{v1} vs {v2}"


# ── 3. 封顶与下限 ─────────────────────────────────────────────────────


def test_seven_day_cap(tmp_path):
    """超长停机（30 天）按 7 天算 —— 不一次性抹平。

    给它留一点「还记得点什么」的余地，也避免长时间停机后
    所有欲望瞬间归零、整条驱动链失去输入。
    """
    ds, d = _sys_with(tmp_path)
    ds._last_decay = time.time() - 30 * 86400
    ds.decay()
    # 7 天 = 7 个半衰期 ⇒ 0.5^7 ≈ 0.0078 ⇒ 撞下限 0.05
    assert d.intensity == pytest.approx(ds._DECAY_FLOOR), (
        f"30 天应封顶到下限，实得 {d.intensity}"
    )


def test_floor_protects_from_zero(tmp_path):
    """永不衰减到 0（0 会让它从列表里消失，无法追溯）。"""
    ds, d = _sys_with(tmp_path, intensity=0.06)
    ds._last_decay = time.time() - 7 * 86400
    ds.decay()
    assert d.intensity >= ds._DECAY_FLOOR


def test_resolved_desires_untouched(tmp_path):
    ds, d = _sys_with(tmp_path, intensity=0.9)
    d.resolved = True
    ds._last_decay = time.time() - 24 * 3600
    ds.decay()
    assert d.intensity == 0.9, "已解决的欲望不该被衰减"


def test_zero_elapsed_is_noop(tmp_path):
    ds, d = _sys_with(tmp_path)
    ds._last_decay = time.time()
    ds.decay()
    assert d.intensity == pytest.approx(0.95, abs=1e-6)


# ── 4. 回归：不再出现「41 天僵尸」────────────────────────────────────


def test_stale_desire_eventually_decays(tmp_path):
    """🔴 生产实证：「想跟大家聊天」强度 0.95 **挂了 41 天**没变。

    原因：`_last_decay` 是内存属性，进程重启即重置为「现在」⇒
    `elapsed` 永远是 0 ⇒ 永不衰减。加上低频调用，僵尸欲望能挂一个多月，
    并持续以 0.95 的优势垄断「它想做什么」（把真实的新欲望全压下去）。
    """
    ds, d = _sys_with(tmp_path, intensity=0.95)
    # 模拟「41 天没被正确衰减」的现场：直接给一个 41 天的 elapsed
    ds._last_decay = time.time() - 41 * 86400
    ds.decay()
    assert d.intensity <= ds._DECAY_FLOOR + 1e-6, (
        f"41 天后的欲望必须衰减到下限，实得 {d.intensity}"
    )


# ── 5. 极短间隔不得产生无意义微衰减（2026-10-23 由 test_danger 抓出）──


def test_sub_second_elapsed_is_noop(tmp_path):
    """🔴 1 秒以内的衰减没有语义，必须跳过。

    有调用方依赖「强度**恰好等于**门槛」的精确值：
    danger 主动追问存 `intensity=0.55`，而 `get_expressible` 门槛也是 0.55。
    指数衰减在 elapsed≈0 时仍乘一个 0.9999999 的因子 ⇒ 0.55 变 0.5499999
    ⇒ 判为未达门槛（`tests/test_danger.py` 抓出）。
    """
    ds, d = _sys_with(tmp_path, intensity=0.55)
    ds._last_decay = time.time() - 0.3      # 0.3 秒
    ds.decay()
    assert d.intensity == 0.55, f"亚秒级衰减不该改变强度，实得 {d.intensity}"


def test_threshold_value_survives_short_interval(tmp_path):
    """恰好等于门槛的强度，经过短间隔后仍应 >= 门槛。"""
    ds, d = _sys_with(tmp_path, intensity=0.55)
    for _ in range(20):
        ds._last_decay = time.time() - 0.1
        ds.decay()
    assert d.intensity >= 0.55, (
        f"20 次亚秒级调用后仍应 >=0.55，实得 {d.intensity}"
    )


# ── 6. 衰减时钟必须持久化（僵尸欲望的真正机制）───────────────────────


def test_decay_clock_persists_across_restart(tmp_path):
    """🔴 `_last_decay` 原来只是内存属性 ⇒ 每次重启重置为「现在」
    ⇒ elapsed=0 ⇒ **永不衰减**。这正是「强度 0.95 的欲望挂了 41 天」
    的真正机制：不是衰减算法错，而是**衰减的时钟每次重启都归零**。
    """
    ds, d = _sys_with(tmp_path, intensity=1.0)
    ds._last_decay = time.time() - 30 * 86400
    ds.decay()
    assert d.intensity <= ds._DECAY_FLOOR + 1e-6
    ds._save_decay_state()

    # 模拟重启：全新实例，从盘上恢复时钟
    ds2 = DesireSystem(str(tmp_path))
    elapsed = time.time() - ds2._last_decay
    assert elapsed < 60, (
        f"重启后时钟应从落盘状态恢复（elapsed 应≈0），实得 {elapsed:.0f}s"
    )


def test_decay_state_file_is_separate(tmp_path):
    """衰减状态独立落盘，不污染 desires.json 的列表结构。"""
    ds, _ = _sys_with(tmp_path)
    ds.decay()
    ds._save_decay_state()
    assert (tmp_path / "desire_decay_state.json").exists()
    # desires.json 仍是欲望列表（不是 dict 包裹状态）
    raw = __import__("json").loads((tmp_path / "desires.json").read_text(encoding="utf-8")) \
        if (tmp_path / "desires.json").exists() else []
    assert isinstance(raw, list), "desires.json 必须仍是欲望列表"


def test_restart_then_continue_decay(tmp_path):
    """停机 2 天后重启，衰减应继续（而不是从零开始）。"""
    ds, d = _sys_with(tmp_path, intensity=1.0)
    ds._last_decay = time.time() - 2 * 86400
    ds.decay()
    # 2 天 = 2 个半衰期 ⇒ 1.0 × 0.25 = 0.25
    assert 0.2 < d.intensity < 0.3, f"2 天应约 0.25，实得 {d.intensity}"
