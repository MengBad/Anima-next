"""B1 机制守卫：宪法 Drive 桥的**链首来源必须真实且可审计**。

背景（2026-09-18 项目全面审查，报告 `reports/2026-09-18_project_audit_and_evolution_plan.md`）：

- 宪法派生链 `Memory → Identity → Need → Affect → Drive` 的第一条边在
  `anima/core/autonomy.py:_constitution_drive` 里是**现场合成的替身 Memory**
  （2 个 mood 标量 + 常量 `relation_depth=0.2`）；全库唯一真实读取水合 Memory 的地方
  只在 `anima_core.py:296` 且仅用于计数。
- 该替身自带的 `semantic` 记录让 `identity.traits` 峰值恒为 1.0 ⇒ `need.urgency=1.0`
  ⇒ 激活门被"顶开"。于是这条标称已接线的链，实际上与真实记忆无关。

本测试文件是**机制守卫**，不做"某函数返回了某个 dict"的浅断言，而是钉住：

1. **来源必须如实标注**：拿到真实记忆 = `live`，真实但为空 = `live-empty`，
   退回替身 = `synthetic`。任何"替身伪装成已接线"的回归都会在此失败。
2. **输入端必须真的参与运算**：同样的 perception 下，带 trait 的真记忆激活 Drive，
   空记忆不激活（载荷证明）——仅"传了个参数进去"不算接线。
3. **纯函数契约**：`f(Memory)` 不得改源（传快照、只读）。
4. **接线点**：`Heartbeat._constitution_drive_snapshot` 必须优先读
   `core.memory_authority`，读不到才退替身。

⚠️ 已知边界（台账 D-13，未被本文件掩盖）：当前水合只产 `episode`，而
`derive_identity` 只把 `semantic` 的数值字段聚成 traits ⇒ 生产形状的真实记忆
`identity_traits == 0`、Drive 只由 Perception(novelty) 决定。也就是说：
**接上真记忆 ≠ 链变活**，只是从"伪造的活"变成"诚实的弱"。要真正让链有内容，
必须先补 Reflection → Memory 闭环（D-13）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from anima.constitution.state import Memory, MemoryRecord


# ---------------------------------------------------------------------------
# 构造器
# ---------------------------------------------------------------------------

def _memory_live_with_trait() -> Memory:
    """真实形状且有内容的 Memory：一条 episode + 一条带数值 trait 的 semantic。"""
    return Memory(
        episodes=[MemoryRecord(kind="episode", content={"note": "群里聊了天气"}, salience=0.3)],
        semantic=[MemoryRecord(kind="semantic", content={"curiosity": 0.9}, salience=1.0)],
        relations=[],
    )


def _memory_hydrated_shape() -> Memory:
    """当前生产水合的真实形状：只有 episode，没有 semantic ⇒ traits 恒空（D-13）。"""
    return Memory(
        episodes=[MemoryRecord(kind="episode", content={"note": "x"}, salience=0.3)],
    )


# ---------------------------------------------------------------------------
# 1. 来源标注（防"替身伪装"）
# ---------------------------------------------------------------------------

def test_no_memory_falls_back_to_labeled_synthetic():
    """不传 memory = 显式替身回退，必须如实标注 synthetic，且保留旧行为。"""
    from anima.core.autonomy import _constitution_drive, _drive_gate_ok

    d = _constitution_drive(0.8, 0.6, relation_depth=0.2, threat=0.0)
    assert d is not None
    assert d["memory_source"] == "synthetic"
    # 替身自带 semantic → traits 非空（这正是"顶开激活门"的机制来源）
    assert d["identity_traits"] >= 1
    assert d["record_counts"]["semantic"] == 1
    assert _drive_gate_ok(d) is True  # 旧行为不变：正情绪高唤醒 → 允许主动


def test_live_memory_is_labeled_live():
    from anima.core.autonomy import _constitution_drive

    d = _constitution_drive(0.8, 0.6, threat=0.0, memory=_memory_live_with_trait())
    assert d is not None
    assert d["memory_source"] == "live"
    assert d["record_counts"] == {"episodes": 1, "semantic": 1, "relations": 0}
    assert d["identity_traits"] == 1


def test_live_but_empty_memory_is_labeled_live_empty():
    """真实记忆为空 ≠ 替身：两者必须可区分，否则"接上了"无法证伪。"""
    from anima.core.autonomy import _constitution_drive

    d = _constitution_drive(0.8, 0.6, threat=0.0, memory=Memory())
    assert d is not None
    assert d["memory_source"] == "live-empty"
    assert d["identity_traits"] == 0
    assert d["record_counts"] == {"episodes": 0, "semantic": 0, "relations": 0}


def test_hydrated_production_shape_is_wired_but_inert():
    """生产水合形状：接线是真的（live），但它不产生 traits —— D-13 的直接取证。"""
    from anima.core.autonomy import _constitution_drive

    d = _constitution_drive(0.8, 0.6, threat=0.0, memory=_memory_hydrated_shape())
    assert d["memory_source"] == "live"
    assert d["record_counts"]["episodes"] == 1
    assert d["identity_traits"] == 0  # ← 台账 D-13：Reflection→Memory 闭环缺失


# ---------------------------------------------------------------------------
# 2. 载荷证明（输入端非空才算真接线）
# ---------------------------------------------------------------------------

def test_live_memory_is_load_bearing():
    """同一 perception：带 trait 的真记忆激活 Drive，空记忆不激活。

    这是"输入端真的参与运算"的最强证明——只断言参数被传进去是不够的。
    """
    from anima.core.autonomy import _constitution_drive, _drive_gate_ok

    same_perception = dict(mood_arousal=0.2, mood_valence=0.1, threat=0.0)
    with_trait = _constitution_drive(memory=_memory_live_with_trait(), **same_perception)
    without = _constitution_drive(memory=Memory(), **same_perception)

    assert with_trait["activated"] is True
    assert without["activated"] is False          # 无 traits → need.urgency=0
    assert _drive_gate_ok(with_trait) is True
    assert _drive_gate_ok(without) is False
    # 且差异只能来自 Memory 边（Perception 完全相同）
    assert with_trait["record_counts"]["semantic"] == 1
    assert without["record_counts"]["semantic"] == 0


def test_traits_absent_means_drive_depends_only_on_novelty():
    """诚实边界：真实记忆中无 semantic 时，激活只由 Perception(novelty) 决定。"""
    from anima.core.autonomy import _constitution_drive

    low = _constitution_drive(0.2, 0.1, threat=0.0, memory=_memory_hydrated_shape())
    high = _constitution_drive(0.9, 0.1, threat=0.0, memory=_memory_hydrated_shape())
    assert low["identity_traits"] == high["identity_traits"] == 0
    assert low["activated"] is False
    assert high["activated"] is True  # 0.4 * 0.9 = 0.36 > 0.3 激活门


# ---------------------------------------------------------------------------
# 3. 纯函数契约
# ---------------------------------------------------------------------------

def test_live_memory_is_not_mutated():
    """f(Memory) 只读：桥内部必须取快照，不得改源。"""
    from anima.core.autonomy import _constitution_drive

    mem = _memory_live_with_trait()
    before = (list(mem.episodes), list(mem.semantic), list(mem.relations))
    content_before = [dict(r.content) for r in (*mem.episodes, *mem.semantic)]

    _constitution_drive(0.5, 0.5, threat=0.0, memory=mem)

    assert (mem.episodes, mem.semantic, mem.relations) == before
    assert [dict(r.content) for r in (*mem.episodes, *mem.semantic)] == content_before


# ---------------------------------------------------------------------------
# 4. 接线点：Heartbeat 必须优先读 MemoryAuthority
# ---------------------------------------------------------------------------

@pytest.fixture()
def _mood_unavailable(monkeypatch):
    """把 mood 读取打断，使感知常量可预期（0.2 / 0.1），避免测试依赖 mood 单例状态。"""
    import anima.cognitive.mood as mood_mod

    def _boom(*_a, **_k):
        raise RuntimeError("mood 不可用（测试隔离）")

    monkeypatch.setattr(mood_mod, "get_mood_system", _boom)


def _stub_heartbeat(data_dir, memory_authority):
    """只需 config.persistence.data_dir 与 memory_authority 的轻量 self 桩。"""
    return SimpleNamespace(core=SimpleNamespace(
        config=SimpleNamespace(persistence=SimpleNamespace(data_dir=str(data_dir))),
        memory_authority=memory_authority,
    ))


def test_snapshot_prefers_real_memory_authority(_mood_unavailable, tmp_path):
    from anima.core.autonomy import Heartbeat
    from anima.constitution.memory_authority import MemoryAuthority

    auth = MemoryAuthority()
    auth.engine.ingest_semantic({"curiosity": 0.8}, salience=1.0)

    stub = _stub_heartbeat(tmp_path, auth)
    d = Heartbeat._constitution_drive_snapshot(stub, 727631526)

    assert d is not None
    assert d["memory_source"] == "live"
    assert d["identity_traits"] == 1
    assert "gate_ok" in d
    assert d["gate_ok"] is True


def test_snapshot_falls_back_to_synthetic_without_authority(_mood_unavailable, tmp_path):
    """无 MemoryAuthority（宪法水合失败被隔离）时才退替身，并如实标注。"""
    from anima.core.autonomy import Heartbeat

    stub = _stub_heartbeat(tmp_path, None)
    d = Heartbeat._constitution_drive_snapshot(stub, 727631526)

    assert d is not None
    assert d["memory_source"] == "synthetic"
    assert "gate_ok" in d


def test_snapshot_survives_broken_authority(_mood_unavailable, tmp_path):
    """snapshot_memory() 抛异常时必须 fail-closed 到替身，不得让主动表达崩掉。"""
    from anima.core.autonomy import Heartbeat

    class _Boom:
        def snapshot_memory(self):
            raise RuntimeError("authority 损坏")

    stub = _stub_heartbeat(tmp_path, _Boom())
    d = Heartbeat._constitution_drive_snapshot(stub, 727631526)

    assert d is not None
    assert d["memory_source"] == "synthetic"
