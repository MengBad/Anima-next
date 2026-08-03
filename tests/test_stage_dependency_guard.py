"""M0.1 Commit 3c-1a —— Stage 依赖方向守护（AST based dependency guard）。

目标（用户拍板）：检查 Stage 是否绕过 Facade 直接获取 Core。
机制用 AST（非字符串匹配），覆盖三种 legacy 形态：
  1. ctx.meta["core"]
  2. ctx.meta.get("core")
  3. (ctx.meta or {}).get("core")
并硬禁：pipeline/stages 内任何 raw.require()（raw 债务不得扩散到 Stage）。

增量收紧策略：当前仅 guardrail.py 的 Telemetry 路径完成迁移（3c-1a）。
其余 ctx.meta core 访问尚在 PENDING_META_CORE_FILES 登记（债务清单），
每个后续 commit 收紧一处并删对应文件名；最终 Phase C 此集合清空、
且 guardrail 的 or_empty 形态消失。
允许 raw 的位置：contracts/access.py、adapter/、migration tests —— 不在本守护范围。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

STAGES_DIR = pathlib.Path(__file__).parent.parent / "anima" / "pipeline" / "stages"

# 3c-1a 债务登记：仍含 ctx.meta core 访问的 stage 文件。
# 每迁移一个文件删一个名；最终必须清空（Phase C）。
PENDING_META_CORE_FILES = {
    "generation.py",
    "cognition.py",
    "guardrail.py",
    "memory.py",
    "permission.py",
    "_shared.py",
}


def _is_meta_attr(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "meta"


def _is_literal_str(node: ast.AST, value: str) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value == value


def _unwrap_meta_or_empty(node: ast.AST) -> ast.AST:
    """(X.meta or {}) -> X.meta；否则原样返回。"""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Or):
        right = node.right
        if isinstance(right, ast.Dict) or (
            isinstance(right, ast.Constant) and right.value in ({}, None)
        ):
            return node.left
    return node


def _find_meta_core_accesses(tree: ast.AST) -> list[tuple[int, str]]:
    """返回 [(lineno, form)]，form ∈ {subscript, get, or_empty_get}。"""
    results: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript):
            base = _unwrap_meta_or_empty(node.value)
            if _is_meta_attr(base):
                sl = node.slice
                if isinstance(sl, ast.Constant) and isinstance(sl.value, str) and sl.value == "core":
                    results.append((node.lineno, "subscript"))
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "get":
                base = _unwrap_meta_or_empty(func.value)
                if _is_meta_attr(base) and node.args and _is_literal_str(node.args[0], "core"):
                    form = "or_empty_get" if isinstance(func.value, ast.BinOp) else "get"
                    results.append((node.lineno, form))
    return results


def _uses_ctx_core(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            v = node.func.value
            if isinstance(v, ast.Attribute) and v.attr == "core":
                if isinstance(v.value, ast.Name) and v.value.id == "ctx":
                    return True
    return False


def _is_raw_require(node: ast.AST) -> bool:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "require":
        v = node.func.value
        return (isinstance(v, ast.Name) and v.id == "raw") or (
            isinstance(v, ast.Attribute) and v.attr == "raw"
        )
    return False


def _all_stage_files():
    return sorted(STAGES_DIR.glob("*.py"))


def test_stages_no_raw_require_in_stages():
    """硬规则：pipeline/stages 禁止任何 raw.require()（raw 债务不得扩散到 Stage）。"""
    for f in _all_stage_files():
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if _is_raw_require(node):
                pytest.fail(f"{f.name}:{node.lineno}: raw.require() forbidden in stages")


def test_stages_meta_core_access_confined_to_pending():
    """ctx.meta core 访问必须被限制在已登记文件；禁止新出现或放任不登记。"""
    found: dict[str, list] = {}
    for f in _all_stage_files():
        acc = _find_meta_core_accesses(ast.parse(f.read_text(encoding="utf-8")))
        if acc:
            found[f.name] = acc

    for name, acc in found.items():
        assert name in PENDING_META_CORE_FILES, (
            f"{name}: 出现新的 ctx.meta core 访问 {acc}。"
            f"若已迁移请改用 ctx.core.<facade>()；否则在 PENDING_META_CORE_FILES 登记并注明 commit。"
        )
    for name in PENDING_META_CORE_FILES:
        assert name in found, (
            f"{name}: 登记为待迁移但已无 ctx.meta core 访问——该文件已迁移完，请从 PENDING 移除。"
        )


def test_guardrail_telemetry_uses_facade_not_meta():
    """3c-1a 证明点：guardrail 的 guard-fixation telemetry 不再经 meta['core']，且采用 ctx.core。"""
    f = STAGES_DIR / "guardrail.py"
    tree = ast.parse(f.read_text(encoding="utf-8"))
    acc = _find_meta_core_accesses(tree)
    or_empty = [ln for (ln, form) in acc if form == "or_empty_get"]
    assert not or_empty, f"guardrail.py 仍含 (ctx.meta or {{}}).get('core') @ {or_empty}（T1 未迁移）"
    assert _uses_ctx_core(tree), "guardrail.py 未使用 ctx.core.<facade>()（Facade 未被采用）"
