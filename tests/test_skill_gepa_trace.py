"""GEPA 轨迹导出单元测试 —— 只用夹具，绝不读生产数据。

针对 21b64cf 之后的 ``tool_executed`` 事件（带 request / template / result_preview）：

1. 新事件能导出：trace == result_preview，score == deterministic_score 现算；
2. 事件自带的 score 被忽略；
3. 缺 request 或 template → 不可用；全部不可用 → 空文件 + TraceUnavailable；
4. ok=true 且没有 request 时不导出（ok 不是分数）；
5. 只收目标技能；坏行隔离；data/ 与 /opt/anima 拒读。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from anima.skills import gepa, gepa_trace


TEMPLATE = "把检索结果整理成 3-5 条要点，每条附来源。"
PREVIEW = "1. 标题 https://example.com/a 来源：example.com"


def _event(**over: object) -> dict:
    """一条 21b64cf 之后的 skill_web_brief 审计事件。"""
    row = {
        "type": "tool_executed",
        "tool": "skill_web_brief",
        "request": "查 X 最新版本",
        "template": TEMPLATE,
        "result_preview": PREVIEW,
        "ok": True,
        "result_len": len(PREVIEW),
        "id": "e1",
    }
    row.update(over)
    return row


def _write(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 1. 新事件 → trace 取 result_preview，score 现算
# ---------------------------------------------------------------------------
def test_new_event_exports_trace_from_result_preview(tmp_path):
    audit = _write(tmp_path / "audit.jsonl", [_event()])
    export = gepa_trace.extract_skill_traces(gepa_trace.load_audit_rows(audit), "web_brief")

    assert export.ok and len(export.rows) == 1
    row = export.rows[0]
    assert row.trace == PREVIEW
    assert row.template == TEMPLATE
    assert row.request == "查 X 最新版本"
    # score 是算出来的，不是抄来的
    assert row.score == gepa.deterministic_score("查 X 最新版本", PREVIEW)
    assert row.score > 0.0


def test_score_matches_deterministic_score_exactly(tmp_path):
    """有结果 + 有来源 + 覆盖查询词 → 满分；拿掉链接 → 掉 0.3。"""
    with_src = "关于「查 X 最新版本」的结果 https://example.com/a"
    no_src = "关于「查 X 最新版本」的结果，没有链接"
    audit = _write(tmp_path / "audit.jsonl", [
        _event(id="a", result_preview=with_src),
        _event(id="b", result_preview=no_src),
    ])
    rows = gepa_trace.extract_skill_traces(gepa_trace.load_audit_rows(audit), "web_brief").rows

    assert rows[0].score == gepa.deterministic_score("查 X 最新版本", with_src)
    assert rows[1].score == gepa.deterministic_score("查 X 最新版本", no_src)
    assert rows[0].score > rows[1].score


def test_old_event_falls_back_to_legacy_result_field(tmp_path):
    """旧事件没有 result_preview 时才回看 result / output / trace。"""
    legacy = _event(id="old")
    legacy.pop("result_preview")
    legacy["result"] = "旧字段原文 https://example.com/a"
    rows = gepa_trace.extract_skill_traces(
        gepa_trace.load_audit_rows(_write(tmp_path / "a.jsonl", [legacy])), "web_brief").rows
    assert rows[0].trace == "旧字段原文 https://example.com/a"


def test_result_preview_wins_over_legacy_fields(tmp_path):
    row = _event(id="both", result="旧原文", result_preview="新预览")
    rows = gepa_trace.extract_skill_traces(
        gepa_trace.load_audit_rows(_write(tmp_path / "a.jsonl", [row])), "web_brief").rows
    assert rows[0].trace == "新预览"


# ---------------------------------------------------------------------------
# 2. 事件自带的 score 被忽略
# ---------------------------------------------------------------------------
def test_event_score_field_is_ignored(tmp_path):
    audit = _write(tmp_path / "audit.jsonl", [_event(id="e1", score=0.99)])
    row = gepa_trace.extract_skill_traces(
        gepa_trace.load_audit_rows(audit), "web_brief").rows[0]
    assert row.score == gepa.deterministic_score("查 X 最新版本", PREVIEW)
    assert row.score != 0.99


# ---------------------------------------------------------------------------
# 3. 缺 request / template → 不可用；全不可用 → 空文件 + 报错
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("missing", ["request", "template"])
def test_missing_field_is_unusable_not_invented(tmp_path, missing):
    row = _event(id="e1")
    row.pop(missing)
    export = gepa_trace.extract_skill_traces(
        gepa_trace.load_audit_rows(_write(tmp_path / "a.jsonl", [row])), "web_brief")
    assert export.rows == [] and export.matched == 1
    assert len(export.unusable) == 1 and export.unusable[0]["id"] == "e1"


def test_legacy_event_without_template_still_unusable(tmp_path):
    """21b64cf 之前的旧事件：有 result 但没 template → 仍不可用，产物空文件。"""
    legacy = _event(id="old")
    legacy.pop("template")
    legacy["result"] = "原文"
    out = tmp_path / "traces.jsonl"
    with pytest.raises(gepa_trace.TraceUnavailable):
        gepa_trace.export_skill_traces(_write(tmp_path / "a.jsonl", [legacy]), out,
                                       skill_id="web_brief")
    assert out.read_text(encoding="utf-8") == ""


def test_no_traces_writes_empty_file_and_raises(tmp_path):
    """旧形状事件（只有 tool/ok/result_len）→ 空文件 + 明确报错。"""
    out = tmp_path / "traces.jsonl"
    with pytest.raises(gepa_trace.TraceUnavailable) as exc:
        gepa_trace.export_skill_traces(_write(tmp_path / "a.jsonl", [
            {"type": "tool_executed", "tool": "skill_web_brief", "ok": True, "result_len": 42},
        ]), out, skill_id="web_brief")

    assert out.exists() and out.read_text(encoding="utf-8") == ""   # 空文件，不是夹具
    assert "禁止伪造" in str(exc.value)


def test_empty_audit_also_raises(tmp_path):
    out = tmp_path / "traces.jsonl"
    with pytest.raises(gepa_trace.TraceUnavailable):
        gepa_trace.export_skill_traces(_write(tmp_path / "a.jsonl", []), out, skill_id="web_brief")
    assert out.read_text(encoding="utf-8") == ""


# ---------------------------------------------------------------------------
# 4. ok=true 不是分数，没有 request 就不导出
# ---------------------------------------------------------------------------
def test_ok_true_without_request_is_not_exported(tmp_path):
    audit = _write(tmp_path / "audit.jsonl", [
        {"type": "tool_executed", "tool": "skill_web_brief", "ok": True,
         "result_len": 42, "template": TEMPLATE, "result_preview": PREVIEW},
    ])
    export = gepa_trace.extract_skill_traces(gepa_trace.load_audit_rows(audit), "web_brief")
    assert export.rows == []
    assert "request" in export.unusable[0]["reason"]


def test_ok_true_is_never_used_as_the_score(tmp_path):
    """ok 只表示结果是否成型，分数一律由 deterministic_score 决定（不是 1.0）。"""
    audit = _write(tmp_path / "audit.jsonl", [_event(id="e1", ok=True)])
    row = gepa_trace.extract_skill_traces(gepa_trace.load_audit_rows(audit), "web_brief").rows[0]
    assert row.score == gepa.deterministic_score("查 X 最新版本", PREVIEW)
    assert 0.0 < row.score <= 1.0


# ---------------------------------------------------------------------------
# 4b. feedback 通道：工具错误 / 空结果 / 护栏拒绝 → 进 feedback_text
# ---------------------------------------------------------------------------
SOFT_FAIL = "没有搜到「查 X 最新版本」的相关结果（通道状态：bing=empty）。"
GUARD_FAIL = "错误: 你的权限不足，无法调用工具 'web_search'"


def test_soft_failure_text_becomes_feedback(tmp_path):
    """软失败：工具没抛异常，返回原文就是失败 → 那段原文即反馈。"""
    audit = _write(tmp_path / "a.jsonl", [_event(id="e1", result_preview=SOFT_FAIL)])
    row = gepa_trace.extract_skill_traces(
        gepa_trace.load_audit_rows(audit), "web_brief").rows[0]
    assert row.feedback_text == SOFT_FAIL
    assert row.trace == SOFT_FAIL


def test_guardrail_refusal_becomes_feedback(tmp_path):
    audit = _write(tmp_path / "a.jsonl", [_event(id="e1", result_preview=GUARD_FAIL)])
    row = gepa_trace.extract_skill_traces(
        gepa_trace.load_audit_rows(audit), "web_brief").rows[0]
    assert row.feedback_text == GUARD_FAIL


def test_ok_false_becomes_feedback_even_without_marker(tmp_path):
    audit = _write(tmp_path / "a.jsonl", [
        _event(id="e1", ok=False, result_preview="不知道为什么没成"),
    ])
    row = gepa_trace.extract_skill_traces(
        gepa_trace.load_audit_rows(audit), "web_brief").rows[0]
    assert row.feedback_text == "不知道为什么没成"


def test_successful_call_has_no_feedback(tmp_path):
    """成功调用不硬凑反馈——没有反馈的行本来就不进 D_feedback。"""
    audit = _write(tmp_path / "a.jsonl", [_event(id="e1", ok=True)])
    row = gepa_trace.extract_skill_traces(
        gepa_trace.load_audit_rows(audit), "web_brief").rows[0]
    assert row.feedback_text == ""


def test_explicit_feedback_field_wins(tmp_path):
    """Owner 纠正等显式反馈优先，不被引擎推断的失败原文覆盖。"""
    audit = _write(tmp_path / "a.jsonl", [
        _event(id="e1", result_preview=SOFT_FAIL, feedback_text="Owner：别用这个词"),
    ])
    row = gepa_trace.extract_skill_traces(
        gepa_trace.load_audit_rows(audit), "web_brief").rows[0]
    assert row.feedback_text == "Owner：别用这个词"


def test_failed_rows_can_form_d_feedback(tmp_path):
    """凑得出 D_feedback：失败行带反馈，成功行不带。"""
    audit = _write(tmp_path / "a.jsonl", [
        _event(id="f0", result_preview=SOFT_FAIL),
        _event(id="f1", result_preview=GUARD_FAIL),
        _event(id="f2", ok=True),
    ])
    rows = gepa_trace.extract_skill_traces(
        gepa_trace.load_audit_rows(audit), "web_brief").rows
    assert [r.id for r in gepa.feedback_ready(rows)] == ["f0", "f1"]


# ---------------------------------------------------------------------------
# 5. 六字段产物 / 只收目标技能 / 坏行隔离 / 拒读生产路径
# ---------------------------------------------------------------------------
def test_exported_file_is_readable_by_gepa(tmp_path):
    audit = _write(tmp_path / "audit.jsonl", [
        _event(id="e1", feedback_text="缺来源"),
        _event(id="e2", feedback_text=""),
    ])
    out = tmp_path / "traces.jsonl"
    gepa_trace.export_skill_traces(audit, out, skill_id="web_brief")

    rows = gepa.load_jsonl(out)
    assert [r.id for r in rows] == ["e1", "e2"]
    assert set(json.loads(out.read_text(encoding="utf-8").splitlines()[0])) == set(
        gepa_trace.TRACE_FIELDS)
    assert [r.id for r in gepa.feedback_ready(rows)] == ["e1"]


def test_nested_args_request_is_recovered(tmp_path):
    audit = _write(tmp_path / "audit.jsonl", [
        {"type": "tool_executed", "tool": "skill_web_brief", "args": {"query": "嵌套入参"},
         "template": TEMPLATE, "result_preview": PREVIEW, "id": "e9"},
    ])
    rows = gepa_trace.extract_skill_traces(
        gepa_trace.load_audit_rows(audit), "web_brief").rows
    assert len(rows) == 1 and rows[0].request == "嵌套入参"


def test_other_skills_and_event_types_are_ignored(tmp_path):
    audit = _write(tmp_path / "audit.jsonl", [
        _event(id="e1"),
        _event(id="e2", tool="skill_compose_notice"),
        _event(id="e3", tool="web_search"),
        {"type": "llm_call", "tool": "skill_web_brief", "request": "x",
         "template": TEMPLATE, "result_preview": PREVIEW},
    ])
    export = gepa_trace.extract_skill_traces(gepa_trace.load_audit_rows(audit), "web_brief")
    assert [r.id for r in export.rows] == ["e1"]
    assert export.matched == 1


def test_broken_lines_are_isolated(tmp_path):
    (tmp_path / "audit.jsonl").write_text(
        "{not json}\n" + json.dumps(_event(id="e1"), ensure_ascii=False) + "\n[]\n",
        encoding="utf-8")
    export = gepa_trace.extract_skill_traces(
        gepa_trace.load_audit_rows(tmp_path / "audit.jsonl"), "web_brief")
    assert [r.id for r in export.rows] == ["e1"]


def test_refuses_repository_data_dir():
    with pytest.raises(ValueError):
        gepa_trace.load_audit_rows(Path(gepa._repo_root()) / "data" / "audit" / "2026-10-09.jsonl")


def test_refuses_opt_anima():
    with pytest.raises(ValueError):
        gepa_trace.load_audit_rows("/opt/anima/data/audit/2026-10-09.jsonl")
