"""A 方案：工作模式（任务驱动 vs 对话驱动的差距）。

## 为什么加这个

实测（近 3 天）：私聊 15 条，只有 5 次进 agent.planner（1/3）。
其余 2/3 是「模型看着 69 个工具，选择用文字回答」。

定位差异：
  Memoh  = 任务驱动，说一句就进 agent 循环，没有「只聊天」出口
  本 bot = 对话驱动，先判是不是聊天；且每轮注入人格/情绪/欲望/群氛围
            tokens 常到 15.5k/16k，大量预算花在「怎么扮演猫娘」

A 方案：任务类指令 → 工作模式 → 不注入情绪/欲望/关系/伤痕，保留说话风格。
"""
from __future__ import annotations

import pathlib
import re

COGNITION = (pathlib.Path(__file__).resolve().parent.parent
             / "anima" / "pipeline" / "stages" / "cognition.py")


def _src() -> str:
    return COGNITION.read_text(encoding="utf-8")


def _work_mode_block() -> str:
    """截取工作模式判定块。"""
    src = _src()
    idx = src.find("work_mode")
    assert idx > 0, "找不到工作模式代码"
    start = src.rfind("    # 2026-11-02 A 方案", 0, idx)
    return src[start if start > 0 else idx:idx + 1800]


def test_work_mode_clears_emotional_context():
    """工作模式必须清空欲望/关系/伤痕注入。"""
    block = _work_mode_block()
    for key in ("desire_ctx", "relation_ctx", "scar_ctx"):
        assert re.search(rf'{key}\s*=\s*""', block), f"{key} 没被清空"


def test_work_mode_uses_owner_list():
    """权限判据必须与 handler 一致（owner 名单），不能用不存在的 resolve_level。

    只看**代码行**，跳过注释 —— 注释里说明「不用 resolve_level」，不该被判成引用。
    """
    block = _work_mode_block()
    code_lines = [ln for ln in block.splitlines()
                  if ln.strip() and not ln.strip().startswith("#")]
    code = "\n".join(code_lines)
    assert "resolve_level" not in code, "代码里引用了不存在的函数"
    assert "owners" in code, "没有用 owner 名单判定"


def test_work_mode_sets_meta_flag():
    """要留下 ctx.meta 标记，供下游阶段与日志追溯。"""
    assert 'ctx.meta["work_mode"] = True' in _src()


def test_work_mode_only_in_task_sentences():
    """只有任务类句子才进工作模式，闲聊不受影响。"""
    block = _work_mode_block()
    assert "is_agent_task" in block


def test_work_mode_is_fail_soft():
    """判定失败不能拖垮管线 —— 必须 try/except。"""
    block = _work_mode_block()
    assert "except Exception" in block


def test_still_injects_time_context():
    """工作模式仍保留时间上下文 —— 它不影响专注。"""
    src = _src()
    idx = src.find("assemble_persona_system(")
    assert idx > 0
    window = src[idx:idx + 400]
    assert "time_context=" in window
