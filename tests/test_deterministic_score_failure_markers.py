"""P0-3：``deterministic_score`` 必须与重放口径同分数——**失败原文不是「结果」**。

背景（owner 指出并经实测核实）：
- 重放 ``replay()`` 里 ``failed = error or 空输出 or 命中 TOOL_FAILURE_MARKERS`` ⇒ ``0.0``；
- 导出器走 ``deterministic_score(request, trace)``，**不认这批标记** ⇒ 软失败行拿到
  ``0.4``（因为 text 非空），含「来源/出处」字样时再 +0.3 ⇒ 最高 ``0.7``。

同一份轨迹两个读数，Φ0/Φ* 恰好取自重放那一侧，所以导出器这列虽不参与选址与采纳门槛，
但它迟早会被人当成 Φ0 用。口径统一到「命中失败标记 = 0.0」，这个是本批测试的判据。

方向是 fail-safe：宁可把一条成功率不足的输出判成 0，也不给失败原文虚高的分。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from anima.skills import gepa, gepa_trace


TEMPLATE = "把检索结果整理成 3-5 条要点，每条附来源。"


# --- 1. 每一种失败标记都要归零（逐个走列表，不写死示例） -------------------
@pytest.mark.parametrize("marker", gepa.TOOL_FAILURE_MARKERS)
async def test_every_failure_marker_scores_zero(marker: str):
    """每个失败标记单独出现都必须 0.0 —— 防止有人加标记却忘了同步这里。"""
    output = f"{marker}（本次未产出可用结果）"
    assert gepa.deterministic_score("查 X 最新版本", output) == 0.0


# --- 2. 真实形状的软失败原文（工具没抛异常，返回的就是失败描述）----------
@pytest.mark.parametrize("output", [
    "没有搜到与「X 最新版本」相关的可靠结果，建议换个说法。",
    "搜索时出错了，请稍后再试。",
    "联网检索全部通道都不通，暂时没法查。",
    "工具暂时不可用。",
    "未获得可验证结果。",
    "等待主人审批后才能继续。",
])
async def test_soft_failure_text_scores_zero(output: str):
    # RED：修之前这些句子 text 非空 ⇒ 拿 0.4（含「来源」字样可达 0.7）
    assert gepa.deterministic_score("查 X 最新版本", output) == 0.0


# --- 3. 产物带来源/覆盖查询词也不能「救回」失败原文 ----------------------
# 🔴 失败标记的判定必须比「有来源/覆盖」更优先，否则一条失败原文会因为恰好
#    含 URL 和查询词而拿到满分。
async def test_failure_text_is_not_rescued_by_source_or_coverage():
    """失败原文里恰好含 URL 与全部查询词，也必须 0.0（失败优先于有来源/覆盖）。"""
    output = "没有搜到 X 最新版本的资料 https://example.com/a 来源：example.com X 最新版本"
    # RED：修之前 = 0.4 + 0.3(来源) + 0.3(覆盖 3/3) = 1.0 —— 一次失败被判成满分
    assert gepa.deterministic_score("查 X 最新版本", output) == 0.0


# --- 4. 正常成功件不受影响（不许为了修 bug 把好的一起砍了）--------------
# 注：``_query_terms`` 取中文**二元组**，request「查 X 最新版本」⇒ wanted = {最新,新版,版本}。
#    想要 coverage=1.0，输出里必须真的出现「最新版本」这段连续中文。
async def test_successful_output_keeps_full_score():
    request = "查 X 最新版本"
    output = "1. X 最新版本 v2.0 https://example.com/x（来源：官网）"
    # 有结果 0.4 + 有来源 0.3 + 覆盖查询词 0.3
    assert gepa.deterministic_score(request, output) == pytest.approx(1.0)


async def test_successful_output_without_source_scores_0_4():
    # 「暂时查不到相关记载」不含任何失败标记，也不含 URL/www/来源/出处/引用/[数字]
    output = "暂时查不到相关记载。"
    assert gepa.deterministic_score("查 X 最新版本", output) == pytest.approx(0.4)


async def test_empty_output_still_zero():
    assert gepa.deterministic_score("查 X 最新版本", "") == 0.0


# --- 5. 端到端：导出器写出的 score 列与重放判据同号 ---------------------
async def test_exporter_score_matches_replay_verdict(tmp_path: Path):
    """导出器 score 列必须认失败标记 —— 与 replay 的 ``failed`` 判据一致。"""
    audit = tmp_path / "audit.jsonl"
    audit.write_text(json.dumps({
        "type": "tool_executed", "tool": "skill_web_brief",
        "request": "查 X 最新版本", "template": TEMPLATE,
        "result_preview": "没有搜到相关内容，换个关键词试试。", "ok": True, "result_len": 17,
    }, ensure_ascii=False) + "\n", encoding="utf-8")

    out = tmp_path / "T.jsonl"
    export = gepa_trace.export_skill_traces(audit, out, skill_id="web_brief")
    row = export.rows[0]
    assert row.feedback_text, "软失败必须有反馈文本（否则进不了 D_feedback）"
    # RED：修之前这里是 0.4
    assert row.score == 0.0
    assert gepa.deterministic_score(row.request, row.trace) == row.score
