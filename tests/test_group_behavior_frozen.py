"""群聊行为冻结基线（项目执行书 §3.2 保证 3）。

目的：给「私聊 Memoh 化」这套改造上锁。

owner 的硬约束是**群聊行为逐字不变**。但私聊与群聊共用 handler.py 同一条
流水线入口，任何在流水线层的改动都可能同时影响两边。本文件把群聊路径的
**可观测决策点**固化成快照——一旦有人（包括我自己）改到群聊分支，这里立刻红。

覆盖的是「决策」而不是「文案」：模型输出本就不确定，锁文案没有意义；
真正危险的是**分支走向变了**（该拦的不拦了 / 该回的变成草稿流了）。
"""
from __future__ import annotations

import ast
import pathlib

ANIMA = pathlib.Path(__file__).resolve().parent.parent / "anima"
HANDLER = ANIMA / "llm" / "handler.py"


def _handler_source() -> str:
    return HANDLER.read_text(encoding="utf-8")


def test_agent_submit_still_private_only():
    """多步 Agent 任务仍只在私聊自动开跑；群聊只建草稿。

    2026-10-21 的判据：群里出现「已创建 Agent 草稿」本身就是破绽。
    """
    src = _handler_source()
    idx = src.find("self.core.agent.submit(")
    assert idx > 0, "找不到 agent.submit 调用点"
    window = src[max(0, idx - 900):idx]
    assert 'detail_type == "private"' in window, (
        "agent.submit 不再受 detail_type == private 保护 —— 群聊可能自动开跑任务"
    )


def test_approval_dialog_private_only():
    """自然语言审批（危险工具/授权/补丁）只在私聊消费。

    只看**模块导入行**（`from anima.agent import ...`），不看函数体内的
    局部 import（如授权申请里的 GrantQueue）——后者本来就在别的位置。
    """
    src = _handler_source()
    for fn in ("approval_dialog", "patch_dialog"):
        idx = src.find(f"from anima.agent import {fn}")
        assert idx > 0, f"找不到 {fn} 的导入点"
        window = src[max(0, idx - 700):idx]
        assert 'detail_type == "private"' in window, (
            f"{fn} 不再受私聊保护 —— 群里可能出现审批问答"
        )


def test_skill_creation_private_only():
    """「造能力」路由只在私聊；群聊里说「做个工具」仍走原管线。"""
    src = _handler_source()
    idx = src.find("is_skill_creation_request")
    assert idx > 0
    window = src[max(0, idx - 500):idx]
    assert 'detail_type == "private"' in window, (
        "is_skill_creation_request 不再受私聊保护"
    )


def test_group_branch_unchanged_markers():
    """群聊仍然走防抖 + 同群串行锁 + group 分支准备三件套。"""
    src = _handler_source()
    assert "_group_debouncer" in src
    assert "_group_turn_lock" in src
    assert 'detail_type == "group"' in src


def test_no_private_tool_leaks_into_group_schema():
    """新增的私聊专属工具必须标 private_only。

    静态检查：任何名字带 private_ 前缀的工具注册，必须同时出现
    private_only=True，否则会在群聊 schema 里暴露。
    """
    builtin = ANIMA / "tools" / "builtin" / "__init__.py"
    tree = ast.parse(builtin.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = getattr(node.func, "attr", "") or getattr(node.func, "id", "")
        if func != "Tool":
            continue
        name = ""
        if node.args and isinstance(node.args[0], ast.Constant):
            name = str(node.args[0].value)
        is_private = any(
            kw.arg == "private_only"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value is True
            for kw in node.keywords
        )
        if name.startswith("private_") and not is_private:
            offenders.append(name)
    assert not offenders, (
        f"这些工具名带 private_ 前缀但没标 private_only=True，会在群聊暴露: {offenders}"
    )
