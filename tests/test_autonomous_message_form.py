# -*- coding: utf-8 -*-
"""自主消息的**形态**契约（2026-10-29）。

## 为什么需要这个文件

owner 原话：「你不能优化这些消息的逻辑和工作步骤，这些我都感觉不太好」。

前几轮一直在**调阈值**（23 秒→5 步/60 秒→改 created_at），
但每次改完还是「感觉不太好」。原因不是发多少条，而是**形态** ——
所有自主消息都是「系统在报工单」：

    本喵去干个活喵：<what>
    为什么：<why>                                       ← 字段名
    不用回…想叫停就说「停掉 5ed918804c」（或发 !anima …）  ← 内部标识 + 命令

    [1/3] 调用 read_group_history 读取群…               ← 步骤编号 + 工具名
    [重规划 #1] 换了个做法，改为 2 步                    ← 内部工程事件
    [完成] 任务已全部执行（但没有可展示的产出）           ← 状态标签

    [超时中止] …已经花了 90 秒、超过 60 秒的上限了…      ← 读秒表
    [资源超限] <异常串>                                 ← 异常原文

一条工单可以有无数种措辞，但**骨架**不变。所以本文件锁的是骨架：

1. 用户消息里**不得出现** `[xxx]` / `【xxx】` 状态标签
2. 用户消息里**不得出现** `字段名：` 形式（「为什么：」「任务：」「进度：」）
3. 用户消息里**不得出现** `!anima` 命令（叫停支持自然语言）
4. 用户消息里**不得出现** 裸 task_id（内部标识）
5. 步骤进度**不再逐步推送**（整体取消，改为超时发一句人话）

⚠️ 不是「不许有方括号」——引号内容里可以有。判据限定在
   「行首/句首的状态标签」与「_notify/gateway.send 的实际字面量」。
"""
from __future__ import annotations

import inspect
import re

import pytest

from anima.agent import executor as ex_mod
from anima.agent.executor import AgentExecutor


# ── 工具：抓出实际会发给用户的字面量 ─────────────────────────────────

def _user_literals(func) -> list[str]:
    """从函数源码里抽出 `_notify(task, "...")` / `_notify(task, f"...")` 的字面量。"""
    src = inspect.getsource(func)
    out = re.findall(r'_notify\(\s*task\s*,\s*f?"([^"]*)"', src)
    out += re.findall(r'_notify\(\s*task\s*,\s*msg\)', src)     # 变量形式另行核对
    return out


_STATUS_TAG = re.compile(r"^\s*[\[【][^】\]]{1,12}[\]】]")


def _has_status_tag(text: str) -> bool:
    """行首是否以 `[xxx]` / `【xxx】` 状态标签开头。"""
    return any(_STATUS_TAG.match(line) for line in str(text).splitlines())


# ── 1. execute() 的所有用户消息 ─────────────────────────────────────

def test_execute_messages_have_no_status_tags():
    """`execute()` 里没有任何一条消息以 `[完成]` `[失败]` `[超时中止]` 开头。"""
    bad = [m for m in _user_literals(AgentExecutor.execute) if _has_status_tag(m)]
    assert not bad, f"仍有状态标签当用户消息：{bad}"


def test_execute_messages_have_no_field_names():
    """不得出现 `字段名：` 形式（`任务：` / `进度：` / `内容：` / `原因：`）。"""
    _FIELD = re.compile(r"(任务|内容|进度|原因|需求|看详情|重跑|批准安装|为什么)[：:]")
    bad = [m for m in _user_literals(AgentExecutor.execute) if _FIELD.search(m)]
    assert not bad, f"仍有日志字段名混进用户消息：{bad}"


def test_execute_messages_have_no_commands():
    """不得出现 `!anima` 命令（自然语言入口已覆盖叫停/继续）。"""
    bad = [m for m in _user_literals(AgentExecutor.execute) if "!anima" in m]
    assert not bad, f"用户消息里不该教命令：{bad}"


# ── 2. 步骤进度整体取消 ─────────────────────────────────────────────

def test_step_notify_is_disabled():
    """`_should_notify_steps` 恒 False —— 步骤进度不再推送。

    这是**设计决定**，不是阈值调高：
        旧 [1/3] 调用 read_group_history 读取群…   ← 内部计划，用户看不了也帮不上
        新 （静默，只在超 180s 时发一句「还在弄喵」）
    """
    ex = AgentExecutor.__new__(AgentExecutor)
    class _T:
        steps = [object()] * 20
        created_at = 0
    ex._exec_started_at = 0
    assert ex._should_notify_steps(_T()) is False
    import time
    ex._exec_started_at = time.time() - 3600
    assert ex._should_notify_steps(_T()) is False


def test_replan_is_not_pushed():
    """重规划**不再发消息**（只进日志/审计）。"""
    src = inspect.getsource(AgentExecutor._replan_or_give_up)
    # 只看**代码行**：注释里引用旧文案作历史说明是允许的
    code = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
    bad = [ln.strip() for ln in code if "[重规划 #" in ln]
    assert not bad, f"重规划说明仍在代码里：{bad}"
    assert "不推送" in src, "应明确记录「不推送」的决定"


def test_long_silence_nudge_exists():
    """有「跑太久发一句人话」的替代机制。"""
    assert hasattr(AgentExecutor, "_maybe_nudge_silence")
    ex = AgentExecutor.__new__(AgentExecutor)
    ex.LONG_SILENCE_SEC = 180.0
    ex.LONG_SILENCE_MAX_HINTS = 1
    ex._exec_started_at = 0
    # 没记录执行起点 ⇒ 不发（fail-closed）
    import asyncio
    asyncio.run(ex._maybe_nudge_silence(type("T", (), {"creator": 1, "id": "x"})()))


# ── 3. 报备是一句话 ─────────────────────────────────────────────────

def test_handoff_note_is_one_line():
    """报备必须是**一行**，且不含 task_id / 命令 / 字段名。"""
    from anima.core.autonomy import Heartbeat

    hb = Heartbeat.__new__(Heartbeat)
    note = hb._compose_handoff_note(
        what="给你找张最搞笑的图", why="时间到了", task_id="5ed918804c")

    assert "\n" not in note, f"报备应是单行，实得：{note!r}"
    assert "5ed918804c" not in note, "不该露 task_id"
    assert "!anima" not in note, "不该教命令"
    assert "为什么" not in note, "不该用字段名"
    assert "给你找张最搞笑的图" in note, "必须说清在做什么"


@pytest.mark.parametrize("why", ["时间到了", "到点了", "到期了", ""])
def test_handoff_note_drops_meaningless_reason(why):
    """「时间到了」这种无信息量理由不该塞进括号 —— 报备本身体现了「现在做」。"""
    from anima.core.autonomy import Heartbeat

    hb = Heartbeat.__new__(Heartbeat)
    note = hb._compose_handoff_note(what="整理报告", why=why)
    assert "（" not in note and "（时间到了" not in note


def test_handoff_note_keeps_meaningful_reason():
    """有信息量的理由要保留（用户不知道「本喵在追的目标 X」）。"""
    from anima.core.autonomy import Heartbeat

    hb = Heartbeat.__new__(Heartbeat)
    note = hb._compose_handoff_note(what="整理报告", why="本喵在追的目标「把群聊整理好」")
    assert "把群聊整理好" in note


# ── 4. 全仓扫描：不许再出现旧文案 ───────────────────────────────────

@pytest.mark.parametrize("forbidden,where", [
    ("[重规划 #", "agent/executor.py"),
    ("[已取消] 主人终止了任务", "agent/executor.py"),
    ("[资源超限]", "agent/executor.py"),
    ("[超时中止]", "agent/executor.py"),
])
def test_old_message_forms_removed(forbidden, where):
    """旧形态的字面量不得再出现在**代码行**里（注释引用历史不算）。"""
    import pathlib
    src = pathlib.Path("anima", where).read_text(encoding="utf-8")
    code_lines = [ln for ln in src.splitlines()
                  if not ln.lstrip().startswith("#")]
    hits = [ln.strip() for ln in code_lines if forbidden in ln]
    assert not hits, f"旧文案仍在代码里：{forbidden!r} -> {hits[:3]}"
