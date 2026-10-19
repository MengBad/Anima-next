# -*- coding: utf-8 -*-
"""D-37：Agent 步骤读取「无人产出」的文件（2026-10-20 修复）。

## 现场
Agent 任务 `36bcd32ab6`（10-18 11:07）：
```
ReAct 轮次 1: 1 个工具调用
anima.tools ERROR 工具 read_file 执行异常
FileNotFoundError: 文件不存在: output/script_part1.txt
工具调用完成 name=filesystem_read ok=False result_len=17
```
而该任务的 6 个步骤**没有任何一步调用过 write_file** —— 工作区里
（`data/agent_workspace/`）当时只有 1 个 09-26 的旧文件，`output/` 目录根本不存在。

## 根因（比"读错文件名"更靠前一层）
`Step.tool_name` 是**刻意留空**的（由 ReAct 在该步自行决定工具）⇒ 步骤描述
「先输出前 8 句」没说用哪个工具。模型在工作区里**看不到任何清单**，只能猜文件名
⇒ 猜错 ⇒ FileNotFoundError ⇒ 该步失败 ⇒ 触发 replan，而下一轮**仍然不知道该读什么**，会再猜一次。

## 修法（本轮）
1. `list_workspace_files()`：只读列出工作区现有文件（相对路径、按 mtime 倒序、限 40 个）。
2. `make_read_file_tool` 在「文件不存在」时**不再抛裸异常**，改为返回带清单的可操作回执。
   🔴 抛异常 ⇒ 步骤失败 ⇒ replan（无意义，模型还是会再猜）；回执 ⇒ 模型当场知道有什么、该怎么写。

## 刻意不做
- **不改规划器 / 不改 prompt**（U2 纪律：不靠塞例句解决问题）；
- **不改成功路径**（命中文件时返回值与旧实现逐字相同）；
- **不放宽安全**：路径穿越、超大文件仍然 `ValueError` 如实抛出，让上层判失败。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from anima.tools.builtin.file import (
    list_workspace_files,
    make_read_file_tool,
    read_file,
    write_file,
)


@pytest.fixture
def ws(tmp_path):
    return str(tmp_path)


def test_missing_file_returns_actionable_hint_instead_of_raising(ws):
    tool = make_read_file_tool(core=None)
    # 绑定不到 core 时用默认 "data"；这里直接传 core 桩以指向 tmp 工作区
    core = type("C", (), {"config": type("P", (), {"persistence": type("D", (), {"data_dir": ws})()})()})()
    tool = make_read_file_tool(core=core)
    out = tool("output/script_part1.txt")
    assert isinstance(out, str)
    assert "文件不存在" in out
    assert "当前工作区可用的文件有" in out
    # 空工作区要如实说明，不能编造文件
    assert "（工作区为空" in out


def test_hint_lists_existing_files(ws):
    write_file("notes.md", "hello", ws)
    core = type("C", (), {"config": type("P", (), {"persistence": type("D", (), {"data_dir": ws})()})()})()
    tool = make_read_file_tool(core=core)
    out = tool("nope.txt")
    assert "notes.md" in out, "回执里应列出工作区现有文件"


def test_success_path_unchanged(ws):
    """成功时返回值必须与 read_file 一致（本轮不改动成功路径）。

    注意：`write_file` 落在 ``<data_dir>/output/``，而 `read_file` 从
    ``<data_dir>`` 根读取 ⇒ 成功路径要用 **output/ok.txt** 这个真实存在的位置。
    （第一版我用 "ok.txt" 读到的是 data 根，自然 FileNotFound —— 测试的错，不是实现的错。）
    """
    path = write_file("ok.txt", "content-123", ws)
    rel = str(Path(path).resolve().relative_to(Path(ws).resolve()))
    core = type("C", (), {"config": type("P", (), {"persistence": type("D", (), {"data_dir": ws})()})()})()
    tool = make_read_file_tool(core=core)
    assert tool(rel) == read_file(rel, ws)
    assert tool(rel) == "content-123"


def test_security_errors_still_raise(ws):
    """路径穿越必须仍然抛错 —— 不能被"友好回执"吃掉。"""
    core = type("C", (), {"config": type("P", (), {"persistence": type("D", (), {"data_dir": ws})()})()})()
    tool = make_read_file_tool(core=core)
    with pytest.raises(ValueError):
        tool("../outside.txt")


def test_list_workspace_files_is_read_only_and_bounded(ws):
    for i in range(5):
        write_file(f"f{i}.txt", "x", ws)
    names = list_workspace_files(ws, limit=3)
    assert len(names) <= 3
    assert all(not n.startswith("/") for n in names), "只返回相对路径，不暴露绝对路径"
    # 工作区为空
    assert list_workspace_files(str(Path(ws) / "nonexistent")) == []


def test_production_case_wording():
    """生产原文要能被读懂：说清"没有文件"而不是只报错。"""
    src = open(
        Path(__file__).resolve().parents[1] / "anima" / "tools" / "builtin" / "file.py",
        encoding="utf-8",
    ).read()
    assert "当前工作区可用的文件有" in src
    assert "应改用写入工具先产出" in src
