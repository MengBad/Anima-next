"""成长快照契约（2026-10-23）。

## 为什么需要它

前几轮已让 `情感 → Need → Drive → Goal → 推进` 整条链跑通。但 owner 看到的
是一堆孤立事件，**没有「它在变」的感受**。

RFC-0100 North Star：
> *Make anima-next a digital life that grows a little more, every day, than yesterday.*

**「比昨天多一点」必须可核对**，否则无从谈起。

## 本测试锁死的边界

1. **只由真实差异驱动**：没有变化就如实说「和之前差不多」，
   **绝不为了好看而编造成长**（自欺比没有更糟）；
2. **只读**：快照不修改任何状态（Identity/Goal/Memory 都不动）；
3. **独立存储**：落 `data/growth/snapshots.jsonl`，不污染既有文件；
4. **有界**：只保留最近 N 份（防无限增长）；
5. **坏行隔离**：jsonl 有坏行不抛，跳过继续；
6. 快照不足 2 份时**如实说攒不够**，不硬编对比。
"""
import json
import time

import pytest

from anima.constitution.growth import (
    MAX_SNAPSHOTS, _append_snapshot, diff_snapshots, format_growth,
    load_snapshots, take_snapshot,
)


def _snap(ts, traits=None, values=None, mem=None, goals=None):
    return {"ts": ts, "traits": traits or {}, "values": values or [],
            "memory": mem or {}, "goals": goals or {}}


# ── 1. 对比只由真实差异驱动 ───────────────────────────────────────────


def test_no_change_reports_no_growth():
    """🔴 没有变化必须如实说 —— 不编造「成长」。"""
    a = _snap(1000, {"social": 1.0}, ["x"], {"episodes": 1}, {"completed": 0})
    b = _snap(2000, {"social": 1.0}, ["x"], {"episodes": 1}, {"completed": 0})
    d = diff_snapshots(a, b)
    assert d["has_growth"] is False
    assert d["changes"] == []


def test_trait_change_detected():
    d = diff_snapshots(_snap(1, {"social": 0.8}), _snap(2, {"social": 1.0}))
    assert d["has_growth"] is True
    assert any("social" in c and "增强" in c for c in d["changes"])


def test_trait_decrease_detected():
    d = diff_snapshots(_snap(1, {"social": 1.0}), _snap(2, {"social": 0.5}))
    assert any("减弱" in c for c in d["changes"])


def test_small_trait_change_ignored():
    """微小波动（<0.05）不算成长 —— 避免噪音被当成进步。"""
    d = diff_snapshots(_snap(1, {"social": 1.0}), _snap(2, {"social": 1.02}))
    assert d["has_growth"] is False


def test_values_growth_detected():
    d = diff_snapshots(_snap(1, values=["a"]), _snap(2, values=["a", "b"]))
    assert any("在乎的事多了" in c for c in d["changes"])


def test_memory_growth_detected():
    d = diff_snapshots(
        _snap(1, mem={"episodes": 1, "relations": 80}),
        _snap(2, mem={"episodes": 4, "relations": 81}))
    assert any("episodes 多了 3 条" in c for c in d["changes"])
    assert any("relations 多了 1 条" in c for c in d["changes"])


def test_goal_completion_detected():
    d = diff_snapshots(_snap(1, goals={"completed": 0}),
                       _snap(2, goals={"completed": 2}))
    assert any("做成了 2 件" in c for c in d["changes"])


# ── 2. 汇报文案 ───────────────────────────────────────────────────────


def test_format_with_insufficient_snapshots(tmp_path):
    """快照不足 2 份 ⇒ 如实说攒不够，不硬编。"""
    out = format_growth(tmp_path)
    assert "攒" in out or "至少" in out
    assert "变化" in out


def test_format_with_growth(tmp_path):
    _append_snapshot(tmp_path, _snap(time.time() - 86400, {"social": 0.5}))
    _append_snapshot(tmp_path, _snap(time.time(), {"social": 1.0}))
    out = format_growth(tmp_path)
    assert "social" in out
    assert "增强" in out


def test_format_without_growth_is_honest(tmp_path):
    _append_snapshot(tmp_path, _snap(time.time() - 86400, {"social": 1.0}))
    _append_snapshot(tmp_path, _snap(time.time(), {"social": 1.0}))
    out = format_growth(tmp_path)
    assert "差不多" in out or "没有明显" in out, f"应如实说没变化：{out!r}"


# ── 3. 存储：独立 / 有界 / 坏行隔离 ───────────────────────────────────


def test_snapshots_stored_in_isolated_file(tmp_path):
    _append_snapshot(tmp_path, _snap(time.time()))
    assert (tmp_path / "growth" / "snapshots.jsonl").exists(), (
        "必须独立存储（data/growth/），不污染既有文件"
    )


def test_snapshot_count_bounded(tmp_path):
    for i in range(MAX_SNAPSHOTS + 10):
        _append_snapshot(tmp_path, _snap(time.time() + i, {"social": 1.0}))
    snaps = load_snapshots(tmp_path, limit=10000)
    assert len(snaps) <= MAX_SNAPSHOTS, f"应裁剪到 {MAX_SNAPSHOTS} 份，实得 {len(snaps)}"


def test_corrupted_lines_skipped(tmp_path):
    p = tmp_path / "growth" / "snapshots.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text(
        json.dumps({"ts": 1, "traits": {}}) + "\n"
        + "{ broken json\n"
        + json.dumps({"ts": 2, "traits": {}}) + "\n",
        encoding="utf-8")
    snaps = load_snapshots(tmp_path)
    assert len(snaps) == 2, "坏行应被跳过，好行保留"


# ── 4. take_snapshot 只读且安全 ───────────────────────────────────────


def test_take_snapshot_does_not_modify_goals(tmp_path):
    """🔴 快照只读 —— 不得修改既有 Goal 状态。"""
    from anima.agent.goal import GoalStore
    from anima.core.persistence import PersistenceProvider
    import types

    P = PersistenceProvider(str(tmp_path))
    st = GoalStore(P)
    g = st.create("我的目标", 42)
    before = json.dumps([x.objective for x in st.list()])

    core = types.SimpleNamespace(
        persistence=P,
        config=types.SimpleNamespace(
            persistence=types.SimpleNamespace(data_dir=str(tmp_path))),
        memory_authority=None,
    )
    take_snapshot(core)

    after = json.dumps([x.objective for x in GoalStore(P).list()])
    assert before == after, "快照不得修改既有目标"


def test_take_snapshot_survives_errors():
    """无 memory_authority / 无 persistence ⇒ 不崩。"""
    import types
    core = types.SimpleNamespace(
        persistence=None,
        config=types.SimpleNamespace(
            persistence=types.SimpleNamespace(data_dir="/nonexistent/dir")),
        memory_authority=None,
    )
    take_snapshot(core)   # 不应抛出


# ── 5. 配置与意图 ─────────────────────────────────────────────────────


def test_default_off():
    from anima.core.config import AnimaConfig
    assert AnimaConfig().autonomy.growth_snapshot_enabled is False


@pytest.mark.parametrize("text", [
    "你有什么变化", "你最近变化大吗", "你成长了吗", "和昨天比怎么样",
])
def test_growth_intent_recognized(text):
    from anima.llm.request_executor import classify_request_intent
    assert classify_request_intent(text) == "growth"


@pytest.mark.parametrize("text", ["我的目标", "帮我盯着 X"])
def test_growth_does_not_steal_goal_intent(text):
    from anima.llm.request_executor import classify_request_intent
    assert classify_request_intent(text) == "goal"


# ── 6. 成长快照不得依赖 memory_authority（2026-10-23 部署后踩坑）──────


def test_snapshot_works_without_memory_authority(tmp_path):
    """🔴 快照不需要 memory_authority —— 它能自己兜底。

    实测踩坑：第一版把 growth 快照写在 `if _auth is not None:` 分支里
    ⇒ `_auth` 为 None 时**永不记录**（部署后连 `data/growth/` 目录都没建出来）。
    """
    import types

    core = types.SimpleNamespace(
        persistence=None,
        config=types.SimpleNamespace(
            persistence=types.SimpleNamespace(data_dir=str(tmp_path))),
        memory_authority=None,
    )
    snap = take_snapshot(core)
    assert snap is not None, "没有 memory_authority 也必须能采集（有兜底）"
    assert (tmp_path / "growth" / "snapshots.jsonl").exists()


def test_growth_block_placed_outside_auth_guard_source_level():
    """源码契约：growth 快照块必须在 `_auth is not None` 判定之外。"""
    import inspect
    from anima.core.autonomy import Heartbeat

    src = inspect.getsource(Heartbeat._tick)
    i_growth = src.find("growth_snapshot_enabled")
    i_auth = src.find("_auth = getattr(self.core, \"memory_authority\", None)")
    assert i_growth > 0, "应有成长快照块"
    assert i_auth > 0, "应有 auth 判定"
    assert i_growth < i_auth, (
        f"成长快照必须在 auth 判定**之前**（实测：放在里面导致 auth=None 时永不记录）"
        f" growth@{i_growth} auth@{i_auth}"
    )


# ── 7. 快照块不得引用尚未赋值的局部变量（2026-10-23 生产实测）──────────


def test_growth_block_does_not_reference_undefined_now():
    """🔴 生产实测：`_bj_date_str(now)` 抛 `UnboundLocalError`
    —— `now` 是 `_tick` 后面（第 1520 行）才赋值的局部变量。

    表现：每 30 秒一条 WARNING「快照失败」，快照一条都没落盘
    （这个异常一直存在，但此前用 logger.debug 记录、默认不输出 ⇒ 看不见）。
    """
    import inspect
    from anima.core.autonomy import Heartbeat

    src = inspect.getsource(Heartbeat._tick)
    i_growth = src.find("growth_snapshot_enabled")
    assert i_growth > 0

    # 找到 growth 块（到下一个 try/except 边界即可，简单取 800 字窗口）
    window = src[i_growth:i_growth + 800]
    assert "_bj_date_str(now)" not in window, (
        "🔴 快照块不得用 `now`（该处尚未赋值）—— 必须用 time.time()"
    )
    assert "time.time()" in window, "应改用 time.time() 取当前时间"


def test_tick_does_not_reference_now_before_assignment():
    """通用防护：`_tick` 中 growth/emotion 前置块不得引用 `now`。

    这是本轮踩的坑的泛化 —— 「在函数开头用后面才赋值的局部变量」。
    """
    import inspect
    from anima.core.autonomy import Heartbeat

    src = inspect.getsource(Heartbeat._tick)
    i_now_assign = src.find("\n        now = ")
    assert i_now_assign > 0, "应存在 now 赋值"
    head = src[:i_now_assign]
    # 前半段（now 赋值之前）不得出现裸 `now`
    for marker in ("growth_snapshot_enabled", "emotion_bridge_enabled"):
        i = head.find(marker)
        if i < 0:
            continue
        seg = head[i:i + 800]
        assert "(now)" not in seg, (
            f"🔴 {marker} 块在 now 赋值之前，不得使用 now"
        )
