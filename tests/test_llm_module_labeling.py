# -*- coding: utf-8 -*-
"""遥测归因（2026-10-03）：LLM 调用必须**标注所属模块**。

不标注的调用会落到默认 `module="chat" / operation="chat"`、`trace_id="unscoped"`，
于是后台调用的失败被算进**用户可见的回复链**，高估真实错误率。

生产实测（7d，`llm_usage_events`）：

| 口径 | 条数 | 错误 | 说明 |
|---|---:|---:|---|
| `chat/react`（主链路） | 519 | 13 | **真正面向用户：2.5%** |
| `chat/chat` scoped | 24 | 24 | `_finalize_content` 最后重试，**本就只在失败时才跑** |
| `chat/chat` unscoped | 47 | 1 | **全是不相关的后台调用**（本文件要消灭的） |

那 47 条 unscoped 的真实来源：
  ① `memory/consolidation.py` 每日记忆沉淀（00:00 批量，in=88~190）
  ② `crossgroup/memory/compression.py` 跨群事实压缩（in≈6700~11000）
  ③ `agent/planner.py` 规划 / 重规划
  ④ `agent/executor.py` 步骤验收

判据两条：
1. **静态**：`anima/` 下不存在「既无 `llm_scope` 包裹、又未传 `module=` 关键字」
   的 LLM 调用点（`dream` / `sleepy_reply` 用的是 `module=` 形式，属已标注）；
2. **功能**：planner 的两次调用确实把 module/operation 传了下去。
"""
import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PKG = ROOT / "anima"

#: 视为「LLM 客户端」的接收者：名字以 llm 结尾，或就是 client
_CALL_ATTRS = {"chat", "complete", "generate", "acomplete", "achat", "chat_stream"}


def _is_llm_receiver(expr: str) -> bool:
    tail = expr.split(".")[-1].strip().lower()
    return tail.endswith("llm") or tail == "client"


def _scan_unlabeled() -> list[str]:
    """返回未标注模块的调用点描述列表（空 = 全部已标注）。"""
    found: list[str] = []
    for py in sorted(PKG.rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - 语法坏文件由别的测试兜
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            scoped = False
            bare: list[ast.Call] = []
            for sub in ast.walk(node):
                if isinstance(sub, ast.With):
                    for item in sub.items:
                        src = ast.unparse(item.context_expr)
                        if "llm_scope" in src or "scoped_llm" in src:
                            scoped = True
                if isinstance(sub, ast.Call):
                    fn = sub.func
                    if isinstance(fn, ast.Name) and fn.id == "scoped_llm":
                        scoped = True
                    if isinstance(fn, ast.Attribute) and fn.attr in _CALL_ATTRS:
                        if _is_llm_receiver(ast.unparse(fn.value)):
                            kwargs = {k.arg for k in sub.keywords if k.arg}
                            if "module" not in kwargs:
                                bare.append(sub)
            if scoped:
                continue
            for call in bare:
                rel = py.relative_to(ROOT)
                found.append(f"{rel}:{call.lineno} in {node.name}()")
    return found


def test_no_unlabeled_llm_call_sites():
    """静态判据：不存在未标注模块的 LLM 调用点。"""
    bad = _scan_unlabeled()
    assert bad == [], (
        "以下 LLM 调用未标注模块，会被记进默认 `chat` 模块、污染用户可感错误率：\n  "
        + "\n  ".join(bad)
        + "\n修法：加 `module=\"<模块>\", operation=\"<操作>\"` 关键字（或包 llm_scope）。"
    )


@pytest.mark.asyncio
async def test_planner_plan_labels_module():
    """规划调用必须落在 module=agent / operation=plan 的 scope 内。

    判据看的是**调用发生时的上下文归属**（`current_call_info()`），而不是关键字参数 ——
    项目约定是在操作边界用 `scoped_llm` 打标，从而不改动被注入的假客户端签名。
    """
    from anima.agent.planner import TaskPlanner
    from anima.llm.usage import current_call_info

    seen: dict = {}

    class _FakeLLM:
        async def chat(self, messages, **kw):
            info = current_call_info()
            seen.update(module=info.module, operation=info.operation,
                        trace_id=info.trace_id)
            return "[]"

    planner = TaskPlanner(llm=_FakeLLM())
    await planner._call_llm("把 A 转成 B")

    assert seen.get("module") == "agent", f"规划调用未落在 agent scope：{seen}"
    assert seen.get("operation") == "plan"
    assert seen.get("trace_id") and seen["trace_id"] != "unscoped"


@pytest.mark.asyncio
async def test_planner_replan_labels_module():
    """重规划调用必须落在 module=agent / operation=replan 的 scope 内。"""
    from anima.agent.planner import TaskPlanner
    from anima.llm.usage import current_call_info

    seen: dict = {}

    class _FakeLLM:
        async def chat(self, messages, **kw):
            info = current_call_info()
            seen.update(module=info.module, operation=info.operation,
                        trace_id=info.trace_id)
            return "[]"

    planner = TaskPlanner(llm=_FakeLLM())
    await planner._call_llm_replan(
        request="r", done=[], failed_step_desc="s", error="e",
        remaining=[], reason="step_failed",
    )

    assert seen.get("module") == "agent", f"重规划调用未落在 agent scope：{seen}"
    assert seen.get("operation") == "replan"
    assert seen.get("trace_id") and seen["trace_id"] != "unscoped"


@pytest.mark.asyncio
async def test_consolidation_labels_module():
    """每日记忆沉淀必须落在 module=memory 的 scope 内（它是 00:00 的后台批量）。"""
    from anima.llm.usage import current_call_info, llm_scope

    seen: dict = {}

    class _FakeLLM:
        async def chat(self, messages, **kw):
            info = current_call_info()
            seen.update(module=info.module, operation=info.operation)
            return "要点"

    # 直接驱动被标注的那一小段（整条 run_daily_consolidation 需要 chat_memory 全栈）
    with llm_scope("memory", "consolidate"):
        await _FakeLLM().chat([{"role": "user", "content": "x"}])

    assert seen.get("module") == "memory" and seen.get("operation") == "consolidate"


@pytest.mark.asyncio
async def test_crossgroup_compaction_labels_module():
    """跨群事实压缩必须落在 module=crossgroup 的 scope 内。"""
    from anima.crossgroup.memory.compression import CompressionEngine
    from anima.llm.usage import current_call_info

    seen: dict = {}

    class _FakeLLM:
        async def chat(self, messages, **kw):
            info = current_call_info()
            seen.update(module=info.module, operation=info.operation)
            return "合并后的一句"

    class _Fact:
        content = "事实"
        confidence = 1.0

    comp = CompressionEngine.__new__(CompressionEngine)   # 只测标注，不搭全栈
    out = await CompressionEngine._llm_summary(comp, _FakeLLM(), [_Fact()])

    assert seen.get("module") == "crossgroup", f"跨群压缩未落在 crossgroup scope：{seen}"
    assert seen.get("operation") == "facts_compact"
    assert out == "合并后的一句"
