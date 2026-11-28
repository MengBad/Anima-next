"""D-13 层二（数值 trait 生产路径）的守卫测试 —— 2026-11-29。

设计稿：`design/D13-layer2-numeric-traits.md`（三轮验算，含三个实现级陷阱）。

本文件把设计稿 §3.7.4 的验收基准表钉死。任何一行对不上即实现退化。
"""
from __future__ import annotations

import pytest

from anima.constitution.identity import derive_identity
from anima.constitution.memory_authority import MemoryAuthority
from anima.constitution.need import derive_need
from anima.constitution.reflection_loop import ReflectionSedimenter
from anima.constitution.state import Memory, MemoryRecord


def _memory(ok: int, fail: int) -> Memory:
    """直接构造 trait 证据记录（salience 必须 1.0，见设计稿 §3.7.1）。"""
    m = Memory()
    for _ in range(ok):
        m.semantic.append(MemoryRecord(
            kind="semantic", content={"delivery_ok": 1.0}, salience=1.0))
    for _ in range(fail):
        m.semantic.append(MemoryRecord(
            kind="semantic", content={"delivery_fail": 1.0}, salience=1.0))
    return m


# ── 1. 验收基准表（设计稿 §3.7.4，逐行钉死）─────────────────────────

@pytest.mark.parametrize("ok,fail,expected", [
    (1, 0, 0.667),    # 1/1  Laplace 平滑：小样本向 0.5 收缩，不饱和
    (5, 0, 0.857),
    (10, 0, 0.917),   # 10/10 接近 1 但严格小于 1
    (7, 3, 0.667),
    (3, 2, 0.571),   # 3 成功 2 失败 = 3/5 -> (3+1)/(5+2)=0.571
    (10, 10, 0.5),
    (0, 10, 0.15),    # 全失败触底，且必须 >= 0.15（跳出 AVERSION 阈值）
])
def test_reliability_matches_design_table(ok, fail, expected):
    ident = derive_identity(_memory(ok, fail).snapshot())
    assert ident.traits.get("reliability") == pytest.approx(expected, abs=1e-3)


def test_reliability_never_saturates():
    """三条硬判据之一：reliability 严格在开区间 (0,1) 内。"""
    for ok, fail in ((1, 0), (100, 0), (0, 100), (50, 50)):
        r = derive_identity(_memory(ok, fail).snapshot()).traits["reliability"]
        assert 0.0 < r < 1.0, f"{ok}/{ok+fail} 得到 {r}，越界或饱和"


def test_peak_intensity_within_unit_range():
    """判据之二：peak_intensity 必须落在 [0,1]，且不应因累加顶爆。"""
    for ok, fail in ((1, 0), (7, 3), (10, 10), (0, 10)):
        ident = derive_identity(_memory(ok, fail).snapshot())
        assert 0.0 <= ident.peak_intensity <= 1.0, (
            f"{ok}/{ok+fail} 的 peak_intensity={ident.peak_intensity} 超出 [0,1]"
        )
        assert ident.peak_intensity == pytest.approx(
            max(ident.traits.values()), abs=1e-3)


def test_reliability_exempt_from_normalization():
    """判据之三：归一化不得把 reliability 抬到 1.0（设计稿 §3.7.3 陷阱三）。"""
    ident = derive_identity(_memory(7, 3).snapshot())
    # 若未豁免，归一化会把 0.667/0.667 抬成 1.0
    assert ident.traits["reliability"] == pytest.approx(0.667, abs=1e-3)
    assert ident.traits["reliability"] < 1.0


def test_all_failed_does_not_trigger_bogus_aversion():
    """全失败不得产出「avoid losing: reliability」这种荒谬目标。"""
    ident = derive_identity(_memory(0, 10).snapshot())
    need = derive_need(ident)
    aversions = list(need.vectors.values())[3]
    assert not any("reliability" in str(a) for a in aversions), (
        f"AVERSION 误把 reliability 当可失去之物：{aversions}"
    )


# ── 2. 写入链：成功与失败都必须被计数 ──────────────────────────────

def test_failed_carrying_still_counts():
    """FAILED 承载走 filtered 分支，但**必须**留下 trait 证据。

    否则 reliability 只统计成功（实测 7 胜 3 负会算成 8/9 而非 7/10）。
    """
    auth = MemoryAuthority()
    sed = ReflectionSedimenter(auth, max_records=500)
    for i in range(10):
        sed.sediment(plan_ref=f"hb#{i}", succeeded=(i < 7), outcome="测试")

    ident = derive_identity(auth.snapshot_memory())
    assert ident.traits["reliability"] == pytest.approx(0.667, abs=1e-3), (
        f"7 胜 3 负应为 0.667，实得 {ident.traits}"
    )
    assert sed._trait_evidence == 10, "10 次承载应产生 10 条 trait 证据"


def test_trait_records_use_unit_salience():
    """设计稿 §3.7.1 陷阱一：trait 记录必须 salience=1.0。

    用 0.5 会让计数减半、破坏 Laplace 平滑先验（实测全部塌缩到 0.667）。
    """
    auth = MemoryAuthority()
    sed = ReflectionSedimenter(auth, max_records=500)
    sed.sediment(plan_ref="p1", succeeded=True, outcome="ok")
    recs = [r for r in auth.snapshot_memory().semantic
            if "delivery_ok" in r.content]
    assert recs, "应写入 trait 证据记录"
    assert all(r.salience == 1.0 for r in recs), (
        f"trait 记录 salience 必须 1.0，实得 {[r.salience for r in recs]}"
    )


def test_ingested_excludes_trait_evidence():
    """`ingested` 只统计 reflect() 沉淀，不含 trait 证据（语义分离）。"""
    auth = MemoryAuthority()
    sed = ReflectionSedimenter(auth, max_records=500)
    out = sed.sediment(plan_ref="p1", succeeded=True, outcome="ok")
    assert sed.ingested == out["records"], (
        "ingested 应等于 reflect 沉淀的记录数，不含 trait 证据"
    )
    assert sed._trait_evidence == 1
