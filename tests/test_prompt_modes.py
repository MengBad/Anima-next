"""模式化 system prompt —— 照抄 Memoh 的 mode_* 契约做法。

## 为什么改成模式替换而不是叠补丁

Memoh 把 system prompt 拆成互不叠加的模式契约（system_common / mode_chat /
mode_discuss / mode_schedule / mode_subagent），运行时按 sessionType 选一份。

本 bot 此前把「必须真做」当 fragment 往人格 prompt 里加，后果（生产实证）：
  2026-11-02  dropped=ask_user_rule —— 规则第一次用就被 15.5k/16k 预算裁掉
  2026-11-03  模型看完 69 个工具仍选择「用文字回答」
而人格 prompt 光扮演要求就有数千字，会把任务约束稀释掉。

于是照 Memoh：工作模式整体替换 system prompt，与陪伴模式互不叠加。
"""
from __future__ import annotations

import pathlib

from anima.prompts import (
    available_modes,
    build_work_system,
    load_mode,
    mode_for,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent
COGNITION = ROOT / "anima" / "pipeline" / "stages" / "cognition.py"
CONTRACT = ROOT / "anima" / "prompts" / "mode_work.md"


def test_work_mode_available():
    assert "work" in available_modes()


def test_contract_file_exists_and_nonempty():
    assert CONTRACT.exists(), "mode_work.md 不存在"
    assert len(CONTRACT.read_text(encoding="utf-8")) > 500


def test_contract_demands_real_action():
    c = load_mode("work")
    assert "必须真做" in c
    assert "不要写" in c and "手册" in c
    assert "试过哪些具体动作" in c, "缺少「必须说明已尝试什么」的要求"


def test_contract_has_instruction_priority():
    """照抄 Memoh：明确指令优先级 + 网页内容当数据。"""
    c = load_mode("work")
    assert "指令优先级" in c
    assert "当作数据" in c, "没有防 prompt 注入的说明"


def test_work_system_replaces_persona():
    """工作模式必须整体替换 —— 人格文本不能残留。"""
    s = build_work_system("PERSONA_PLACEHOLDER", time_context="T")
    assert "PERSONA_PLACEHOLDER" not in s, "人格 prompt 被叠加进工作模式了"
    assert "工作模式契约" in s
    assert s.startswith("T"), "时间上下文应在最前"


def test_work_system_is_compact():
    """契约要小 —— 这是它不被预算裁掉的前提。"""
    s = build_work_system("x", time_context="t")
    assert len(s) < 3000, f"契约 {len(s)} 字太长，会挤占任务上下文"


def test_group_never_gets_work_mode():
    """群里永远是陪伴模式 —— 工作契约会暴露内部机制。"""
    assert mode_for("group", True) == ""
    assert mode_for("private", True) == "work"
    assert mode_for("private", False) == ""


def test_missing_contract_falls_back():
    """契约缺失时返回空串，调用方回退人格 prompt（不能崩）。"""
    assert load_mode("no_such_mode") == ""


def test_cognition_swaps_prompt():
    """cognition 必须在工作模式下整体替换。"""
    src = COGNITION.read_text(encoding="utf-8")
    assert "build_work_system" in src
    assert 'ctx.meta["prompt_mode"] = "work"' in src
    idx = src.find("build_work_system")
    window = src[idx:idx + 500]
    assert "ctx.system_prompt = _work_sys" in window, "没有真正替换 system_prompt"


def test_packaging_includes_md():
    """pyproject 必须声明 package-data，否则部署后读不到 .md。"""
    pp = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "anima.prompts" in pp, "prompts 的 .md 没被打包"
