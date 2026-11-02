"""规划器方向：必须「自己动手做」，不是「写手册教用户做」。

## 生产实证（2026-11-03 00:36，task=8db61ee470）

  owner：「根据这个文件https://.../SKILL.md，执行华为云获取积分的任务」
  它做的：browse_web 读 md → 写文件 → 规划出「整理手动操作指引给用户」
          → 全程**没打开过 developer.huaweicloud.com**
  结果：步骤 2 验收 FAIL：「输出内容过于简略，缺少具体操作指引」
        replan×2 后 status=failed

根因：规划器把「执行任务」理解成「写操作手册」，而重规划 prompt 第 3 条
明说「能力缺失就改为如实说明限制」—— 这句话成了第一次失败后的退路。
"""
from __future__ import annotations

import pathlib

PLANNER = (pathlib.Path(__file__).resolve().parent.parent
           / "anima" / "agent" / "planner.py")


def _src() -> str:
    return PLANNER.read_text(encoding="utf-8")


def test_plan_prompt_demands_real_action():
    """规划 prompt 必须明确反对「写手册」。"""
    src = _src()
    idx = src.find("_SYSTEM_PROMPT = (")
    end = src.find("_REPLAN_SYSTEM_PROMPT", idx)
    block = src[idx:end]
    assert "自己动手做" in block, "没有强调必须真做"
    assert "不是「写一份说明教用户做」" in block or "写一份说明" in block
    assert "整理手动操作步骤" in block, "缺少反例"


def test_plan_prompt_allows_explanation_only_when_impossible():
    src = _src()
    idx = src.find("_SYSTEM_PROMPT = (")
    end = src.find("_REPLAN_SYSTEM_PROMPT", idx)
    block = src[idx:end]
    assert "确实做不到" in block, "没有限定「说明原因」的适用条件"


def test_replan_tries_alternatives_first():
    """重规划必须「先换路子」，不能第一次失败就写说明。"""
    src = _src()
    idx = src.find("_REPLAN_SYSTEM_PROMPT")
    block = src[idx:idx + 1200]
    assert "先换路子" in block, "缺少「先换路子」的要求"
    assert "browse_web" in block, "没有给出具体替代工具"


def test_replan_requires_evidence_before_giving_up():
    """允许写说明，但必须附带「已尝试过哪些动作」。"""
    src = _src()
    idx = src.find("_REPLAN_SYSTEM_PROMPT")
    block = src[idx:idx + 1200]
    assert "已尝试过哪些具体动作" in block


def test_replan_keeps_production_counterexample():
    """反例要留在 prompt 里 —— 它是这条规则的由来。"""
    src = _src()
    idx = src.find("_REPLAN_SYSTEM_PROMPT")
    block = src[idx:idx + 1200]
    assert "2026-11-03" in block
    assert "没打开过目标页面" in block
