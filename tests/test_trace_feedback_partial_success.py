# -*- coding: utf-8 -*-
"""feedback 通道捕获「部分成功」里的缺失信号（2026-10-11）。

## 生产实测的漏网
10-10 的 4 条真实 `skill_web_brief` 轨迹里，有 2 条明确写了「没查到」，
但 `TOOL_FAILURE_MARKERS` 里只有「没有搜到」⇒ **一条都没被捕获**，
D_feedback 产出速率 = 0/天（第二道瓶颈）。

## 为什么不能直接往 TOOL_FAILURE_MARKERS 里加词
这个词表被 `deterministic_score` 短路（P0-3），加词会把这 2 条从 1.00 打成 0.00。
但它们是**部分成功**：既说了「没查到」，也给了可用内容（YouTube 视频、版本差异）
⇒ 判 0 是误杀（同类教训：护栏改写要「只切命中句」，不能整段替换。

## 真正的区分
- ``deterministic_score`` 问的是「**任务做成没有**」⇒ 部分成功就该拿部分以上的分；
- ``feedback_text`` 问的是「**哪里还能改**」⇒ 缺的那部分本身就是最好的反馈。

两者本来就不该共用一套判据。所以：
- 硬失败（``TOOL_FAILURE_MARKERS``）继续同时影响两者，行为不变；
- 新增软失败词表（``SOFT_FAILURE_MARKERS``）**只喂 feedback，不碰 score**；
- feedback 只写**命中句**，不整段搬（缺的那部分才是反馈，做成的部分不是）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from anima.skills import gepa, gepa_trace

TEMPLATE = "把检索结果整理成 3-5 条要点，每条附来源。"

#: 生产真实形态：一条要点说「没查到」，另一条给了可用内容。
PARTIAL_SUCCESS = (
    "1. 关于该梗在 2026-10-10 的具体更新：没查到（来源：Tavily，抓取时间：2026-10-10）。\n"
    "2. YouTube 存在名为《Saki-Saki Memes Be Like》的视频，播放量为 58,824 次"
    "（来源：YouTube，发布时间：2022-05-06）。"
)


def _event(**over: object) -> dict:
    row = {
        "type": "tool_executed",
        "tool": "skill_web_brief",
        "request": "祢saki 梗 meme 更新",
        "template": TEMPLATE,
        "result_preview": PARTIAL_SUCCESS,
        "ok": True,
        "result_len": len(PARTIAL_SUCCESS),
        "id": "e1",
    }
    row.update(over)
    return row


def _write(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")
    return path


def _row(tmp_path, **over):
    audit = _write(tmp_path / "a.jsonl", [_event(**over)])
    return gepa_trace.extract_skill_traces(
        gepa_trace.load_audit_rows(audit), "web_brief").rows[0]


# --- 1. 漏网本身 ---------------------------------------------------------
async def test_partial_success_now_yields_feedback(tmp_path):
    """RED：说了「没查到」就该有反馈。当前词表只有「没有搜到」⇒ 整条漏网。"""
    row = _row(tmp_path)
    assert row.feedback_text, "部分成功里的缺失信号完全漏网（D_feedback 速率因此为 0）"


# --- 2. 句级精准：只搬缺失句，不搬做成句 ---------------------------------
async def test_feedback_carries_only_the_missing_sentence(tmp_path):
    """feedback 是「哪里还能改」⇒ 只该含缺的那句，不该把做成的内容一起搬走。"""
    row = _row(tmp_path)
    fb = row.feedback_text
    assert "没查到" in fb, f"缺失信号没进反馈: {fb!r}"
    assert "YouTube" not in fb and "58,824" not in fb, (
        f"把做成的内容也搬进了反馈（整段搬运，会稀释真正的问题）: {fb!r}")


async def test_feedback_is_shorter_than_the_whole_trace(tmp_path):
    """同上：反馈必然比原文短，否则等于没做句级提取。"""
    row = _row(tmp_path)
    assert 0 < len(row.feedback_text) < len(row.trace)


# --- 3. score 必须与 feedback 解耦（防回归，当前应已绿）------------------
async def test_partial_success_keeps_a_high_score(tmp_path):
    """部分成功必须拿到「做成」的那份分，不许因为补了词就被打成 0。

    实测这条：有结果 0.4 + 有来源 0.3 + 覆盖查询词 0.3×⅓ = **0.8**
    （三个查询词里命中一个 —— 覆盖是按命中比例给的分）。
    本意的防线是「**不许 0**」：补 SOFT_FAILURE_MARKERS 若不小心并进
    deterministic_score 的短路，这里会直接塌到 0.0。
    """
    row = _row(tmp_path)
    assert row.score == pytest.approx(0.8), (
        f"部分成功的分数不对：{row.score}（期望 0.8；"
        f"若为 0.0 说明软失败词表被误接进了 deterministic_score）")
    assert row.score > 0.5


@pytest.mark.parametrize("marker", gepa.SOFT_FAILURE_MARKERS)
async def test_soft_markers_never_zero_the_score(marker):
    """每个软失败措辞单独出现，只要还有实质内容，就不许判 0。"""
    output = f"1. 关于 X 的更新：{marker}。\n2. X 的发布时间是 2026-09-21 https://x.com/a 来源：X"
    # 修之前这条会因进而 inspected：只要 score 不是 0 就说明没被硬词表短路
    assert gepa.deterministic_score("查 X 更新", output) > 0.0


# --- 4. 各种「说人话」的缺失措辞都要能捕获 -------------------------------
@pytest.mark.parametrize("wording", [
    "没查到", "查不到", "没找到", "未找到", "没有找到", "找不到",
    "无结果", "没有结果", "搜不到", "未检索到",
])
async def test_common_missing_wordings_are_captured(tmp_path, wording):
    """整理 LLM 的措辞是自由的，词表要覆盖高频说法（补词表而非调阈值）。"""
    body = f"1. 关于 X 在 2026-10-10 的更新：{wording}（来源：Tavily）。"
    row = _row(tmp_path, result_preview=body)
    assert row.feedback_text, f"措辞 {wording!r} 漏网"


# --- 5. 硬失败行为不许变（整段 + 判 0）-----------------------------------
async def test_hard_failure_still_carries_the_whole_text(tmp_path):
    """ok=False 是真失败 ⇒ 整段保留（此时没有「做成」的部分需要保护）。"""
    row = _row(tmp_path, ok=False, result_preview="不知道为什么没成，但这条也要留着")
    assert row.feedback_text == "不知道为什么没成，但这条也要留着"


async def test_hard_marker_still_zeros_the_score():
    """TOOL_FAILURE_MARKERS 的判 0 行为（P0-3）不受本次改动影响。"""
    assert gepa.deterministic_score(
        "查 X", "错误: 工具暂时不可用 https://x.com 来源 X") == 0.0


# --- 6. 成功调用仍然不硬凑反馈 -------------------------------------------
async def test_clean_success_still_has_no_feedback(tmp_path):
    row = _row(tmp_path, result_preview="1. X 于 2026-09-21 发布 https://x.com 来源：官网")
    assert row.feedback_text == ""


# --- 7. 词表分工必须显式可见（防止后人合并两套判据）----------------------
async def test_two_marker_sets_are_disjoint():
    """软/硬两套词表不许重叠，否则「为什么这条判 0」又说不清了。"""
    overlap = set(gepa.SOFT_FAILURE_MARKERS) & set(gepa.TOOL_FAILURE_MARKERS)
    assert not overlap, f"两套词表重叠了：{overlap}"
