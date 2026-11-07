"""完整 Memoh 化：多模式契约 + 执行代理可切换。

Memoh 结构（已逐字读过）：
  system_common.md  指令优先级 + 安全（网页内容当数据）
  mode_chat / mode_schedule / mode_subagent / mode_discuss
  _memory.md        记忆写入规则（稳定 id 防重复、标签要能区分）
  {{include:_name}} 在 init 时展开，各模式互不叠加。

本 bot 落地：_common / _memory / mode_work / mode_schedule。
陪伴模式不走这里（人格 prompt 是本 bot 特色，保留）。

关键一环：executor 之前硬编码「你是哈基米（猫娘）」，执行任务时仍在扮演，
12 次工具调用后输出「成长中心地址是 … 你快去看看吧～」，验收判「过于简略」。
现在按 task.session["prompt_mode"] 切换契约。
"""
from __future__ import annotations

import pathlib

from anima.prompts import available_modes, build_system, load_mode, mode_for

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROMPTS = ROOT / "anima" / "prompts"
EXECUTOR = ROOT / "anima" / "agent" / "executor.py"
HANDLER = ROOT / "anima" / "llm" / "handler.py"


def test_two_modes_available():
    m = available_modes()
    assert "work" in m and "schedule" in m


def test_all_prompt_files_present():
    for f in ("_common.md", "_memory.md", "mode_work.md", "mode_schedule.md"):
        p = PROMPTS / f
        assert p.exists(), f + " 缺失"
        assert len(p.read_text(encoding="utf-8")) > 300, f + " 太短"


def test_common_layer_has_priority_and_injection_guard():
    c = (PROMPTS / "_common.md").read_text(encoding="utf-8")
    assert "指令优先级" in c
    assert "当数据" in c and "不当指令" in c
    assert "先问" in c


def test_memory_rules_cover_dedup_and_tags():
    m = (PROMPTS / "_memory.md").read_text(encoding="utf-8")
    assert "不要重复写" in m
    assert "标签要能区分" in m
    assert "信息量" in m
    assert "sealed" in m


def test_schedule_mode_no_one_waiting():
    s = load_mode("schedule")
    assert "没人在等" in s or "没有人在等" in s
    assert "不要向主人提问" in s or "没人在线" in s
    assert "不要自行发明" in s or "不要发明" in s


def test_build_system_orders_common_before_contract():
    s = build_system("work", time_context="T")
    assert s.startswith("T")
    i_common = s.find("指令优先级")
    # "工作模式契约" 只存在于被剥掉的 HTML 开发者注释里；
    # 契约正文以 "## 你是谁" 起头（见 test_work_prompt_no_dev_notes.py）。
    i_contract = s.find("## 你是谁")
    assert i_common > 0 and i_contract > 0
    assert i_common < i_contract
    assert "<!--" not in s


def test_build_system_excludes_persona():
    s = build_system("work", time_context="t")
    body = s.split("-->")[-1]
    assert "本喵" not in body
    assert "猫娘" not in body


def test_mode_for_scheduled():
    assert mode_for("", False, scheduled=True) == "schedule"
    assert mode_for("group", True) == ""
    assert mode_for("private", True) == "work"


def test_executor_switches_by_prompt_mode():
    src = EXECUTOR.read_text(encoding="utf-8")
    assert "prompt_mode" in src
    assert "build_system" in src
    idx = src.find("_mode_contract")
    assert idx > 0
    window = src[idx:idx + 2200]
    assert "你是哈基米" in window
    assert "_sys_prompt" in window


def test_handler_passes_work_mode():
    src = HANDLER.read_text(encoding="utf-8")
    assert chr(34) + "prompt_mode" + chr(34) + ": " + chr(34) + "work" + chr(34) in src

def test_executor_reads_session_from_context():
    """session 在 task.context["session"]，不是 task.session。

    生产实证（2026-11-03 21:58 task=4728f69707）：
      首版读 task.session ⇒ 恒为 None ⇒ prompt_mode 取不到
      ⇒ 工作契约从未生效，模型仍在用「你是哈基米（猫娘）」执行任务
      ⇒ 任务里 session 字段为 null
    """
    src = EXECUTOR.read_text(encoding="utf-8")
    idx = src.find("_mode_contract")
    assert idx > 0
    block = src[idx:idx + 1200]
    assert 'getattr(task, "session"' not in block, \
        "不能直接读 task.session —— 该字段不存在"
    assert 'context' in block and 'get("session")' in block, \
        "应从 task.context[\"session\"] 读"


def test_executor_logs_mode_switch():
    """切换模式时要留日志，否则「没生效」和「没触发」分不清。"""
    src = EXECUTOR.read_text(encoding="utf-8")
    assert "[执行代理] system prompt 已切到模式契约" in src
