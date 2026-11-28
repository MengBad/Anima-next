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
from anima.constitution.state import Memory, MemoryRecord, WantVector


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


# ── 3. 阶段 2：resilience 与 reliability 正交 ────────────────────────

def _seq(name: str, seq: list[bool]) -> dict:
    """按给定成败序列跑真实 sedimenter，返回 traits。"""
    auth = MemoryAuthority()
    sed = ReflectionSedimenter(auth, max_records=500)
    for i, ok in enumerate(seq):
        sed.sediment(plan_ref=f"{name}#{i}", succeeded=ok, outcome="x")
    return derive_identity(auth.snapshot_memory()).traits


def test_resilience_is_orthogonal_to_reliability():
    """resilience 必须提供 reliability 之外的信息。

    若把 caution 定义为 fail/total，它与 reliability 恒互补（和为 1）——
    那是同一信息的两种写法，无法表达「很不可靠但很坚持」。
    """
    # 低可靠 + 高韧性：交替失败/继续
    low_rel_high_res = []
    for _ in range(8):
        low_rel_high_res.extend([False, True])
    t1 = _seq("lrhr", low_rel_high_res)

    # 低可靠 + 低韧性：连续失败后不再尝试
    t2 = _seq("lrlr", [True] + [False] * 8)

    assert t1["reliability"] < 0.6, f"应低可靠，实得 {t1}"
    assert t1.get("resilience", 0) > 0.8, f"应高韧性，实得 {t1}"
    assert t2["reliability"] < 0.6, f"也应低可靠，实得 {t2}"
    assert "resilience" not in t2, f"没有继续尝试就不该有韧性 trait，实得 {t2}"


def test_resilience_absent_without_failures():
    """从没失败过 ⇒ 推不出韧性，不编造该 trait。"""
    t = _seq("allok", [True] * 10)
    assert "resilience" not in t, f"无失败样本不应产 resilience，实得 {t}"
    assert "reliability" in t


def test_resilience_exempt_from_normalization():
    """resilience 与 reliability 同为 0–1 状态量，都必须豁免归一化。"""
    t = _seq("mix", [True] * 8 + [False, True])
    assert t.get("resilience") is not None
    assert t["resilience"] < 1.0, f"归一化会把 0-1 状态量抬到 1.0，实得 {t}"


# ── 4. 阶段 3：翻门前置检查脚本 ─────────────────────────────────────

def test_drive_gate_readiness_script_exists():
    """必须有可执行的翻门检查，不能靠感觉翻。"""
    from pathlib import Path

    script = Path("scripts/check_drive_gate_readiness.py")
    assert script.is_file()
    body = script.read_text(encoding="utf-8")
    assert "peak_intensity" in body
    assert "CURIOSITY" in body
    assert "主动发言" in body, "必须检查条件 3：翻转后主动发言量不下降"


def test_drive_gate_still_off():
    """层二落地**不得**顺带翻转 drive_gate —— 那是独立决策。"""
    from anima.core.config import AutonomyConfig

    assert AutonomyConfig().drive_gate is False


# ── 5. 每日翻门就绪巡检（阶段 3 自动化）────────────────────────────

def _fake_heartbeat(traits: dict, peak: float, curiosity: list[str]):
    """构造一个只够跑巡检的最小 Heartbeat 替身。"""
    from types import SimpleNamespace
    from anima.core.autonomy import Heartbeat

    sent: list[str] = []

    class _GW:
        async def send(self, msg):
            sent.append(getattr(msg, "message", ""))
            return 1

    # 必须返回真实的空 Memory（不是 None）—— derive_identity 会遍历它的字段
    from anima.constitution.state import Memory
    auth = SimpleNamespace(snapshot_memory=lambda: Memory())
    hb = object.__new__(Heartbeat)
    hb.core = SimpleNamespace(
        config=SimpleNamespace(
            permissions=SimpleNamespace(owners=[12345]),
            autonomy=SimpleNamespace(drive_gate_readiness_check_enabled=True),
        ),
        gateway=_GW(),
        memory_authority=auth,
    )
    hb._drive_gate_mark = {}
    return hb, sent


def test_readiness_tick_skips_when_not_ready(monkeypatch):
    """未就绪时**不打扰** owner（项目取向：宁可安静）。"""
    import anima.constitution.identity as ident_mod
    import anima.constitution.need as need_mod
    from types import SimpleNamespace
    from anima.core.autonomy import Heartbeat

    monkeypatch.setattr(ident_mod, "derive_identity",
                        lambda m: SimpleNamespace(traits={}, peak_intensity=0.0))
    monkeypatch.setattr(need_mod, "derive_need",
                        lambda i: SimpleNamespace(vectors={WantVector.CURIOSITY: []}))

    hb, sent = _fake_heartbeat({}, 0.0, [])
    hb._bj_date_str = lambda ts: "2026-11-29"
    import asyncio
    asyncio.run(hb._drive_gate_readiness_tick(0.0))
    assert sent == [], f"未就绪不应发消息，实发 {sent}"


def test_readiness_tick_notifies_once_on_transition(monkeypatch):
    """就绪时提醒**一次**，同日不重复（不打卡）。"""
    import anima.constitution.identity as ident_mod
    import anima.constitution.need as need_mod
    from types import SimpleNamespace

    # 注意：被测方法内部是 `from X import Y`，故必须 patch 模块属性；
    # 且 `snapshot_memory` 必须返回非 None（真实实现会遍历它）。
    monkeypatch.setattr(ident_mod, "derive_identity",
                        lambda m: SimpleNamespace(
                            traits={"reliability": 0.667}, peak_intensity=0.667))
    monkeypatch.setattr(need_mod, "derive_need", lambda i: SimpleNamespace(
        vectors={WantVector.CURIOSITY: ["explore: reliability"]}))

    hb, sent = _fake_heartbeat({"reliability": 0.667}, 0.667, ["explore: reliability"])
    hb._bj_date_str = lambda ts: "2026-11-29"
    import asyncio

    asyncio.run(hb._drive_gate_readiness_tick(0.0))
    assert len(sent) == 1, f"首次就绪应提醒一次，实发 {len(sent)}"
    assert "drive_gate" in sent[0]

    # 同日再跑：已提醒过，不再发
    asyncio.run(hb._drive_gate_readiness_tick(1.0))
    assert len(sent) == 1, "同日不得重复提醒"


def test_readiness_tick_resets_after_not_ready(monkeypatch):
    """状态回落（不再就绪）后清标记，将来重新就绪能再提醒一次。"""
    import anima.constitution.identity as ident_mod
    import anima.constitution.need as need_mod
    from types import SimpleNamespace
    import asyncio

    hb, sent = _fake_heartbeat({}, 0.0, [])
    hb._bj_date_str = lambda ts: "2026-11-29"
    hb._drive_gate_mark = {"notified_ready": "2026-11-28"}

    monkeypatch.setattr(ident_mod, "derive_identity",
                        lambda m: SimpleNamespace(traits={}, peak_intensity=0.0))
    monkeypatch.setattr(need_mod, "derive_need",
                        lambda i: SimpleNamespace(vectors={WantVector.CURIOSITY: []}))

    asyncio.run(hb._drive_gate_readiness_tick(0.0))
    assert "notified_ready" not in hb._drive_gate_mark, (
        "未就绪时应清除已提醒标记，否则将来就绪不会再提醒"
    )


def test_readiness_rejects_saturated_peak(monkeypatch):
    """peak 饱和(>=1.0) 不算就绪 —— 那正是我们要防的退化形态。"""
    import anima.constitution.identity as ident_mod
    import anima.constitution.need as need_mod
    from types import SimpleNamespace
    import asyncio

    monkeypatch.setattr(ident_mod, "derive_identity",
                        lambda m: SimpleNamespace(
                            traits={"reliability": 1.0}, peak_intensity=1.0))
    monkeypatch.setattr(need_mod, "derive_need", lambda i: SimpleNamespace(
        vectors={WantVector.CURIOSITY: ["explore: reliability"]}))

    hb, sent = _fake_heartbeat({}, 0.0, [])
    hb._bj_date_str = lambda ts: "2026-11-29"
    asyncio.run(hb._drive_gate_readiness_tick(0.0))
    assert sent == [], "peak 饱和时不应判定就绪"
