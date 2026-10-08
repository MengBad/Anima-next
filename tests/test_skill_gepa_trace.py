"""GEPA 轨迹导出单元测试 —— 只用夹具，绝不读生产数据。

覆盖：
1. 夹具审计能抽出六字段轨迹，且能被 gepa.load_jsonl 回读；
2. 缺 request / template / score 一律判为不可用，**不伪造**；
3. 抽不到 → 空文件 + TraceUnavailable；
4. 只收目标技能，别的技能不混进来；
5. 坏行隔离；生产路径（data/、/opt/anima）拒读。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from anima.skills import gepa, gepa_trace


TEMPLATE = "把检索结果整理成 3-5 条要点，每条附来源。"


def _event(**over: object) -> dict:
    """一条「完整」的 skill_web_brief 工具调用审计事件。"""
    row = {
        "type": "tool_executed",
        "tool": "skill_web_brief",
        "request": "查 X 最新版本",
        "template": TEMPLATE,
        "result": "1. 标题 https://example.com/a",
        "score": 0.4,
        "feedback_text": "缺来源",
        "id": "e1",
    }
    row.update(over)
    return row


def _write(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 1. 夹具审计 → 六字段轨迹
# ---------------------------------------------------------------------------
def test_extracts_fixture_traces(tmp_path):
    audit = _write(tmp_path / "audit.jsonl", [
        _event(id="e1", score=0.4, feedback_text="缺来源"),
        _event(id="e2", score=0.8, feedback_text="", request="查 Y 价格"),
    ])
    export = gepa_trace.extract_skill_traces(gepa_trace.load_audit_rows(audit), "web_brief")

    assert export.ok and len(export.rows) == 2
    assert export.scanned == 2 and export.matched == 2 and export.unusable == []
    first = export.rows[0]
    assert (first.id, first.request, first.template) == ("e1", "查 X 最新版本", TEMPLATE)
    assert first.score == 0.4 and first.feedback_text == "缺来源"
    assert first.trace.startswith("1. 标题")


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
    # 只有带反馈的那条能进 D_feedback
    assert [r.id for r in gepa.feedback_ready(rows)] == ["e1"]


def test_nested_args_request_is_recovered(tmp_path):
    audit = _write(tmp_path / "audit.jsonl", [
        {"type": "tool_executed", "tool": "skill_web_brief", "args": {"query": "嵌套入参"},
         "template": TEMPLATE, "score": 0.5, "id": "e9"},
    ])
    export = gepa_trace.extract_skill_traces(gepa_trace.load_audit_rows(audit), "web_brief")
    assert len(export.rows) == 1 and export.rows[0].request == "嵌套入参"


# ---------------------------------------------------------------------------
# 2. 缺字段不伪造
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("missing", ["request", "template", "score"])
def test_missing_field_is_unusable_not_invented(tmp_path, missing):
    row = _event(id="e1")
    row.pop(missing)
    audit = _write(tmp_path / "audit.jsonl", [row])
    export = gepa_trace.extract_skill_traces(gepa_trace.load_audit_rows(audit), "web_brief")

    assert export.rows == [] and export.matched == 1
    assert len(export.unusable) == 1 and export.unusable[0]["id"] == "e1"


def test_boolean_ok_is_not_used_as_score(tmp_path):
    """审计里的 ok=True 不是分数证据 —— 不许拿它猜 score。"""
    audit = _write(tmp_path / "audit.jsonl", [
        {"type": "tool_executed", "tool": "skill_web_brief", "request": "查 X",
         "template": TEMPLATE, "ok": True, "result_len": 120},
    ])
    export = gepa_trace.extract_skill_traces(gepa_trace.load_audit_rows(audit), "web_brief")
    assert export.rows == []
    assert "score" in export.unusable[0]["reason"]


# ---------------------------------------------------------------------------
# 3. 抽不到 → 空文件 + 明确报错
# ---------------------------------------------------------------------------
def test_no_traces_writes_empty_file_and_raises(tmp_path):
    audit = _write(tmp_path / "audit.jsonl", [
        {"type": "tool_executed", "tool": "skill_web_brief", "ok": True, "result_len": 42},
    ])
    out = tmp_path / "traces.jsonl"
    with pytest.raises(gepa_trace.TraceUnavailable) as exc:
        gepa_trace.export_skill_traces(audit, out, skill_id="web_brief")

    assert out.exists() and out.read_text(encoding="utf-8") == ""   # 空文件，不是夹具
    assert "禁止伪造" in str(exc.value)


def test_empty_audit_also_raises(tmp_path):
    out = tmp_path / "traces.jsonl"
    with pytest.raises(gepa_trace.TraceUnavailable):
        gepa_trace.export_skill_traces(_write(tmp_path / "a.jsonl", []), out, skill_id="web_brief")
    assert out.read_text(encoding="utf-8") == ""


# ---------------------------------------------------------------------------
# 4. 只收目标技能 / 坏行隔离
# ---------------------------------------------------------------------------
def test_other_skills_and_event_types_are_ignored(tmp_path):
    audit = _write(tmp_path / "audit.jsonl", [
        _event(id="e1"),
        _event(id="e2", tool="skill_compose_notice"),
        _event(id="e3", tool="web_search"),
        {"type": "llm_call", "tool": "skill_web_brief", "request": "x",
         "template": TEMPLATE, "score": 1},
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


# ---------------------------------------------------------------------------
# 5. 生产路径拒读
# ---------------------------------------------------------------------------
def test_refuses_repository_data_dir():
    with pytest.raises(ValueError):
        gepa_trace.load_audit_rows(Path(gepa._repo_root()) / "data" / "audit" / "2026-10-09.jsonl")


def test_refuses_opt_anima():
    with pytest.raises(ValueError):
        gepa_trace.load_audit_rows("/opt/anima/data/audit/2026-10-09.jsonl")
