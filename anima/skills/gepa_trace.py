"""GEPA 轨迹导出：从本地审计 JSONL 抽某个技能的工具调用轨迹。

**只做抽取，不做合成。** 审计里没有的东西就是没有——抽不到就写空文件并明确报错，
绝不编造 ``request`` / ``template`` / ``feedback_text``（No Hidden Authority / Audit Everything）。

一条轨迹**可用**，当且仅当同时恢复出：
- ``request``：本次技能调用的输入（``request``，或 ``args.request`` / ``args.query``）；
- ``template``：当时生效的 ``execution.template``。

缺任一即判为**不可用**（计入 ``unusable``），不写进产物。
``trace`` / ``feedback_text`` 可空——空的反馈行本来也进不了 ``D_feedback``。

字段来源（2026-10-09 起，见 ``anima/tools/__init__.py`` 的 ``tool_executed`` 写入点）：
- ``trace`` 取 ``result_preview``（截断后的结果原文）；**只有**旧事件没有
  ``result_preview`` 时才回看旧字段 ``result`` / ``output`` / ``trace``；
- ``score`` **不**从审计里读，也不另存——一律用
  ``gepa.deterministic_score(request, trace)`` **现算**（有结果 / 有来源 / 覆盖查询词）；
- ``ok=true`` 仍**不是**分数证据（它只是 ``normalize_tool_result`` 的结果，不等于任务做成）。

已知限制：21b64cf 之前的旧事件没有 ``template``，因此仍会判为不可用——
那是「抽不到」而不是「没有发生」，产物为空文件并报错，不伪造。

禁用路径：仓库 ``data/`` 与 ``/opt/anima`` 一律拒读（复用 ``gepa.assert_not_production_data``），
只接受脱敏导出或夹具。

禁用路径：仓库 ``data/`` 与 ``/opt/anima`` 一律拒读（复用 ``gepa.assert_not_production_data``），
只接受脱敏导出或夹具。

CLI：``python -m anima.skills.gepa_trace --audit A.jsonl --out T.jsonl --skill web_brief``
（抽不到 → 空文件 + 退出码 2）
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from anima.skills.gepa import GepaRow, assert_not_production_data, deterministic_score

__all__ = [
    "TRACE_FIELDS",
    "TraceExport",
    "TraceUnavailable",
    "load_audit_rows",
    "extract_skill_traces",
    "export_skill_traces",
    "main",
]

#: 导出产物每行固定这六个字段（与 ``GepaRow`` 一致）。
TRACE_FIELDS = ("id", "request", "template", "trace", "score", "feedback_text")

#: 普通工具调用的审计事件类型。
_TOOL_EVENT = "tool_executed"
#: request 的候选字段名（含嵌套 args）。
_REQUEST_KEYS = ("request", "query", "input", "args_request", "args_query")
_TEMPLATE_KEYS = ("template", "execution_template")
#: 结果原文：新事件用 result_preview（截断后的原文），旧事件才回看这些老字段。
_TRACE_KEYS = ("result_preview", "result", "output", "trace", "result_text")
_FEEDBACK_KEYS = ("feedback_text", "feedback", "error")
_ID_KEYS = ("id", "execution_id", "evidence_id", "trace_id")


class TraceUnavailable(RuntimeError):
    """抽不到真实轨迹（已写出空文件）。调用方必须显式处理，不得拿夹具顶替。"""


@dataclass
class TraceExport:
    """一次导出的结果（无论成败都给得出证据）。"""

    skill_id: str
    out_path: str = ""
    rows: list[GepaRow] = field(default_factory=list)
    scanned: int = 0        # 扫描到的合法审计行数
    matched: int = 0        # tool_executed 且 tool == skill_<id> 的条数
    unusable: list[dict[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.rows)

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "out_path": self.out_path,
            "rows": len(self.rows),
            "scanned": self.scanned,
            "matched": self.matched,
            "unusable": len(self.unusable),
            "unusable_sample": self.unusable[:5],
        }


def load_audit_rows(path: str | Path) -> list[dict[str, Any]]:
    """读审计 JSONL（坏行隔离）；路径先过生产数据禁令。"""
    resolved = assert_not_production_data(path)
    rows: list[dict[str, Any]] = []
    for line in resolved.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue  # 坏行隔离，与 AuditLogger 同款策略
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _pick(row: dict[str, Any], args: dict[str, Any], keys: Sequence[str]) -> str:
    """按候选字段名取值：先看顶层，再看嵌套 args。"""
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if key.startswith("args_") and args:
            nested = args.get(key[len("args_"):])
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    if args:
        for key in keys:
            nested = args.get(key)
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return ""


def extract_skill_traces(rows: Iterable[dict[str, Any]], skill_id: str) -> TraceExport:
    """从已解析的审计行里抽 ``skill_<skill_id>`` 的工具调用轨迹。"""
    tool_name = f"skill_{skill_id}"
    export = TraceExport(skill_id=skill_id)
    rows = list(rows)
    export.scanned = len(rows)

    for index, row in enumerate(rows):
        if row.get("type") != _TOOL_EVENT or row.get("tool") != tool_name:
            continue
        export.matched += 1
        args = row.get("args") if isinstance(row.get("args"), dict) else {}

        request = _pick(row, args, _REQUEST_KEYS)
        template = _pick(row, args, _TEMPLATE_KEYS)
        trace = _pick(row, args, _TRACE_KEYS)      # result_preview 优先，旧字段兜底

        rid = _pick(row, args, _ID_KEYS) or f"{skill_id}-{index}"
        if not request:
            export.unusable.append({"id": rid, "reason": "缺少 request（审计未记录入参）"})
            continue
        if not template:
            export.unusable.append({"id": rid, "reason": "缺少 template（审计未记录当时生效的模板）"})
            continue

        export.rows.append(GepaRow(
            id=rid,
            request=request,
            template=template,
            trace=trace,
            # 分数**现算**：不读事件里的 score，也不认 ok —— 只认任务做成没有。
            score=deterministic_score(request, trace),
            feedback_text=_pick(row, args, _FEEDBACK_KEYS),
        ))
    return export


def export_skill_traces(audit_path: str | Path, out_path: str | Path,
                        *, skill_id: str) -> TraceExport:
    """导出到 ``out_path``。抽不到就写空文件并抛 :class:`TraceUnavailable`。"""
    export = extract_skill_traces(load_audit_rows(audit_path), skill_id)
    target = Path(out_path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    if export.rows:
        payload = "\n".join(
            json.dumps({key: row.to_dict()[key] for key in TRACE_FIELDS}, ensure_ascii=False)
            for row in export.rows
        ) + "\n"
    else:
        payload = ""      # 空文件：抽不到就是抽不到，绝不拿夹具填
    target.write_text(payload, encoding="utf-8")
    export.out_path = str(target)
    if not export.rows:
        raise TraceUnavailable(
            f"审计 {audit_path} 中没有可恢复的 {skill_id} 轨迹"
            f"（扫描 {export.scanned} 行，命中 skill_{skill_id} {export.matched} 条，"
            f"均因缺少 request/template/score 判为不可用）。已写出空文件 {target}，禁止伪造。"
        )
    return export


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m anima.skills.gepa_trace",
        description="从本地审计 JSONL 导出技能轨迹（抽不到 → 空文件 + exit 2）",
    )
    parser.add_argument("--audit", required=True, help="审计 JSONL（脱敏导出或夹具，禁 data/ 与 /opt/anima）")
    parser.add_argument("--out", required=True, help="输出轨迹 JSONL")
    parser.add_argument("--skill", default="web_brief")
    args = parser.parse_args(argv)

    try:
        export = export_skill_traces(args.audit, args.out, skill_id=args.skill)
    except (ValueError, TraceUnavailable) as exc:
        print(json.dumps({"kind": "gepa_trace_export", "ok": False, "error": str(exc)},
                         ensure_ascii=False, indent=2))
        return 2
    print(json.dumps({"kind": "gepa_trace_export", **export.to_dict()},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
