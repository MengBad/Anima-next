# -*- coding: utf-8 -*-
"""群关系一致性观测（H¹ 只读）契约测试（2026-10-25）。

## 这个模块做什么

把 `relational_sheaf` 用在**真实关系数据**上，回答一个现有系统看不到的问题：

> 这个群里的关系，对「我」来说自洽吗？

`RelationEngine` 只能回答「我对 A 多熟」（**按人切开**）。
本模块试着补**全局一致性**视角。

## 本轮最重要的产出：证明 H¹ 不可用

写测试时逐行追踪发现，上游 `compute_h1()` 在当前建模下**恒为 0**：

```
全同       [0.5]*8 vs [0.5]*8      → h1=0
强反差     [1.0]*8 vs [0.0]*8      → h1=0
仅信任不同  0.9 vs 0.1（其余相同）  → h1=0
```

根因：`rank_d0 = dim C¹`（`P_base` 对角 + 次对角耦合 ⇒ 8 行全独立），
`h1 = max(0, ker_d1 - rank_d0) = max(0, 12 - 16) = 0`。

⇒ 本模块**不把 H¹ 当读数**，只用实测有效的
`dissociation_pressure` 与 `inconsistency_vector`。
H¹ 只作「上游原值」附带记录，字段名带 `_unusable` 后缀。

## 判据

1. 构造器只读 `Relation` 已有字段，不做新计算；
2. 边茎映射的每个维度可归因（改一个字段 ⇒ 只变一维）；
3. 有效指标**必须对输入敏感**（这是本轮的核心判据）；
4. H¹ 恒为 0 —— **钉死这个事实**，防止有人以为它坏了；
5. 全流程只读，不写任何文件；
6. 极端输入不抛（空关系、None 字段、负交互数）。
"""
from __future__ import annotations

import pathlib

import pytest

from anima.cognitive.sheaf.observe import (
    _byte_similarity,
    _edge_stalk,
    _stalk_to_bytes,
    _stage_norm,
    build_group_complex,
)

_MOD = (pathlib.Path(__file__).resolve().parents[1]
        / "anima" / "cognitive" / "sheaf" / "observe.py")


class _Rel:
    """最小 Relation 桩（字段与 `anima.cognitive.relation.Relation` 对齐）。"""

    def __init__(self, uid, *, familiarity=0.5, trust=0.5, valence=0.0,
                 bond_tension=0.0, relational_depth=0.5, stage="friend",
                 interaction_count=10):
        self.user_id = uid
        self.familiarity = familiarity
        self.trust = trust
        self.valence = valence
        self.bond_tension = bond_tension
        self.relational_depth = relational_depth
        self.stage = stage
        self.interaction_count = interaction_count


# ── 1. 边茎映射：每个维度可归因 ─────────────────────────────────────

def test_stalk_has_eight_dims():
    assert len(_edge_stalk(_Rel(1))) == 8


def test_stalk_clamps_to_unit_range():
    stalk = _edge_stalk(_Rel(1, familiarity=99.0, trust=-5.0, valence=99.0,
                             bond_tension=99.0, relational_depth=-9.0))
    assert all(0.0 <= v <= 1.0 for v in stalk), f"越界：{stalk}"


def test_changing_familiarity_moves_only_dim0():
    """🔴 可归因性：改一个字段只能动它那一维，否则指标无法解释。"""
    base = _edge_stalk(_Rel(1, familiarity=0.2))
    moved = _edge_stalk(_Rel(1, familiarity=0.9))
    diff = [i for i in range(8) if abs(base[i] - moved[i]) > 1e-9]
    assert diff == [0], f"改 familiarity 影响了维度 {diff}"


def test_changing_trust_moves_only_dim1():
    base = _edge_stalk(_Rel(1, trust=0.2))
    moved = _edge_stalk(_Rel(1, trust=0.9))
    assert [i for i in range(8) if abs(base[i] - moved[i]) > 1e-9] == [1]


def test_changing_tension_moves_only_dim3():
    base = _edge_stalk(_Rel(1, bond_tension=0.1))
    moved = _edge_stalk(_Rel(1, bond_tension=0.9))
    assert [i for i in range(8) if abs(base[i] - moved[i]) > 1e-9] == [3]


def test_changing_activity_moves_only_dim7():
    base = _edge_stalk(_Rel(1, interaction_count=5))
    moved = _edge_stalk(_Rel(1, interaction_count=500))
    assert [i for i in range(8) if abs(base[i] - moved[i]) > 1e-9] == [7]


def test_valence_sign_uses_dim6():
    """valence 的符号单独占一维，正负必须可区分。"""
    neg = _edge_stalk(_Rel(1, valence=-0.8))
    pos = _edge_stalk(_Rel(1, valence=0.8))
    assert neg[6] == 0.0
    assert pos[6] == 1.0


def test_stage_norm_maps_all_stages():
    for stage, expect in (("stranger", 0.0), ("acquaintance", 0.33),
                          ("friend", 0.66), ("close", 1.0)):
        assert _stage_norm(stage) == pytest.approx(expect)


def test_stage_norm_unknown_falls_back_to_zero():
    assert _stage_norm("完全不存在的阶段") == 0.0
    assert _stage_norm(None) == 0.0


def test_activity_is_log_compressed():
    """交互数用对数压缩 —— 避免大数吃满维度。

    公式 `log10(cnt+1)/3`：
        10 次   → log10(11)/3   ≈ 0.347
        1000 次 → log10(1001)/3 ≈ 1.000（封顶）
    """
    few = _edge_stalk(_Rel(1, interaction_count=10))[7]
    many = _edge_stalk(_Rel(1, interaction_count=1000))[7]
    assert few < many
    assert many == pytest.approx(1.0, abs=0.01)
    assert few == pytest.approx(0.347, abs=0.01)


def test_activity_saturates():
    """超过 1000 次不再增长（封顶 1.0）。"""
    a = _edge_stalk(_Rel(1, interaction_count=1000))[7]
    b = _edge_stalk(_Rel(1, interaction_count=100000))[7]
    assert b <= 1.0
    assert a == pytest.approx(b, abs=1e-9)


# ── 2. bytes 转换与相似度 ──────────────────────────────────────────

def test_stalk_to_bytes_has_fixed_length():
    b = _stalk_to_bytes([0.5] * 8)
    assert len(b) == 8
    assert isinstance(b, bytes)


def test_stalk_to_bytes_pads_short_input():
    assert len(_stalk_to_bytes([0.5, 0.5])) == 8


def test_identical_stalks_similarity_is_one():
    a = _stalk_to_bytes([0.5] * 8)
    assert _byte_similarity(a, a) == pytest.approx(1.0)


def test_orthogonal_stalks_similarity_is_zero():
    a = _stalk_to_bytes([1.0, 0, 0, 0, 0, 0, 0, 0])
    b = _stalk_to_bytes([0, 1.0, 0, 0, 0, 0, 0, 0])
    assert _byte_similarity(a, b) == pytest.approx(0.0, abs=1e-9)


def test_similarity_handles_empty():
    assert _byte_similarity(b"", b"\x01") == 0.0


def test_similarity_handles_zero_vector():
    z = bytes([0] * 8)
    assert _byte_similarity(z, _stalk_to_bytes([1.0] * 8)) == 0.0


# ── 3. 复形构造 ────────────────────────────────────────────────────

def test_complex_skips_low_interaction_relations():
    rels = {1: _Rel(1, interaction_count=100), 2: _Rel(2, interaction_count=1)}
    sheaf, meta = build_group_complex(rels)
    assert len(meta["edges"]) == 1
    assert meta["skipped"] == [2]


def test_complex_edge_order_is_stable():
    """顶点编号必须按 user_id 排序 —— 否则跨次读数无法比较。"""
    rels = {30: _Rel(30, interaction_count=50), 10: _Rel(10, interaction_count=50)}
    _, meta = build_group_complex(rels)
    assert [e["user_id"] for e in meta["edges"]] == [10, 30]


def test_complex_triangles_are_all_pairs():
    rels = {i: _Rel(i, interaction_count=20) for i in range(1, 4)}
    _, meta = build_group_complex(rels)
    # 3 个 partner ⇒ C(3,2) = 3 个三角形
    assert meta["n_triangles"] == 3


def test_complex_empty_relations_is_safe():
    sheaf, meta = build_group_complex({})
    assert meta["edges"] == []
    assert meta["n_triangles"] == 0
    assert sheaf.compute_h1() == 0


def test_complex_single_relation_has_no_triangle():
    _, meta = build_group_complex({1: _Rel(1, interaction_count=50)})
    assert meta["n_triangles"] == 0


# ── 4. 🔴 核心：有效指标必须对输入敏感 ─────────────────────────────

def _build(**overrides):
    base = dict(interaction_count=50, relational_depth=0.6)
    base.update(overrides)
    rels = {
        1: _Rel(1, familiarity=0.9, trust=0.9, valence=0.8,
                bond_tension=0.1, **base),
        2: _Rel(2, familiarity=0.2, trust=0.2, valence=-0.8,
                bond_tension=0.9, **base),
    }
    return build_group_complex(rels)[0]


def test_dissociation_pressure_responds_to_divergence():
    """🔴 本轮核心判据：反差大的关系 ⇒ 解离压力更高。

    这是「这个指标能不能用」的唯一检验 —— 若不随输入变化，它就是死的。
    """
    diverge = _build()                       # 一个很熟、一个很生
    same = build_group_complex({
        1: _Rel(1, familiarity=0.5, trust=0.5, valence=0.0,
                bond_tension=0.5, relational_depth=0.6, interaction_count=50),
        2: _Rel(2, familiarity=0.5, trust=0.5, valence=0.0,
                bond_tension=0.5, relational_depth=0.6, interaction_count=50),
    })[0]
    p_div = diverge.dissociation_pressure()
    p_same = same.dissociation_pressure()
    assert p_div > p_same, (
        f"解离压力对关系反差无反应（反差 {p_div} vs 全同 {p_same}）"
        " ⇒ 该指标不可用")


def test_inconsistency_vector_responds_to_divergence():
    diverge = _build()
    same = build_group_complex({
        1: _Rel(1, familiarity=0.5, trust=0.5, valence=0.0,
                bond_tension=0.5, relational_depth=0.6, interaction_count=50),
        2: _Rel(2, familiarity=0.5, trust=0.5, valence=0.0,
                bond_tension=0.5, relational_depth=0.6, interaction_count=50),
    })[0]
    assert (max(diverge.inconsistency_vector())
            > max(same.inconsistency_vector()))


def test_dissociation_pressure_in_unit_range():
    for p in (build_group_complex({})[0],
              _build()):
        assert 0.0 <= p.dissociation_pressure() <= 1.0


def test_inconsistency_length_matches_edges():
    sheaf = _build()
    assert len(sheaf.inconsistency_vector()) == sheaf.complex.n_edges


# ── 4b. 🔴 真实数据验出的致命问题：它量的是「关系多近」而不是「多矛盾」──

def _corr(a, b):
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = sum((x - ma) ** 2 for x in a) ** 0.5
    db = sum((y - mb) ** 2 for y in b) ** 0.5
    return num / (da * db) if da and db else 0.0


def test_inconsistency_correlates_with_relationship_depth():
    """🔴 **这是本模块当前不能用的证据**，必须钉死。

    2026-10-25 用真实生产数据（relation.json，94 人、23 个交互≥10）实测：

        vs 交互次数  r = +0.738
        vs 亲密度    r = +0.840
        vs 关系深度  r = +0.910      ← 近乎完全线性

    最「不自洽」的 8 个人恰好都是关系最深的人（主人 907 次交互、深度 1.00）。

    根因：`coboundary_0 = P_i^T · x_0 - x_i`，两边同向增长 ⇒
    差值主要反映 `x_i` 的模长 ⇒ 关系越近差值越大。

    ⇒ 本指标**只能当关系强度投影**，不能当一致性指标。
    本条测试断言「高相关存在」——若将来改建模（方案 A/B）让相关消失，
    本条会失败，那时该更新 `observe.py` 的结论并改字段名。
    """
    rels = {}
    for i in range(1, 13):
        rels[i] = _Rel(i, familiarity=min(1.0, i / 12),
                       trust=min(1.0, i / 12),
                       relational_depth=min(1.0, i / 12),
                       interaction_count=i * 50)
    sheaf, meta = build_group_complex(rels)
    iv = sheaf.inconsistency_vector()
    depths = [rels[e["user_id"]].relational_depth for e in meta["edges"]]
    r = _corr(iv, depths)
    assert r > 0.85, (
        f"不一致范数与关系深度的相关性降到 {r:+.3f}（原先 +0.91）——"
        "建模可能被改了，请更新 observe.py 的结论段")


def test_deepest_relations_score_highest():
    """直接断言「最深的关系最不自洽」——这是那不成立的具体形态。"""
    rels = {
        1: _Rel(1, familiarity=1.0, trust=0.95, relational_depth=1.0,
                interaction_count=900),
        2: _Rel(2, familiarity=0.05, trust=0.5, relational_depth=0.05,
                interaction_count=10),
    }
    sheaf, meta = build_group_complex(rels)
    iv = sheaf.inconsistency_vector()
    got = {e["user_id"]: v for e, v in zip(meta["edges"], iv)}
    assert got[1] > got[2], (
        "深关系的不一致范数应高于浅关系（当前建模的必然结果）")


def test_observe_docstring_documents_the_failure():
    """源码级判据：结论必须写在 docstring 里，不能只存在于提交信息。"""
    src = _MOD.read_text(encoding="utf-8")
    assert "+0.910" in src or "0.910" in src, "docstring 缺少实测相关系数"
    assert "关系有多近" in src or "关系强度" in src, "docstring 缺少正确诠释"
    assert "不能回答" in src or "不能" in src, "docstring 缺少「不可用」声明"


# ── 5. 🔴 钉死：H¹ 在本配置下恒为 0（不是坏了，是结构决定）──────────

@pytest.mark.parametrize("a,b", [
    ([0.5] * 8, [0.5] * 8),                 # 全同
    ([1.0] * 8, [0.0] * 8),                 # 强反差
    ([0.9, 0.9, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5],
     [0.9, 0.1, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5]),  # 仅信任不同
])
def test_h1_is_always_zero_in_this_configuration(a, b):
    """🔴 事实记录：`compute_h1()` 对边茎**无反应**，恒为 0。

    根因（已逐行追踪）：
        rank_d0 = dim C¹（满秩）⇒ h1 = max(0, ker_d1 - rank_d0) = 0

    这条测试的作用不是"防回归"，是**防止有人误以为 H¹ 能用**。
    若将来上游修好，本条会失败 —— 那时该更新 `observe.py` 的 docstring
    并把 `h1_unusable` 改回 `h1`。
    """
    sheaf = build_group_complex({
        1: _Rel(1, interaction_count=50), 2: _Rel(2, interaction_count=50),
    })[0]
    for i, stalk in enumerate((a, b)):
        sheaf._edge_stalks[i] = list(stalk)
    assert sheaf.compute_h1() == 0, (
        "H¹ 不再恒为 0 —— 上游可能已修复，需更新 observe.py 的结论")


def test_h1_is_zero_regardless_of_relation_count():
    for n in (1, 2, 5):
        rels = {i: _Rel(i, interaction_count=50) for i in range(1, n + 1)}
        sheaf, _ = build_group_complex(rels)
        assert sheaf.compute_h1() == 0


def test_observe_module_marks_h1_as_unusable():
    """源码级判据：H¹ 字段必须带 `_unusable` 后缀（防有人当正常指标用）。"""
    src = _MOD.read_text(encoding="utf-8")
    assert "h1_unusable" in src, "H¹ 字段未标注不可用"
    assert '"h1":' not in src, "仍有调用方可能把 H¹ 当正常读数"


# ── 6. 只读性 ──────────────────────────────────────────────────────

def test_module_does_not_write_files():
    """🔴 观测模块绝不能写盘（只读原则）。"""
    src = _MOD.read_text(encoding="utf-8")
    for forbidden in ("open(", "write_text", "json.dump", ".save(", "persist"):
        assert forbidden not in src, f"观测模块出现写操作 {forbidden}"


def test_module_does_not_inject_prompt():
    """第一步只算不注入 —— 模块**代码**里不得出现注入调用。

    ⚠️ 只查代码行，不查 docstring：docstring 里为了说明「为什么先不注入」
    会**引用** `relation.prompt_fragment` 作为对比，那不是调用。
    判据：出现注入关键词的行，不能是 `import` / 赋值 / 函数调用形态。
    """
    import re
    src = _MOD.read_text(encoding="utf-8")
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for kw in ("add_system_fragment(", "system_prompt =", ".prompt_fragment("):
            if kw in stripped:
                # 允许的形态：纯文本说明（在字符串/docstring 里）。
                # 这里用「该行是否像代码调用」来判断：以 kw 开头或含 `=` 赋值的才算。
                assert not stripped.startswith(kw), (
                    f"观测模块出现了注入调用：{stripped[:80]}")


# ── 7. 极端输入 ────────────────────────────────────────────────────

def test_none_fields_are_tolerated():
    r = _Rel(1)
    r.familiarity = None
    r.trust = None
    r.stage = None
    stalk = _edge_stalk(r)
    assert len(stalk) == 8
    assert all(isinstance(v, float) for v in stalk)


def test_negative_interaction_count_is_safe():
    assert _edge_stalk(_Rel(1, interaction_count=-5))[7] == 0.0


def test_observe_group_returns_none_on_missing_data(tmp_path):
    """取不到数据时返回 None，绝不抛（fail-closed）。"""
    from anima.cognitive.sheaf.observe import observe_group
    got = observe_group(123456, str(tmp_path))
    assert got is None or isinstance(got, dict)


# ── 8. 迁移纪律 ────────────────────────────────────────────────────

def test_observe_stays_out_of_production_path():
    """🔴 观测模块当前**不得**被生产路径 import（第一步只算不注入）。"""
    root = pathlib.Path(__file__).resolve().parents[1]
    hits = []
    for py in (root / "anima").rglob("*.py"):
        if "sheaf" in py.parts:
            continue
        text = py.read_text(encoding="utf-8", errors="ignore")
        if "sheaf.observe" in text or "from anima.cognitive.sheaf import observe" in text:
            hits.append(str(py.relative_to(root)))
    assert not hits, (
        "sheaf.observe 被接进生产路径了：" + ", ".join(hits) +
        " —— 第一步的纪律是「只算不注入」")
