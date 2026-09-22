"""守卫：函数内局部 import 不得遮蔽「模块级已导入的同名符号」并在更早处使用。

背景（2026-09-22 P0 生产回归）：
`handler._handle_inner`（L695–1187）内 **L1017** 有一行局部
`from anima.llm.group_gate import is_direct_group_call`。
Python 在**编译期**据函数体内任一绑定，把该名字判为**整函数的局部名**
⇒ 模块级 L22 的导入被遮蔽 ⇒ 函数内更早的使用点
（L873 `addressed_to_bot=is_direct_group_call(event, text)`）抛
`UnboundLocalError: cannot access local variable 'is_direct_group_call'`。
该异常又被 `except` 吞掉 ⇒ `observe_group_message` 从未被调用
⇒ 群缓冲 `_group_recent` 完全不写 ⇒ 群上下文功能整体下线。

判据：对某名字，函数内存在「使用（Load）」早于**任何**该名字的局部 import ⇒ 真隐患。
⚠️ **不能**用「函数内最后一次局部 import 的行号」作比较 —— `prefilter._prefilter_inner`
每次使用前都紧邻 import（防御式写法，安全），那样比较会误报。
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ANIMA = REPO_ROOT / "anima"


def _imports_of(stmts) -> dict[str, int]:
    """从一组语句里收集 import 绑定的名字 → 行号。"""
    names: dict[str, int] = {}
    for stmt in stmts:
        if isinstance(stmt, ast.Import):
            for a in stmt.names:
                names[a.asname or a.name.split(".")[0]] = stmt.lineno
        elif isinstance(stmt, ast.ImportFrom):
            for a in stmt.names:
                if a.name == "*":
                    continue
                names[a.asname or a.name] = stmt.lineno
    return names


class _FnBody(ast.NodeVisitor):
    """收集某函数体（不含嵌套函数）内的局部 import 与名字使用。"""

    def __init__(self) -> None:
        self.imports: dict[str, list[int]] = {}
        self.loads: dict[str, list[int]] = {}

    def visit_FunctionDef(self, node):  # 嵌套函数：独立作用域，跳过
        pass

    def visit_AsyncFunctionDef(self, node):
        pass

    def _add(self, node):
        for n, ln in _imports_of([node]).items():
            self.imports.setdefault(n, []).append(ln)
        self.generic_visit(node)

    def visit_Import(self, node):
        self._add(node)

    def visit_ImportFrom(self, node):
        self._add(node)

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load):
            self.loads.setdefault(node.id, []).append(node.lineno)
        self.generic_visit(node)


def _scan(root: Path = ANIMA) -> list[str]:
    """返回所有「使用早于任何同名局部 import」的隐患描述。"""
    problems: list[str] = []
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        mod_names = _imports_of(tree.body)
        if not mod_names:
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            v = _FnBody()
            for stmt in fn.body:
                v.visit(stmt)
            for name in set(v.imports) & set(mod_names):
                imp_lines = v.imports[name]
                for ln in v.loads.get(name, []):
                    if not any(i < ln for i in imp_lines):
                        problems.append(
                            f"{path}:{ln} in {fn.name}() uses '{name}' before any "
                            f"local import (local imports at {sorted(imp_lines)})"
                        )
                        break
    return problems


def test_no_local_import_shadows_module_level_name():
    """全仓真隐患必须为 0。"""
    problems = _scan()
    assert not problems, (
        "函数内局部 import 遮蔽了模块级同名符号，且在该 import 之前使用 "
        "⇒ UnboundLocalError：\n" + "\n".join(problems)
    )


def test_scanner_detects_synthetic_hazard(tmp_path):
    """非平凡性证明：扫描器必须能报出人造的遮蔽隐患（防退化为永真）。"""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "mod.py").write_text(
        "from a.b import helper\n"           # 模块级导入
        "\n"
        "def f(x):\n"
        "    if x:\n"
        "        r = helper(1)\n"             # 使用早于局部 import ⇒ 隐患
        "    if x:\n"
        "        from a.b import helper\n"   # 局部 import
        "        helper(2)\n",
        encoding="utf-8",
    )
    problems = _scan(pkg)
    assert problems, "扫描器应当报出人造的遮蔽隐患"
    assert "helper" in problems[0]


def test_scanner_ignores_defensive_reimport(tmp_path):
    """防御式写法（每次使用前紧邻 import）不得误报 —— prefilter 真实模式。"""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "mod.py").write_text(
        "from a.b import helper\n"           # 模块级导入
        "\n"
        "def f(x):\n"
        "    if x:\n"
        "        from a.b import helper\n"   # import 紧跟使用
        "        helper(1)\n"
        "    if x:\n"
        "        from a.b import helper\n"
        "        helper(2)\n",
        encoding="utf-8",
    )
    assert _scan(pkg) == []


def test_handler_has_no_local_import_of_group_call():
    """回归钉：handler 的**函数体内**不得再对该符号做局部 import（模块级 L22 保留）。"""
    tree = ast.parse(
        (REPO_ROOT / "anima" / "llm" / "handler.py").read_text(encoding="utf-8")
    )
    offenders: list[tuple[str, list[int]]] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        v = _FnBody()
        for stmt in fn.body:
            v.visit(stmt)
        if "is_direct_group_call" in v.imports:
            offenders.append((fn.name, sorted(v.imports["is_direct_group_call"])))
    assert not offenders, (
        "handler.py 的函数体内不应再局部 import `is_direct_group_call`"
        f"（会被编译期判为局部名、遮蔽模块级 L22）；发现：{offenders}"
    )


def test_handle_inner_resolves_group_call_as_global():
    """精确复刻 CPython 的作用域判定（= bug 的机制本身）。

    若 `is_direct_group_call` 出现在 `_handle_inner.__code__.co_varnames`，
    说明它被判为**局部名** ⇒ L873 在赋值前使用即 UnboundLocalError；
    修复后它必须是 **GLOBAL**（出现在 co_names）。
    """
    from anima.llm.handler import LLMHandler

    code = LLMHandler._handle_inner.__code__
    assert "is_direct_group_call" not in code.co_varnames, (
        "`is_direct_group_call` 仍被 `_handle_inner` 当作局部名 ⇒ L873 会抛 UnboundLocalError"
    )
    assert "is_direct_group_call" in code.co_names, (
        "`is_direct_group_call` 未解析为全局名 ⇒ 模块级导入丢失"
    )
