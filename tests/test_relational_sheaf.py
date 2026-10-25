# -*- coding: utf-8 -*-
"""Relational Sheaf 契约测试（2026-10-25）。

## 为什么写这个

`G:/Anima/anima/sylanne_alpha/relational_sheaf.py` 有 **1179 行**关系数学，
是「N 个并发关系」建模的核心（层拉普拉斯扩散 + 层上同调 H¹）。
但实测：

```
relational_sheaf : 0 个测试文件引用
void_calculus    : 0
social_field     : 0
computation_spine: 0
```

**1179 行零覆盖** —— 迁移过来等于往生产加一块没人拦得住的代码。
本文件先钉住**契约**（不追求数学完备性证明），为将来的迁移提供回归网。

## 测什么 / 不测什么

✅ 测：
- 拓扑结构不变量（顶点/边/三角形计数、agent=0 约束）
- 上同调 H¹ 的**退化情形**（无边 ⇒ 0；无三角形时的线性代数关系）
- 能量公理 S5（传播消耗能量、耗尽后拒绝）
- 有界性（stalks 不爆 [-2,2]、能量不超上限）
- 序列化往返
- 崩溃安全性（空层 / 非法索引 / 极端值不抛）

❌ 不测：
- 具体数学数值的精确性（没有上游理论文档可对照）
- 物理语义是否正确（那是设计问题，不是契约问题）

## 来源

`G:/Anima`（冻结分支）的副本，用 `sys.path` 注入以避免与 Anima-next 的
`anima` 包冲突（两者同名但不同内容）。
"""
from __future__ import annotations

import pathlib

import pytest

#: 仓库内副本（2026-10-25 从冻结分支 `G:/Anima` 原样拷入）。
#: 🔴 用**仓库内**路径而不是冻结分支：冻结分支只在本机存在，
#: 生产/CI 上没有 ⇒ 测试会静默 skip，等于没写。
_SRC = (pathlib.Path(__file__).resolve().parents[1]
        / "anima" / "cognitive" / "sheaf" / "relational_sheaf.py")

#: 冻结分支的同名原件（仅用于「副本与上游一致」的校验，可以不存在）
_UPSTREAM = pathlib.Path("G:/Anima/anima/sylanne_alpha/relational_sheaf.py")


@pytest.fixture(scope="module")
def sh():
    """从仓库内副本导入。

    用 `importlib` 而非 `import anima.cognitive.sheaf.relational_sheaf`：
    走正常 import 会让它变成包的**已加载子模块**，将来若有人把它接进
    生产路径，测试就在测"已加载的那份"而不是"文件里的那份"。
    这里显式按文件路径加载，保证测的是源码本身。
    """
    import importlib.util
    import sys

    if not _SRC.exists():
        pytest.fail(f"仓库内副本缺失：{_SRC}（这是必需的，不是可选）")
    spec = importlib.util.spec_from_file_location("_rel_sheaf_under_test", _SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── 1. RelationalComplex 拓扑不变量 ─────────────────────────────────

def test_empty_complex_has_only_agent(sh):
    c = sh.RelationalComplex()
    assert c.n_vertices == 1, "空复形只有 agent 顶点"
    assert c.n_edges == 0
    assert c.n_triangles == 0
    assert c.partners() == []


def test_add_partner_is_idempotent(sh):
    c = sh.RelationalComplex()
    c.add_partner(1)
    c.add_partner(1)
    assert c.n_vertices == 2
    assert c.n_edges == 1, "同一 partner 重复添加不得产生多条边"


def test_partner_zero_is_rejected(sh):
    """顶点 0 恒为 agent —— 不许被当成交互对象。"""
    c = sh.RelationalComplex()
    c.add_partner(0)
    assert c.n_vertices == 1
    assert c.n_edges == 0


def test_edge_index_returns_position_or_minus_one(sh):
    c = sh.RelationalComplex()
    assert c.edge_index(5) == -1
    c.add_partner(5)
    assert c.edge_index(5) == 0
    c.add_partner(7)
    assert c.edge_index(7) == 1


def test_triangle_requires_both_endpoints(sh):
    """三角形 (0,i,j) 会自动补边；i==j 或含 0 时被拒。"""
    c = sh.RelationalComplex()
    c.add_triangle(1, 1)
    assert c.n_triangles == 0, "i==j 不是三角形"
    c.add_triangle(0, 1)
    assert c.n_triangles == 0, "含 agent 的两点不构成三角形"
    c.add_triangle(1, 2)
    assert c.n_triangles == 1
    assert c.n_edges == 2, "补三角形必须同时补两条边"


def test_triangle_is_added_at_most_once(sh):
    c = sh.RelationalComplex()
    c.add_triangle(1, 2)
    c.add_triangle(2, 1)
    assert c.n_triangles == 1, "顺序不同是同一个三角形"


def test_remove_partner_cleans_all_simplices(sh):
    c = sh.RelationalComplex()
    c.add_triangle(1, 2)   # 顶点 1,2 + 边 (0,1),(0,2) + 三角 (0,1,2)
    c.add_partner(3)       # 边 (0,3)
    assert c.n_edges == 3
    c.remove_partner(1)
    assert 1 not in c.partners()
    assert c.n_triangles == 0, "含该 partner 的三角形必须一并移除"
    # 待移除：顶点 1、边 (0,1)、三角 (0,1,2)
    # 保留：边 (0,2) 与 (0,3) —— 三角形被删不代表另一条边消失
    assert c.n_edges == 2
    assert sorted(c.partners()) == [2, 3]


def test_remove_nonexistent_partner_is_noop(sh):
    c = sh.RelationalComplex()
    c.add_partner(1)
    c.remove_partner(99)
    assert c.n_vertices == 2
    c.remove_partner(0)
    assert c.n_vertices == 2, "不许移除 agent 自己"


# ── 2. 序列化 ────────────────────────────────────────────────────────

def test_complex_roundtrip(sh):
    c = sh.RelationalComplex()
    c.add_triangle(1, 2)
    c.add_partner(3)
    d = c.to_dict()
    c2 = sh.RelationalComplex.from_dict(d)
    assert c2.n_vertices == c.n_vertices
    assert c2.n_edges == c.n_edges
    assert c2.n_triangles == c.n_triangles
    assert sorted(c2.partners()) == sorted(c.partners())


def test_sheaf_roundtrip_preserves_relations(sh):
    s = sh.ScarSheaf()
    s.add_relationship(1, rel_type="intimate", maturity=0.5)
    s.add_relationship(2, rel_type="friendly", maturity=0.2)
    s2 = sh.ScarSheaf.from_dict(s.to_dict())
    assert s2.complex.n_edges == s.complex.n_edges
    assert s2.complex.partners() == s.complex.partners()


# ── 3. 上同调 H¹ 的退化情形 ─────────────────────────────────────────

def test_h1_is_zero_without_edges(sh):
    s = sh.ScarSheaf()
    assert s.compute_h1() == 0, "没有关系就没有矛盾可谈"


def test_h1_is_non_negative(sh):
    """H¹ 是维度，绝不可能为负（源码里 max(0,...) 兜底，此处钉住）。"""
    s = sh.ScarSheaf()
    for i in range(1, 5):
        s.add_relationship(i, maturity=0.3 * i)
    assert s.compute_h1() >= 0


def test_h1_without_triangles_uses_linear_algebra_formula(sh):
    """无三角形时 H¹ = dim C¹ - rank(δ⁰)，且不超过 dim C¹。"""
    s = sh.ScarSheaf()
    s.add_relationship(1)
    s.add_relationship(2)
    h1 = s.compute_h1()
    dim_c1 = s.complex.n_edges * sh._EDGE_STALK_DIM
    assert 0 <= h1 <= dim_c1


def test_h1_is_deterministic(sh):
    """同一状态重复计算必须同值（缓存不得泄漏旧值）。"""
    s = sh.ScarSheaf()
    for i in (1, 2, 3):
        s.add_relationship(i, maturity=0.3 * i)
    assert s.compute_h1() == s.compute_h1()


def test_inconsistency_vector_length_matches_edges(sh):
    s = sh.ScarSheaf()
    for i in (1, 2, 3):
        s.add_relationship(i, maturity=0.3 * i)
    v = s.inconsistency_vector()
    assert len(v) == s.complex.n_edges
    assert all(x >= 0.0 for x in v), "范数不可能为负"


def test_dissociation_pressure_in_unit_range(sh):
    s = sh.ScarSheaf()
    for i in (1, 2, 3):
        s.add_relationship(i, maturity=0.3 * i)
    p = s.dissociation_pressure()
    assert 0.0 <= p <= 1.0, f"解离压力越界：{p}"


def test_spectral_gap_is_finite_and_non_negative(sh):
    import math
    s = sh.ScarSheaf()
    s.add_relationship(1)
    gap = s.spectral_gap()
    assert math.isfinite(gap)
    assert gap >= 0.0


def test_observe_exposes_expected_keys(sh):
    s = sh.ScarSheaf()
    s.add_relationship(1, maturity=0.4)
    o = s.observe()
    for k in ("h1_dim", "spectral_gap", "energy", "dissociation_pressure",
              "n_relationships", "n_triangles"):
        assert k in o, f"observe 缺 {k}"
    assert o["n_relationships"] == 1


# ── 4. 关系管理与表示矩阵 ───────────────────────────────────────────

def test_add_relationship_grows_stalks(sh):
    s = sh.ScarSheaf()
    s.add_relationship(1)
    assert len(s._edge_stalks) == 1
    assert len(s._presentation_matrices) == 1
    assert len(s._rel_types) == 1
    assert len(s._maturities) == 1


def test_rel_type_string_is_coerced(sh):
    s = sh.ScarSheaf()
    s.add_relationship(1, rel_type="intimate")
    assert s._rel_types[0] == sh.INTIMATE


def test_unknown_rel_type_falls_back_to_friendly(sh):
    s = sh.ScarSheaf()
    s.add_relationship(1, rel_type="完全不存在的类型")
    assert s._rel_types[0] == sh.FRIENDLY


def test_maturity_is_clamped_to_unit_interval(sh):
    s = sh.ScarSheaf()
    s.add_relationship(1, maturity=5.0)
    assert s._maturities[0] == 1.0
    s.set_maturity(1, -3.0)
    assert s._maturities[0] == 0.0


def test_presentation_matrix_has_expected_shape(sh):
    s = sh.ScarSheaf()
    s.add_relationship(1, maturity=0.5)
    P = s._presentation_matrices[0]
    assert len(P) == sh._VERTEX_STALK_DIM
    assert all(len(row) == sh._EDGE_STALK_DIM for row in P)


def test_remove_relationship_shrinks_parallel_arrays(sh):
    s = sh.ScarSheaf()
    s.add_relationship(1)
    s.add_relationship(2)
    s.remove_relationship(1)
    assert len(s._edge_stalks) == 1
    assert len(s._presentation_matrices) == 1
    assert len(s._rel_types) == 1
    assert len(s._maturities) == 1
    assert 1 not in s.complex.partners()


def test_remove_nonexistent_relationship_is_noop(sh):
    s = sh.ScarSheaf()
    s.add_relationship(1)
    s.remove_relationship(99)
    assert len(s._edge_stalks) == 1


# ── 5. 能量公理 S5 ──────────────────────────────────────────────────

def test_propagate_rejects_invalid_source(sh):
    s = sh.ScarSheaf()
    r = s.propagate(0, [1.0] * sh._EDGE_STALK_DIM)
    assert r["propagated"] is False
    assert r["reason"] == "invalid_source"


def test_propagate_consumes_energy(sh):
    s = sh.ScarSheaf()
    s.add_relationship(1)
    before = s._energy
    r = s.propagate(0, [0.5] * sh._EDGE_STALK_DIM)
    assert r["propagated"] is True
    assert s._energy < before, "传播必须消耗能量（公理 S5）"


def test_propagate_refuses_when_energy_depleted(sh):
    s = sh.ScarSheaf(max_energy=1.0)
    s.add_relationship(1)
    s._energy = 0.0
    r = s.propagate(0, [1.0] * sh._EDGE_STALK_DIM)
    assert r["propagated"] is False
    assert r["reason"] == "energy_depleted"


def test_energy_never_exceeds_max(sh):
    s = sh.ScarSheaf(max_energy=1.0)
    s.add_relationship(1)
    for i in range(200):
        s.tick(0, [0.1] * sh._EDGE_STALK_DIM, timestamp=float(i))
    assert s._energy <= 1.0 + 1e-9, "能量不得超上限"


def test_energy_never_negative(sh):
    s = sh.ScarSheaf(max_energy=1.0)
    s.add_relationship(1)
    for i in range(200):
        s.propagate(0, [1.0] * sh._EDGE_STALK_DIM, dt=5.0)
    assert s._energy >= 0.0


# ── 6. 有界性（数值稳定性）──────────────────────────────────────────

def test_edge_stalks_stay_in_clamp_range(sh):
    """源码把 stalk 夹在 [-2, 2] —— 高强度反复输入不得越界。"""
    s = sh.ScarSheaf()
    s.add_relationship(1)
    for _ in range(50):
        s.propagate(0, [10.0] * sh._EDGE_STALK_DIM)
    for v in s._edge_stalks[0]:
        assert -2.0 - 1e-9 <= v <= 2.0 + 1e-9, f"stalk 越界：{v}"


def test_vertex_stalk_finite_after_many_ticks(sh):
    import math
    s = sh.ScarSheaf()
    s.add_relationship(1)
    s.add_relationship(2)
    for i in range(100):
        s.tick(i % 2, [1.0] * sh._EDGE_STALK_DIM, timestamp=float(i))
    for v in s._vertex_stalk:
        assert math.isfinite(v), f"顶点茎出现 NaN/Inf：{v}"


def test_maturity_grows_but_capped(sh):
    s = sh.ScarSheaf()
    s.add_relationship(1, maturity=0.0)
    for i in range(100):
        s.tick(0, [0.0] * sh._EDGE_STALK_DIM, timestamp=float(i))
    assert 0.0 <= s._maturities[0] <= 1.0


# ── 7. tick 输出契约 ────────────────────────────────────────────────

def test_tick_returns_expected_keys(sh):
    s = sh.ScarSheaf()
    s.add_relationship(1)
    out = s.tick(0, [0.1] * sh._EDGE_STALK_DIM, timestamp=1.0)
    for k in ("tick", "propagation", "energy", "dissociation_pressure", "timestamp"):
        assert k in out, f"tick 输出缺 {k}"


def test_tick_counter_increments(sh):
    s = sh.ScarSheaf()
    s.add_relationship(1)
    assert s.tick(0, [0.0] * sh._EDGE_STALK_DIM)["tick"] == 1
    assert s.tick(0, [0.0] * sh._EDGE_STALK_DIM)["tick"] == 2


def test_tick_without_relationships_does_not_crash(sh):
    """空层 tick 必须安全（生产可能在没有关系时被调用）。"""
    s = sh.ScarSheaf()
    out = s.tick(0, [0.0] * sh._EDGE_STALK_DIM)
    assert out["tick"] == 1
    assert out["propagation"]["propagated"] is False


def test_observe_returns_dict(sh):
    s = sh.ScarSheaf()
    s.add_relationship(1, maturity=0.4)
    o = s.observe()
    assert isinstance(o, dict)
    assert o, "observe 不得返回空 dict"


# ── 8. 极端输入不抛（生产 fail-closed 要求）─────────────────────────

@pytest.mark.parametrize("event", [
    [],
    [0.0],
    [1e9] * 8,
    [-1e9] * 8,
])
def test_propagate_survives_extreme_events(sh, event):
    s = sh.ScarSheaf()
    s.add_relationship(1)
    s.propagate(0, event)  # 不抛即通过


def test_negative_dt_is_tolerated(sh):
    s = sh.ScarSheaf()
    s.add_relationship(1)
    s.propagate(0, [0.5] * sh._EDGE_STALK_DIM, dt=-1.0)  # 不抛即通过


def test_derive_params_accepts_empty_personality(sh):
    s = sh.ScarSheaf()
    s.add_relationship(1)
    s.derive_params({})  # 不抛即通过


def test_derive_params_accepts_partial_personality(sh):
    s = sh.ScarSheaf()
    s.add_relationship(1)
    s.derive_params({"openness": 0.7})  # 缺字段不得炸


# ── 9. 迁移前置判据 ─────────────────────────────────────────────────

def test_source_has_no_astrbot_dependency(sh):
    """🔴 迁移可行性的硬前提：这个文件不依赖 AstrBot 就能跑。"""
    src = _SRC.read_text(encoding="utf-8")
    assert "astrbot" not in src, "依赖 AstrBot ⇒ 不能直搬"


def test_source_has_no_io(sh):
    """🔴 纯计算模块：不得有文件/网络 I/O（否则迁移要连数据层一起改）。"""
    src = _SRC.read_text(encoding="utf-8")
    for forbidden in ("open(", "aiohttp", "sqlite", "requests."):
        assert forbidden not in src, f"出现 I/O 调用 {forbidden} ⇒ 非纯模块"


def test_copy_matches_upstream_when_available():
    """仓库内副本必须与冻结分支原件**逐字节一致**（防有人偷改数学）。

    冻结分支不在本机时跳过 —— 校验有本机就拿不到，但不是失败。
    """
    if not _UPSTREAM.exists():
        pytest.skip(f"上游原件不在本机（{_UPSTREAM}），跳过一致性校验")
    assert _SRC.read_bytes() == _UPSTREAM.read_bytes(), (
        "仓库内副本与上游不一致 ⇒ 要么是有意修改（请更新本文档说明），"
        "要么是意外漂移（请恢复）"
    )


def test_inert_by_default():
    """🔴 当前状态是「已迁移、未接线」—— 这不许被悄悄改掉。

    若有人把 sheaf 接进生产路径，必须先有观察实证（见包 __init__ docstring），
    并把本条测试与文档同步更新。防的是「悄悄接上、又没人知道怎么回退」。
    """
    root = pathlib.Path(__file__).resolve().parents[1]
    hits: list[str] = []
    for py in (root / "anima").rglob("*.py"):
        if "sheaf" in py.parts:
            continue  # 包内部互相 import 不算接线
        text = py.read_text(encoding="utf-8", errors="ignore")
        if "cognitive.sheaf" in text or "from anima.cognitive import sheaf" in text:
            hits.append(str(py.relative_to(root)))
    assert not hits, (
        "sheaf 被接进生产路径了：" + ", ".join(hits) +
        " —— 接线需要观察实证 + 更新包文档 + 更新本条测试"
    )
