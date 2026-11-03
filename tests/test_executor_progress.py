"""执行代理的主动沟通：开工播报 / 步骤汇报 / 卡住时询问。

## 生产实证（2026-11-03 21:58 task=4728f69707）

  主人发华为云任务 → 收到「这就去办」→ 然后**再无任何消息**。
  2 分钟后任务 failed，主人是后来查日志才知道的。

  任务记录里写着：
    「需要用户登录状态（Cookie/Token）才能获取积分数据」
  ——它**准确识别出缺什么**，却既没问主人、也没中途说一句，直接判失败。

## 三个根因

  1. ask_user 工具读 core._active_agent_task，而**从没有地方写它**
     ⇒ 「主动询问」这条路根本不存在
  2. 只有 _maybe_nudge_silence（跑太久）才吭声，4 步 2 分钟的短任务全程静默
  3. 缺条件时直接 replan→failed，没有「先问主人」的分支

## 修法

  · execute() 把 task 挂到 core._active_agent_task
  · _announce_start：开工说「几步、先做什么」
  · _report_step：失败必报，成功隔步报
  · _maybe_ask_for_blocker + _needs_human：命中「登录/密码/验证码/Cookie」
    等只有主人能给的东西时，主动挂起询问，不判失败
"""
from __future__ import annotations

import pathlib

EXECUTOR = (pathlib.Path(__file__).resolve().parent.parent
            / "anima" / "agent" / "executor.py")


def _src() -> str:
    return EXECUTOR.read_text(encoding="utf-8")


def test_task_attached_to_core():
    """ask_user 依赖 core._active_agent_task，必须有人写它。"""
    src = _src()
    assert "_core._active_agent_task = task" in src
    idx = src.find("_core._active_agent_task = task")
    window = src[max(0, idx - 600):idx]
    assert "task.status" in window or "executing" in window


def test_announce_start_exists():
    src = _src()
    assert "async def _announce_start" in src
    assert "await self._announce_start(task)" in src


def test_report_step_exists_and_wired():
    src = _src()
    assert "async def _report_step" in src
    assert src.count("await self._report_step(") >= 2, "至少成功/失败两处要调"


def test_report_failure_always():
    """失败必报 —— 这是主人最需要知道的。"""
    src = _src()
    idx = src.find("async def _report_step")
    block = src[idx:idx + 1600]
    assert "if not ok:" in block
    assert "没成" in block


def test_ask_for_blocker_exists():
    src = _src()
    assert "async def _maybe_ask_for_blocker" in src
    assert "make_ask_user_tool" in src
    assert "await self._maybe_ask_for_blocker(" in src


def test_needs_human_pattern():
    """只有主人能给的东西才触发询问，不能为小事打断。"""
    src = _src()
    idx = src.find("_NEED_HUMAN_RE = re.compile(")
    assert idx > 0
    block = src[idx:idx + 700]
    for kw in ("登录", "验证码", "Cookie", "Token", "授权"):
        assert kw in block, "缺少 " + kw


def test_blocker_does_not_mark_failed():
    """问完之后不能同时判 failed —— 任务是「等回答」，不是「失败」。"""
    src = _src()
    idx = src.find("await self._maybe_ask_for_blocker(")
    assert idx > 0
    window = src[idx:idx + 400]
    assert "failed = False" in window
    assert "break" in window