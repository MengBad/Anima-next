"""X5 回归：`hard_fact_recorded` 写了没人读（第三类静默失效）。

2026-10-14 跨函数 meta 键扫描发现：生产 cognition.py:710 写入
`ctx.meta["hard_fact_recorded"]`，但全仓（含 tests/）**零读取点**。

同段落的 `hard_facts_on` 有语义上的消费意图（记 always_on 事实条数），
说明作者确实想让状态可读 —— `hard_fact_recorded` 是被漏掉接线的那个。

处置：本测试钉住「删除死写入」这一零行为变更决策，并要求
`maybe_record_correction` 的副作用必须保留（那才是真功能）。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COG = ROOT / "anima" / "pipeline" / "stages" / "cognition.py"


def _text():
    return COG.read_text(encoding="utf-8")


def test_no_orphaned_result_written_to_dead_key():
    """精确判据：`_x = <调用>()` 的结果只被写进无人读取的 meta 键。

    🔴 2026-10-14 判据收窄记录：首版判据是「凡写入无人读取的 meta 键即报红」，
    实测会误伤 —— 全仓扫出 5 个，逐个核实后：

      mood_provided = True                      常量标记，无计算浪费
      inner_state_mode = _is_mode               有测试锚定（test_scan_s2_self_tell.py:70）
      hard_facts_on = len(...)                  作者留的可观测锚（无测试但无孤儿变量）
      system_dropped_keys = list(...)           同上
      inner_state_injected = any(...)           同上

    ⇒ 「没人读」不等于「该删」。真正的浪费形态只有一种：
    **算了一个结果，唯一用途是写进死键**（X5 的 `_fact`）。
    其余属可观测锚点，删不删由 owner 定，不由测试强制。
    """
    text = _text()
    lines = text.splitlines()

    # 全仓 anima/ 的 meta Load 键集合（AST，严格区分 Store/Load）
    load_keys = set()
    for p in (ROOT / "anima").rglob("*.py"):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"), str(p))
        except SyntaxError:
            continue

        class V(ast.NodeVisitor):
            def visit_Subscript(self, n):
                b = n.value
                if (isinstance(b, ast.Attribute) and b.attr == "meta"
                        and not isinstance(n.ctx, ast.Store)
                        and isinstance(n.slice, ast.Constant)
                        and isinstance(n.slice.value, str)):
                    load_keys.add(n.slice.value)
                self.generic_visit(n)

            def visit_Call(self, n):
                f = n.func
                if (isinstance(f, ast.Attribute) and f.attr == "get"
                        and isinstance(f.value, ast.Attribute) and f.value.attr == "meta"
                        and n.args and isinstance(n.args[0], ast.Constant)
                        and isinstance(n.args[0].value, str)):
                    load_keys.add(n.args[0].value)
                self.generic_visit(n)

        V().visit(tree)

    # 找「临时变量 = 调用」且该变量全文只出现 2 次（定义 + 死写入内使用）
    bad = []
    for i, ln in enumerate(lines):
        m = re.match(r'\s*(\_[A-Za-z]\w*)\s*=\s*.*\(', ln)
        if not m:
            continue
        var = m.group(1)
        occ = sum(1 for x in lines if re.search(r'\b%s\b' % re.escape(var), x))
        if occ != 2:
            continue  # 定义 + 唯一使用 ⇒ 疑似孤儿
        # 那唯一使用是否在「写入无人读取的 meta 键」里
        for j, x in enumerate(lines):
            if var in x and j != i:
                mm = re.search(r'ctx\.meta\[\s*"([^"]+)"\s*\]\s*=', x)
                if mm and mm.group(1) not in load_keys:
                    bad.append((i + 1, j + 1, var, mm.group(1)))

    assert not bad, (
        "算了结果却只写进无人读取的 meta 键（静默失效第三类）：%s" % bad
    )


def test_correction_side_effect_preserved():
    """maybe_record_correction 的调用必须保留 —— 记录纠错是真功能，不能顺手删。"""
    text = _text()
    assert "maybe_record_correction(" in text, (
        "maybe_record_correction 调用丢失 —— 纠错记录功能被误删"
    )


def test_fact_variable_not_orphaned():
    """`_fact` 不得在赋值后无人使用（那正是死写入的根源）。"""
    text = _text()
    seg = text[text.find("_fact = maybe_record_correction("):]
    seg = seg[:seg.find("_hf_store")]
    assert "_fact" not in seg.replace("_fact = maybe_record_correction(", "", 1), (
        "_fact 赋值后仍无消费 ⇒ 又造出一个死写入"
    )


def test_hard_facts_meta_keys_alive():
    """对照锚：有测试锚定的键不得被误删（inner_state_mode 即此类）。"""
    text = _text()
    assert 'ctx.meta["inner_state_mode"]' in text, (
        "inner_state_mode 写入丢失 —— 它被 tests/test_scan_s2_self_tell.py:70 锚定"
    )
