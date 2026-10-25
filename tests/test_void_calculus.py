# -*- coding: utf-8 -*-
"""Void Calculus 契约测试（2026-10-25）。

## 为什么需要它

`void_calculus.py` 有 **546 行**，建模的是「**缺席**」—— 一个在 QQ 群里
高频但当前系统完全缺失的维度：

```
· 被 @ 了但没被回
· 说了话没人接
· 话题被突然岔开（在回避什么）
· 对方沉默 2 小时不回私聊
```

现有 `scar.py` 只记**发生过的**负面事件；void 记的是**没发生的**。
两者互补，不是替代。

上游实测：`void_calculus` **0 个测试文件引用** —— 546 行零覆盖。
本文件先钉契约，为将来接线提供回归网。

## 测什么

✅ 沉默质感分类的分支边界（4 种质感、阈值）
✅ 虚空生命周期（创生 / 存活 / 死亡 / 残影）
✅ 压力动力学（随年龄增长、封顶、不越界）
✅ 容量与抗性（max_voids、ghost_bonus、cooldown）
✅ 合并 / 分裂 / 收缩的**结构不变量**（不要求数值精确）
✅ 序列化往返
✅ 极端输入不抛（NaN / 空向量 / 相似度恒 0 或恒 1）

❌ 不测具体数值精度（无上游理论文档可对照）
"""
from __future__ import annotations

import importlib.util
import math
import pathlib
import sys

import pytest

_SRC = (pathlib.Path(__file__).resolve().parents[1]
        / "anima" / "cognitive" / "sheaf" / "void_calculus.py")
_UPSTREAM = pathlib.Path("G:/Anima/anima/sylanne_alpha/void_calculus.py")


@pytest.fixture(scope="module")
def vc():
    if not _SRC.exists():
        pytest.fail(f"仓库内副本缺失：{_SRC}")
    spec = importlib.util.spec_from_file_location("_void_calc_under_test", _SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _sim_identical(a: bytes, b: bytes) -> float:
    """相同向量相似度 1，不同向量 0（确定性桩，便于断言结构）。"""
    return 1.0 if a == b else 0.0


def _sim_always(level: float):
    return lambda a, b: level


@pytest.fixture
def space(vc):
    return vc.VoidSpace(similarity_fn=_sim_identical)


# ── 1. 沉默质感分类（对应 QQ 群真实场景）────────────────────────────

def test_silence_content_when_short_and_positive(vc):
    """短沉默 + 情绪正面 ⇒ 满足（不用打破）。"""
    assert vc.SilenceTexture.classify(60, 0.5, 0.8) == vc.SilenceTexture.CONTENT


def test_silence_digesting_when_negative(vc):
    """中等沉默 + 情绪负面 ⇒ 在消化（给空间）。"""
    assert vc.SilenceTexture.classify(600, -0.6, 0.5) == vc.SilenceTexture.DIGESTING


def test_silence_distant_when_long_and_cold(vc):
    """长沉默 + 关系冷 ⇒ 疏远（轻量问候）。"""
    assert vc.SilenceTexture.classify(8000, 0.0, 0.1) == vc.SilenceTexture.DISTANT


def test_silence_waiting_is_the_default(vc):
    """其余情形统一落到 waiting（有人可能还在等）。"""
    assert vc.SilenceTexture.classify(400, 0.0, 0.9) == vc.SilenceTexture.WAITING


@pytest.mark.parametrize("texture", ["waiting", "digesting", "distant", "content"])
def test_every_texture_has_a_breaker(vc, texture):
    """🔴 每种质感都必须有破冰策略 —— 漏一种就等于那种沉默没人管。"""
    got = vc.get_silence_breaker(texture)
    assert got and isinstance(got, str)


def test_unknown_texture_falls_back_safely(vc):
    """未知质感不得抛（生产里可能出现脏数据）。"""
    got = vc.get_silence_breaker("完全不存在的质感")
    assert isinstance(got, str)


def test_silence_classify_is_total(vc):
    """任意输入都必须返回四者之一，不得返回 None 或抛。"""
    valid = {vc.SilenceTexture.WAITING, vc.SilenceTexture.DIGESTING,
             vc.SilenceTexture.DISTANT, vc.SilenceTexture.CONTENT}
    for dur in (0, 1, 299, 300, 1799, 1800, 7200, 7201, 1e9):
        for val in (-1.0, -0.31, 0.0, 1.0):
            for warm in (0.0, 0.29, 0.3, 1.0):
                assert vc.SilenceTexture.classify(dur, val, warm) in valid


# ── 2. Void 生命周期 ─────────────────────────────────────────────────

def test_void_alive_when_boundary_non_empty(vc):
    v = vc.Void(boundary=[b"\x01"])
    assert v.is_alive is True
    assert v.is_ghost is False


def test_void_ghost_when_boundary_empty_but_depth_positive(vc):
    """边界清空但留了深度 ⇒ 残影（旧伤留下的敏感度）。"""
    v = vc.Void(boundary=[], depth=0.5)
    assert v.is_alive is False
    assert v.is_ghost is True


def test_empty_void_without_depth_is_neither(vc):
    v = vc.Void(boundary=[], depth=0.0)
    assert v.is_alive is False
    assert v.is_ghost is False


def test_boundary_completeness_is_bounded(vc):
    for n in (0, 1, 5, 100):
        v = vc.Void(boundary=[bytes([i % 256]) for i in range(n)])
        c = v.boundary_completeness
        assert 0.0 <= c <= 1.0, f"完整度越界：{c}（boundary={n}）"


def test_boundary_completeness_increases_with_more_boundary(vc):
    """边界点越多越完整（源码公式：len/(len+estimated)）。"""
    few = vc.Void(boundary=[b"\x00"])
    many = vc.Void(boundary=[bytes([i]) for i in range(20)])
    assert few.boundary_completeness < many.boundary_completeness


# ── 3. 压力动力学（核心心理模型）────────────────────────────────────

def test_pressure_grows_with_age(vc):
    """「越是回避的话题，内心压力越大」—— 压力必须随年龄增长。"""
    v = vc.Void(boundary=[b"\x01"], depth=0.5)
    v.tick()
    first = v.pressure
    for _ in range(10):
        v.tick()
    assert v.pressure > first, "压力未随年龄增长"
    assert v.age == 11


def test_zero_depth_void_accumulates_no_pressure(vc):
    """深度为 0 的虚空不产生压力（它只是被注意到，还没变成心结）。"""
    v = vc.Void(boundary=[b"\x01"], depth=0.0)
    for _ in range(20):
        v.tick()
    assert v.pressure == 0.0


def test_pressure_is_capped(vc):
    v = vc.Void(boundary=[b"\x01"], depth=1.0)
    for _ in range(500):
        v.tick(pressure_cap=5.0)
    assert v.pressure <= 5.0 + 1e-9, f"压力越界：{v.pressure}"


def test_pressure_never_negative(vc):
    v = vc.Void(boundary=[b"\x01"], depth=-1.0)
    for _ in range(20):
        v.tick()
    assert v.pressure >= 0.0


def test_pressure_is_finite(vc):
    v = vc.Void(boundary=[b"\x01"], depth=1e9)
    for _ in range(100):
        v.tick()
    assert math.isfinite(v.pressure), f"压力出现 NaN/Inf：{v.pressure}"


# ── 4. process() 主入口契约 ─────────────────────────────────────────

def test_process_returns_expected_keys(space):
    out = space.process(b"\x01", surprise=0.0, prev_similarity=1.0)
    for k in ("voids_contracted", "voids_deepened", "voids_born",
              "voids_died", "total_pressure", "coupling_events",
              "active_voids", "ghosts"):
        assert k in out, f"process 输出缺 {k}"


def test_process_increments_tick(space):
    space.process(b"\x01", 0.0, 1.0)
    assert space._tick == 1
    space.process(b"\x01", 0.0, 1.0)
    assert space._tick == 2


def test_high_surprise_plus_topic_shift_creates_void(space):
    """🔴 核心语义：高惊讶 + 话题突然偏转 ⇒ 可能正在回避什么 ⇒ 创生虚空。"""
    out = space.process(b"\x01", surprise=0.9, prev_similarity=0.0)
    assert out["voids_born"] == 1
    assert len(space.voids) == 1


def test_low_surprise_creates_nothing(space):
    out = space.process(b"\x01", surprise=0.1, prev_similarity=0.0)
    assert out["voids_born"] == 0


def test_no_shift_creates_nothing(space):
    """惊讶高但话题没偏转 ⇒ 不是回避。"""
    out = space.process(b"\x01", surprise=0.9, prev_similarity=1.0)
    assert out["voids_born"] == 0


def test_cooldown_blocks_immediate_recreation(space):
    space.process(b"\x01", 0.9, 0.0)
    out = space.process(b"\x02", 0.9, 0.0)
    assert out["voids_born"] == 0, "冷却期内不得连续创生"


def test_max_voids_is_respected(vc):
    s = vc.VoidSpace(similarity_fn=_sim_identical, max_voids=2)
    for i in range(20):
        s._creation_cooldown = 0  # 绕过冷却，专测容量上限
        s.process(bytes([i]), surprise=0.9, prev_similarity=0.0)
    assert len(s.voids) <= 2, f"虚空数量超过上限：{len(s.voids)}"


# ── 5. 收缩（被正面面对 ⇒ 压力释放）────────────────────────────────

def test_contract_removes_similar_boundary(space):
    """用户主动谈及被回避的话题 ⇒ 边界被侵蚀。

    ⚠️ 边界只有 1 点时收缩后**立即死亡**（`_reap_dead` 在同一 process 里跑），
    所以这里要给 2 个边界点，才能观察到「收缩但还活着」的中间态。

    ⚠️ 不能断言「压力下降」—— `process()` 先 `tick()` 所有虚空（age+1 ⇒ 压力按
    `depth*log(age+1)` 增长），再做收缩衰减。压力是**两个力的合力**，
    收缩瞬间的净值可能为正。这里只断言**边界**（收缩的实际效果）。
    """
    space.process(b"\x01", 0.9, 0.0)          # 创生 → 边界 [b"\x01"]
    space.voids[0].boundary = [b"\x01", b"\x02"]  # 补一个点，避免一下抽干
    space.voids[0].depth = 0.5
    out = space.process(b"\x01", 0.0, 1.0)    # 只命中 b"\x01"
    assert out["voids_contracted"] == 1
    assert len(space.voids) == 1, "还有一个边界点 ⇒ 虚空仍存活"
    assert space.voids[0].boundary == [b"\x02"], "被谈及的边界点必须移除"


def test_contract_emptying_sole_boundary_kills_void(space):
    """边界只有 1 点且被收缩 ⇒ 虚空死亡（这是设计，不是 bug）。"""
    space.process(b"\x01", 0.9, 0.0)
    out = space.process(b"\x01", 0.0, 1.0)
    assert out["voids_died"] == 1
    assert len(space.voids) == 0


def test_contract_keeps_dissimilar_boundary(space):
    space.process(b"\x01", 0.9, 0.0)
    space.process(b"\x02", 0.0, 1.0)          # 不相似 ⇒ 不收缩
    assert len(space.voids[0].boundary) == 1


def test_contract_never_makes_pressure_negative(space):
    space.process(b"\x01", 0.9, 0.0)
    space.voids[0].boundary = [b"\x01", b"\x02"]
    space.voids[0].pressure = 0.0
    space.process(b"\x01", 0.0, 1.0)
    if space.voids:  # 存活时校验；抽干后已死，无压力可言
        assert space.voids[0].pressure >= 0.0


# ── 6. 死亡与残影 ───────────────────────────────────────────────────

def test_void_dies_when_boundary_emptied(space):
    space.process(b"\x01", 0.9, 0.0)
    space.voids[0].depth = 0.5               # 有深度 ⇒ 死后留残影
    out = space.process(b"\x01", 0.0, 1.0)   # 边界被清空
    assert out["voids_died"] == 1
    assert len(space.voids) == 0
    assert len(space.ghosts) == 1, "有深度的虚空死亡必须留下残影"


def test_shallow_void_dies_without_ghost(space):
    """深度 0 的虚空死亡后不留残影（它没成为心结）。"""
    space.process(b"\x01", 0.9, 0.0)
    space.process(b"\x01", 0.0, 1.0)
    assert len(space.ghosts) == 0


def test_ghost_count_is_capped(vc):
    s = vc.VoidSpace(similarity_fn=_sim_identical)
    s.ghosts = [vc.VoidGhost(depth=0.6, age_at_death=i) for i in range(200)]
    s._reap_dead()
    assert len(s.ghosts) <= 50, f"残影未裁剪：{len(s.ghosts)}"


def test_ghost_lowers_creation_threshold(vc):
    """残影 = 旧伤留下的敏感度 ⇒ 附近更容易形成新虚空。

    🔴 这条测试抓出了上游的一个**真实缺陷**（本仓库已修）：
    `_should_create_void` 原本在开头有硬门槛
    `if surprise < self._detection_threshold: return False`，
    而下面算出的 `effective_threshold`（被 ghost_bonus 降低过）永远走不到 ——
    实测残影 0/1/5/50 个时 surprise=0.35 的结果**全部 False**，
    「旧伤附近更敏感」是死代码。修复后本条才可能通过。
    """
    plain = vc.VoidSpace(similarity_fn=_sim_identical, detection_threshold=0.4)
    sensitive = vc.VoidSpace(similarity_fn=_sim_identical, detection_threshold=0.4)
    sensitive.ghosts = [vc.VoidGhost(depth=0.8, age_at_death=10)]
    # surprise=0.35 低于 detection_threshold(0.4)，但残影把门槛降到 0.30
    assert plain._should_create_void(b"\x01", 0.35, 0.0) is False, \
        "无残影时 0.35 不该创生"
    assert sensitive._should_create_void(b"\x01", 0.35, 0.0) is True, \
        "残影没有降低创生阈值（ghost_bonus 仍是死代码？）"


def test_ghost_sensitivity_is_documented_dead_code_regression(vc):
    """回归：残影数量必须**单调影响**结果（0 个 vs 多个 有差异）。

    这是上一条的加强版 —— 直接验证「残影数量改变结果」这个单调性。
    """
    results = []
    for n in (0, 3):
        s = vc.VoidSpace(similarity_fn=_sim_identical, detection_threshold=0.4)
        s.ghosts = [vc.VoidGhost(depth=0.9, age_at_death=10) for _ in range(n)]
        results.append(s._should_create_void(b"\x01", 0.32, 0.0))
    assert results == [False, True], (
        f"残影对创生阈值无影响（0 个={results[0]}, 3 个={results[1]}）"
        " ⇒ ghost_bonus 又变成死代码了")


def test_high_surprise_still_creates_without_ghost(vc):
    """修 bug 后不得把「无残影时高惊讶创生」也弄坏。"""
    s = vc.VoidSpace(similarity_fn=_sim_identical, detection_threshold=0.4)
    assert s._should_create_void(b"\x01", 0.9, 0.0) is True


def test_more_voids_raises_creation_threshold(vc):
    """抗性：已有虚空越多越难创生（防虚空爆炸）。"""
    s = vc.VoidSpace(similarity_fn=_sim_identical)
    # 5 个虚空 ⇒ effective_threshold = 0.4 + 5*0.02 = 0.5
    s.voids = [vc.Void(boundary=[bytes([i])]) for i in range(5)]
    assert s._should_create_void(b"\xff", 0.45, 0.0) is False, \
        "5 个虚空后 0.45 的惊讶度不该再创生"
    assert s._should_create_void(b"\xff", 0.55, 0.0) is True


# ── 7. 合并 / 分裂的结构不变量 ─────────────────────────────────────

def test_merge_combines_overlapping_voids(vc):
    s = vc.VoidSpace(similarity_fn=_sim_always(0.9), merge_threshold=0.7)
    s.voids = [vc.Void(boundary=[b"\x01"], depth=0.2),
               vc.Void(boundary=[b"\x02"], depth=0.4)]
    s._merge_pass()
    assert len(s.voids) == 1, "重叠虚空必须合并"
    assert s.voids[0].depth == 0.4, "合并取较强深度"
    assert s.voids[0].pressure <= 5.0, "合并后压力必须封顶"


def test_merge_keeps_dissimilar_voids(vc):
    s = vc.VoidSpace(similarity_fn=_sim_always(0.1), merge_threshold=0.7)
    s.voids = [vc.Void(boundary=[b"\x01"]), vc.Void(boundary=[b"\x02"])]
    s._merge_pass()
    assert len(s.voids) == 2


def test_split_requires_minimum_boundary_size(vc):
    """边界 < 4 点不尝试分裂（避免碎片化）。"""
    s = vc.VoidSpace(similarity_fn=_sim_always(0.1))
    s.voids = [vc.Void(boundary=[b"\x01", b"\x02"], depth=0.3, pressure=1.0)]
    s._split_pass()
    assert len(s.voids) == 1


def test_split_preserves_total_pressure(vc):
    """分裂后两块压力各半 ⇒ 总和不变（能量守恒的朴素要求）。"""
    s = vc.VoidSpace(similarity_fn=_sim_always(0.1))
    s.voids = [vc.Void(boundary=[bytes([i]) for i in range(6)],
                       depth=0.3, pressure=2.0)]
    total_before = s.total_pressure()
    s._split_pass()
    assert abs(s.total_pressure() - total_before) < 1e-9 or len(s.voids) == 1


# ── 8. 耦合输出 ─────────────────────────────────────────────────────

def test_coupling_output_only_over_threshold(space):
    space.process(b"\x01", 0.9, 0.0)
    space.voids[0].pressure = 99.0
    out = space.coupling_output()
    assert out, "超阈值的虚空必须被耦合输出"
    assert all("pressure" in e for e in out)


def test_coupling_output_empty_when_below_threshold(space):
    space.process(b"\x01", 0.9, 0.0)
    space.voids[0].pressure = 0.0
    assert space.coupling_output() == []


def test_process_reports_coupling_events(space):
    space.process(b"\x01", 0.9, 0.0)
    out = space.process(b"\x01", 0.0, 1.0)
    assert isinstance(out["coupling_events"], list)


# ── 9. 序列化 ───────────────────────────────────────────────────────

def test_roundtrip_preserves_voids_and_ghosts(vc):
    s = vc.VoidSpace(similarity_fn=_sim_identical)
    s.process(b"\x01", 0.9, 0.0)
    s.voids[0].depth = 0.7
    s.voids[0].pressure = 1.5
    s.ghosts = [vc.VoidGhost(depth=0.6, age_at_death=3)]
    data = s.to_dict()

    s2 = vc.VoidSpace(similarity_fn=_sim_identical)
    s2.from_dict(data)
    assert len(s2.voids) == len(s.voids)
    assert s2.voids[0].boundary == s.voids[0].boundary
    assert abs(s2.voids[0].depth - s.voids[0].depth) < 1e-9
    assert len(s2.ghosts) == 1


def test_tick_survives_roundtrip(vc):
    s = vc.VoidSpace(similarity_fn=_sim_identical)
    for _ in range(5):
        s.process(b"\x01", 0.0, 1.0)
    s2 = vc.VoidSpace(similarity_fn=_sim_identical)
    s2.from_dict(s.to_dict())
    assert s2._tick == s._tick


def test_from_dict_tolerates_empty(vc):
    s = vc.VoidSpace(similarity_fn=_sim_identical)
    s.from_dict({})
    assert s.voids == []
    assert s.ghosts == []


# ── 10. diagnostics ─────────────────────────────────────────────────

def test_diagnostics_keys(space):
    d = space.diagnostics()
    for k in ("active_voids", "ghosts", "total_pressure", "max_depth", "tick"):
        assert k in d


def test_diagnostics_on_empty_space_is_safe(space):
    d = space.diagnostics()
    assert d["active_voids"] == 0
    assert d["max_depth"] == 0.0
    assert d["total_pressure"] == 0.0


# ── 11. set_personality_params ──────────────────────────────────────

def test_personality_params_take_effect(space):
    space.set_personality_params(0.9, 0.9, 0.9, 1.0)
    assert space._contract_threshold == 0.9
    assert space._split_threshold == 0.9
    assert space._merge_threshold == 0.9
    assert space._pressure_cap == 1.0


def test_cooldown_scales_with_openness(space):
    """开放性越高，冷却越长（不急于形成新虚空）。"""
    space.set_cooldown(0.0)
    low = space._cooldown_duration
    space.set_cooldown(1.0)
    high = space._cooldown_duration
    assert high > low


# ── 12. 极端输入不抛（生产 fail-closed）────────────────────────────

def test_process_survives_extreme_surprise(space):
    for s in (-1e9, -1.0, 0.0, 1.0, 1e9, float("inf")):
        space._creation_cooldown = 0
        space.process(b"\x01", s, 0.0)  # 不抛即通过


def test_process_survives_empty_vector(space):
    space.process(b"", 0.9, 0.0)


def test_process_survives_similarity_always_one(vc):
    s = vc.VoidSpace(similarity_fn=_sim_always(1.0))
    for _ in range(10):
        s._creation_cooldown = 0
        s.process(b"\x01", 0.9, 0.0)


def test_process_survives_similarity_always_zero(vc):
    s = vc.VoidSpace(similarity_fn=_sim_always(0.0))
    for _ in range(10):
        s._creation_cooldown = 0
        s.process(b"\x01", 0.9, 0.0)


# ── 13. 迁移前置判据 ────────────────────────────────────────────────

def test_source_has_no_astrbot_dependency():
    assert "astrbot" not in _SRC.read_text(encoding="utf-8")


def test_source_has_no_io():
    src = _SRC.read_text(encoding="utf-8")
    for forbidden in ("open(", "aiohttp", "sqlite", "requests."):
        assert forbidden not in src, f"出现 I/O 调用 {forbidden}"


def test_copy_differs_from_upstream_only_by_known_fix():
    """🔴 仓库内副本**有意**与上游不同 —— 差异必须只有那一处 bug 修复。

    上游 `_should_create_void` 有硬门槛让 ghost_bonus 变死代码（见
    `test_ghost_lowers_creation_threshold` 的 docstring）。本仓库删了那道门。

    本测试的作用：**防止后来者顺手改别的**。若上游不可用则跳过
    （一致性的绝对值无法校验，但「别的地方没动」这件事在本机可验）。
    """
    if not _UPSTREAM.exists():
        pytest.skip(f"上游原件不在本机（{_UPSTREAM}）")
    ours = _SRC.read_text(encoding="utf-8").splitlines()
    theirs = _UPSTREAM.read_text(encoding="utf-8").splitlines()
    # 允许的差异：删掉硬门槛 2 行 + 加注释/文档（净行数应只减不增的断言太脆，
    # 这里改为「上游的每一行要么还在，要么属于被删的那 2 行」）
    removed_gate = "        if surprise < self._detection_threshold:"
    our_blob = "\n".join(ours)
    # 1. 硬门槛确实被删了
    assert removed_gate not in our_blob, "上游的硬门槛还在（bug 未修）"
    # 2. 上游其它行仍全部存在（顺序允许被注释打断，故逐行包含检查）
    missing = [
        ln for ln in theirs
        if ln.strip() and ln.strip() != removed_gate.strip()
        and ln not in our_blob
        # 被删门槛的分支体（return False）我们仍保留在别处，不作强制
    ]
    assert not missing, (
        "副本与上游的差异超出「已知 bug 修复」范围，缺少这些行：\n"
        + "\n".join(missing[:10])
    )
