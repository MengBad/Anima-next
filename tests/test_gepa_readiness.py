# -*- coding: utf-8 -*-
"""🔴 GEPA 启动门槛必须是**代码常量**，不是报告里的口头约定（RED 先行）。

## 症状

`D_feedback 20 / D_pareto 10 / D_test 10` 这三个数字**在代码里完全不存在** ——
它们只活在 `reports/2026-10-10_skill-route-hint.md` §8.4 的表格里。

后果（2026-10-11 实测，我为此人肉跑了 5 个一次性脚本）：
- 无法测试、无法校验、无法被 CLI 直接读出；
- 「离门槛还差多少」每次都要重新算一遍，且**算完不留痕**；
- 换个技能就要重算一遍（阈值是全局的，但距离是 per-skill 的）。

⇒ 门槛是"不可观测"的。而**不可观测的门槛 = 没有门槛**：没人知道该等什么、
等到没有、以及要不要放弃。

## 本测试要钉住的
1. 三个阈值必须是 `anima.skills.gepa` 的**具名常量**（可被 import、可被断言）；
2. `gepa_trace` 的导出结果必须能直接给出「到门槛的距离」，
   且距离 = `max(0, 阈值 - 现有)`，**绝不出现负数**（够了就是够了）；
3. 「够/不够」必须是**显式布尔**，不能让调用方自己比大小
   （那正是这次漏掉的原因：每次都要人肉重算）。

⚠️ 只做**可观测化**，不降低门槛 —— 20/10/10 一个字都不许动。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from anima.skills import gepa  # noqa: E402


# --------------------------------------------------------------------------
# 1. 阈值必须是具名常量
# --------------------------------------------------------------------------

def test_thresholds_are_named_constants():
    """🔴 主回归：门槛必须有代码锚点，不能只活在报告里。"""
    for name in ("MIN_D_FEEDBACK", "MIN_D_PARETO", "MIN_D_TEST"):
        assert hasattr(gepa, name), f"缺少具名常量 {name}（门槛无代码锚点）"
        assert isinstance(getattr(gepa, name), int), f"{name} 必须是 int"


def test_thresholds_match_the_agreed_values():
    """数值必须与既定门槛逐字一致：**不许借这次改动偷偷降门槛**。"""
    assert gepa.MIN_D_FEEDBACK == 20
    assert gepa.MIN_D_PARETO == 10
    assert gepa.MIN_D_TEST == 10


# --------------------------------------------------------------------------
# 2. 距离必须可算、不为负
# --------------------------------------------------------------------------

def test_readiness_reports_distance_to_each_threshold():
    """给定条数，必须能直接拿到「还差几条」。"""
    from anima.skills.gepa_trace import readiness

    r = readiness(n_feedback=2, n_pareto=4)
    assert r["feedback"]["have"] == 2 and r["feedback"]["need"] == 20
    assert r["feedback"]["gap"] == 18
    assert r["pareto"]["gap"] == 6


def test_readiness_gap_never_negative():
    """够了就是够了 —— 距离不许出现负数（否则下游会拿它当"超额完成"）。"""
    from anima.skills.gepa_trace import readiness

    r = readiness(n_feedback=99, n_pareto=99)
    assert r["feedback"]["gap"] == 0
    assert r["pareto"]["gap"] == 0


def test_readiness_ready_is_explicit_boolean():
    """「够/不够」必须是显式布尔，不能让调用方自己比大小。"""
    from anima.skills.gepa_trace import readiness

    assert readiness(n_feedback=2, n_pareto=4)["ready"] is False
    assert readiness(n_feedback=20, n_pareto=10)["ready"] is True


def test_readiness_requires_both_datasets():
    """两个数据集都够才算 ready —— 单够一个不算（D_feedback 是历史瓶颈所在）。"""
    from anima.skills.gepa_trace import readiness

    assert readiness(n_feedback=20, n_pareto=9)["ready"] is False
    assert readiness(n_feedback=19, n_pareto=10)["ready"] is False


# --------------------------------------------------------------------------
# 3. 导出结果必须自带门槛距离（不必人肉重算）
# --------------------------------------------------------------------------

def test_trace_export_exports_carries_readiness():
    """`TraceExport.to_dict()` 必须直接带出门槛距离。

    这条是「下次不用再人肉跑 5 个脚本」的保证。
    """
    from anima.skills.gepa_trace import extract_skill_traces

    row = {
        "type": "tool_executed", "tool": "skill_web_brief",
        "request": "查 X 更新", "template": "整理要点",
        "result_preview": "没查到（来源：x.com）", "ok": True,
    }
    ex = extract_skill_traces([row], "web_brief")
    payload = ex.to_dict()

    assert "readiness" in payload, "导出结果不带门槛距离 ⇒ 仍要人肉重算"
    assert payload["readiness"]["feedback"]["gap"] >= 0
    assert payload["readiness"]["ready"] is False   # 1 条显然不够


def test_trace_export_cli_prints_readiness(capsys):
    """CLI 输出必须含门槛距离（否则运维看不见）。"""
    import json
    import tempfile
    from pathlib import Path as _P

    from anima.skills.gepa_trace import main

    with tempfile.TemporaryDirectory() as td:
        audit = _P(td) / "a.jsonl"
        audit.write_text(json.dumps({
            "type": "tool_executed", "tool": "skill_web_brief",
            "request": "查 X 更新", "template": "整理要点",
            "result_preview": "没查到（来源：x.com）", "ok": True,
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        out = _P(td) / "t.jsonl"
        code = main(["--audit", str(audit), "--out", str(out), "--skill", "web_brief"])
        assert code == 0
        printed = capsys.readouterr().out
        assert "gap" in printed, f"CLI 未打印门槛距离：{printed}"
