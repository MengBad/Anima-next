# -*- coding: utf-8 -*-
"""工作/定时模式的 system prompt 不得夹带开发者笔记（2026-11-07）。

## 生产实证

owner 原话：「现在这个 agent 基于 memoh 的项目」—— 这个感受是准确的，
不是错觉。`mode_work.md` / `_common.md` / `_memory.md` 等文件开头有大段
HTML 注释，写着「照抄 Memoh `mode_chat.md` 结构」「Memoh 原文要点：…」。
注释里以为「HTML 注释不会进 system prompt」，但 `prompts._read()` 以前是
直接 `read_text().strip()` **原样返回** ⇒ 这些笔记逐字进了 system。

实测修复前 `build_system("work")`：
  含 'Memoh': True，含 '照抄': True，总 3593 字
  开头就是「<!-- 通用层：指令优先级 + 安全边界（照抄 Memoh …） -->」

后果（14:29~14:36 华为云任务）：模型自称 Memoh、回话带 `[Memoh] ` 前缀、
用「执行代理」腔混搭猫娘口癖。U2 定律：写进 prompt 的原句会被原样搬进出站。

## 修法

`_read()` 用正则剥掉 `<!-- … -->`。开发者笔记留在源码里给人看，
但不进 LLM 上下文。陪伴模式不受影响（它不读这些文件）。
"""
from __future__ import annotations

import pytest

from anima.prompts import build_system, load_mode


@pytest.mark.parametrize("mode", ["work", "schedule"])
def test_system_has_no_dev_notes(mode):
    s = build_system(mode)
    assert s, f"{mode} 契约不该为空"
    assert "<!--" not in s
    assert "-->" not in s
    assert "Memoh" not in s, "开发者笔记里的 Memoh 不能进 prompt"
    assert "照抄" not in s


def test_work_starts_with_real_contract():
    s = build_system("work")
    assert s.lstrip().startswith("## 指令优先级")


def test_work_contract_still_has_core_rules():
    """剥注释不能误删真正的契约正文。"""
    s = build_system("work")
    assert "工具输出" in s and "当数据" in s
    assert "必须真做" in s
    assert "汇报格式" in s or "做完了" in s


def test_load_mode_strips_comments():
    m = load_mode("work")
    assert "<!--" not in m
    assert "## 你是谁" in m


def test_work_prompt_is_smaller_than_before():
    """修复前 3593 字；剥掉笔记后应显著变短（也省 token）。"""
    s = build_system("work")
    assert len(s) < 2600, f"仍夹带笔记？字数={len(s)}"
