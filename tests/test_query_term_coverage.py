# -*- coding: utf-8 -*-
"""查询词覆盖判据的**词元切分失配**回归（2026-10-11，RED 先行）。

## 症状（生产实测，非构造）

`request='小米 MiMo v2.6 模型 评测 性能'` 的产出写得很好——评分、来源、时间标注全齐
——却只得 **0.85**，因为 `_query_terms` 判它缺了 `mimo / v2.6 / 小米 / 性能 / 模型 / 评测`。

根因是**两侧切分不一致**：

    请求侧  "MiMo v2.6"        → {'mimo', 'v2.6'}          （两个 token，因为中间有空格）
    产出侧  "MiMo-V2.6-Pro"    → {'mimo-v2.6-pro'}         （一个 token，连字符被吞进同一段）

⇒ `v2.6` 在产出里**字面可见**，却因为输出侧没被独立切出来而判缺失。

关键区分（**不能一概放宽**）：
- `v2.6`  ⊂ `mimo-v2.6-pro` 是**真子串** ⇒ 判缺失是误杀（本测试要修的就是它）；
- `小米` 在产出里**真的没出现** ⇒ 判缺失是**正确**的，必须继续判缺失
  （否则覆盖度会退化成"只要提到产品名就算全覆盖"，第 3 步的门槛就失去意义）。

本文件同时钉住"该放宽的放宽、不该放宽的不许放宽"两侧。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from anima.skills import gepa  # noqa: E402


# --------------------------------------------------------------------------
# 1. 词元切分：ASCII 段不许整段吞并相邻的版本号
# --------------------------------------------------------------------------

def test_query_terms_exposes_version_token_inside_hyphenated_name():
    """`MiMo-V2.6-Pro` 里必须能看出 `v2.6` —— 否则与请求侧永远对不上。"""
    terms = gepa._query_terms("MiMo-V2.6-Pro 综合智能指数 46 分")
    assert "v2.6" in terms, f"版本号未被切出：{sorted(terms)}"
    assert "mimo" in terms, f"产品名未被切出：{sorted(terms)}"


def test_query_terms_keeps_full_hyphenated_token_too():
    """放宽不等于破坏：完整 token 仍要在，别把 `mimo-v2.6-pro` 弄丢。"""
    terms = gepa._query_terms("MiMo-V2.6-Pro")
    assert "mimo-v2.6-pro" in terms, f"完整 token 丢失：{sorted(terms)}"


def test_query_terms_existing_behaviour_unchanged():
    """既有语义不许变：中文二元组 + 小写 ASCII 词。"""
    terms = gepa._query_terms("祢saki 梗 meme 更新")
    assert "meme" in terms and "saki" in terms and "更新" in terms
    assert "梗" not in terms          # 单字被 len>=2 过滤（既有行为）
    assert all(t == t.lower() or not t.isascii() for t in terms)


# --------------------------------------------------------------------------
# 2. 打分：真字面覆盖必须拿满分；真缺失仍须扣分
# --------------------------------------------------------------------------

#: 生产原文（10-10 21:36 `小米 MiMo v2.6 模型 评测 性能`），原文照抄不改写。
_PROD_RESPONSE = (
    "1. **综合评测表现**：MiMo-V2.6-Pro 在 Artificial Analysis 综合智能指数中获得 46 分"
    "（来源：大风号_凤凰网，发布时间：2026-09-22），超越 Kimi K3 和 Qwen 3.8 Max。"
)
_PROD_REQUEST = "小米 MiMo v2.6 模型 评测 性能"


def test_literal_substring_coverage_is_not_penalised():
    """🔴 主回归：`v2.6` 字面存在于产出中 ⇒ 不许判缺失。

    基线取自**本测试所用的同一段文本**修复前的读数（0.75），不是拿完整生产原文的
    0.85 —— 基线必须与断言对象同源，否则测的是两回事。
    """
    before = 0.75
    score = gepa.deterministic_score(_PROD_REQUEST, _PROD_RESPONSE)
    assert score > before, (
        f"字面覆盖未计入：request={_PROD_REQUEST!r} trace={_PROD_RESPONSE[:40]!r} "
        f"score={score}（仍 ≤ 修复前 {before}）"
    )
    assert score < 1.0, "产出确实缺「小米/性能/模型」，不该满分"


def test_genuine_missing_terms_still_penalised():
    """不能放宽过头：产出里真没出现的词，仍必须扣分。"""
    trace = "MiMo-V2.6-Pro 的介绍，完全没有提到用户的另外几个词"
    wanted = gepa._query_terms("小米 性能 模型")
    got = gepa._query_terms(trace)
    missing = wanted - got
    assert "小米" in missing and "性能" in missing, (
        f"真缺失被误判为已覆盖：missing={sorted(missing)}"
    )


def test_full_literal_echo_scores_max():
    """上界不变：原词 + 来源标记 ⇒ 1.0（说明判据本身够得着满分）。"""
    assert gepa.deterministic_score(
        _PROD_REQUEST, _PROD_REQUEST + "（来源：https://example.com）"
    ) == 1.0


def test_score_never_regresses_on_unrelated_output():
    """不相关输出不许因为放宽而涨分。"""
    assert gepa.deterministic_score("请求0", "完全无关的内容 https://example.com/a") < 0.8


def test_soft_failure_still_zero_after_loosening():
    """P0-3 不回退：软失败原文仍判 0（放宽词元切分不得绕过它）。"""
    assert gepa.deterministic_score("请求0", "错误: 工具暂时不可用") == 0.0
    assert gepa.deterministic_score("请求0", "没有搜到相关内容") == 0.0


# --------------------------------------------------------------------------
# 3. 真实生产轨迹夹具回归：修复必须精确命中那一条，不许溢出到别的轨迹
# --------------------------------------------------------------------------

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "gepa_prod_traces.jsonl"


def _load_fixture() -> list[dict]:
    import json

    if not _FIXTURE.exists():          # 夹具缺失 ⇒ 跳过而非伪造
        import pytest

        pytest.skip(f"缺少生产轨迹夹具 {_FIXTURE}")
    return [json.loads(line) for line in
            _FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_fixture_fixes_exactly_the_one_broken_trace():
    """🔴 主验收：4 条真实轨迹里**只有**「小米 MiMo v2.6」那条该涨分。

    这条是唯一含 `名称-版本` 连字符写法的，其余三条本来就满分。
    涨分若溢出到别的轨迹，说明放宽过头。
    """
    rows = _load_fixture()
    assert len(rows) == 4, f"夹具条数变了：{len(rows)}"

    expected_before = [1.0, 1.0, 1.0, 0.85]
    expected_after = [1.0, 1.0, 1.0, 0.90]
    for row, want in zip(rows, expected_after):
        got = gepa.deterministic_score(row["request"], row.get("trace") or "")
        assert abs(got - want) < 1e-6, (
            f"{row['request']!r}: 期望 {want}，实得 {got}")

    # 旧的读数确实是这条唯一没满分的 —— 防止夹具被换成"本来就全满分"的假数据
    assert min(expected_before) == 0.85
    assert sum(expected_after) > sum(expected_before)


def test_fixture_phi0_improves_without_reaching_one():
    """Φ0 上升但**不封顶**：产出真缺「小米/性能」，判据不该自我美化。"""
    rows = _load_fixture()
    scores = [gepa.deterministic_score(r["request"], r.get("trace") or "") for r in rows]
    phi0 = sum(scores) / len(scores)
    assert abs(phi0 - 0.975) < 1e-6, f"Φ0={phi0}（期望 0.975）"
    assert phi0 < 1.0, "把真实缺失也判成覆盖 ⇒ 第 3 步门槛会失去意义"
