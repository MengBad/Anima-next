"""D1 契约：工具返回值不得携带内部异常细节（类名 / str(exc) / 堆栈）。

背景（V3 北极星四体验 D1「P0·诚实降级」）：
> 任一工具失败可能把内部 stderr 语言直出用户。
> 方案：盘点 anima/tools 全部出口，统一「用户可读降级文案 + 日志/审计存原始错误」。

既有两道防线都**没盖住输入侧**：
- 输入侧：`anima/tools/__init__.py::tool_unavailable()`（2026-09-18 P0）——
  白名单只放行「行为性、可对外」的原因码，异常文本/堆栈**永不入内**。
  🔴 但实测只有 `x_search.py` 在用；其余工具**直接把 `type(e).__name__` + `str(e)`
  写进返回值** ⇒ 模型可见 ⇒ 可能复述进群。
- 输出侧：`guardrail._narrates_tool_failure()`（r28 P1-2）——依赖"超时/卡死"类**播报词**，
  而 `[agent] 请求失败: ValueError: ...` 不含播报词 ⇒ **漏网**。

判据：扫描 `anima/tools/**` 的**所有 `return` 语句**，其 f-string 插值不得包含
`str(<异常名>)` / `type(<异常名>).__name__` / 裸异常变量。
**logger 调用不在扫描范围**（日志本就该记原始错误）。

本文件自带自检（正例能抓 + 反例不报）——扫描器的「零命中」必须先过自检。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOLS_DIR = ROOT / "anima" / "tools"

#: 视作"异常对象"的常见变量名。`err` 一并纳入：仓库里它一律用来存异常消息。
EXC_NAMES = frozenset({"e", "exc", "exception", "err", "error"})


# ────────────────────────── 扫描器（含自检） ──────────────────────────

def _bad_inserts(stmt: ast.Return) -> list[str]:
    """返回该 `return` 的 f-string 里违规插值的源码文本。"""
    bad: list[str] = []
    for sub in ast.walk(stmt):
        if not isinstance(sub, ast.FormattedValue):
            continue
        try:
            src = ast.unparse(sub.value)
        except Exception:  # noqa: BLE001
            continue
        if src.startswith("str(") and any(
            n == src[4:].split(")")[0].strip() for n in EXC_NAMES
        ):
            bad.append(src)
        elif src.startswith("type(") and "__name__" in src:
            bad.append(src)
        elif src in EXC_NAMES:
            bad.append(src)
    return bad


def _scan_source(src: str) -> list[tuple[int, str]]:
    """扫一段源码 ⇒ [(行号, 违规插值)]。"""
    tree = ast.parse(src)
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and node.value is not None:
            for b in _bad_inserts(node):
                hits.append((getattr(node, "lineno", 0), b))
    return hits


def test_scanner_selfcheck_positive_and_negative():
    """🔴 自检：正例必须能抓、反例必须不报；否则零命中不可信。"""
    positive = [
        'def f(e):\n    return f"失败: {e}"\n',
        'def f(e):\n    return f"失败: {str(e)[:200]}"\n',
        'def f(e):\n    return f"失败: {type(e).__name__}: {str(e)}"\n',
        'def f(exc):\n    return f"出错: {exc}"\n',
    ]
    for src in positive:
        assert _scan_source(src), "正例漏抓：%r" % src

    negative = [
        # 日志里记原始错误 —— 本就该允许
        'import logging\nlogger = logging.getLogger(__name__)\ndef f(e):\n    logger.warning("失败: %s", e)\n    return "失败"\n',
        # 业务变量插值 —— 允许
        'def f(bv):\n    return f"读取失败（bvid={bv}）"\n',
        # 白名单降级串 —— 允许
        'from anima.tools import tool_unavailable\ndef f():\n    return tool_unavailable("X 浏览器未启用")\n',
    ]
    for src in negative:
        assert not _scan_source(src), "反例误伤：%r ⇒ %s" % (src, _scan_source(src))


# ────────────────────────── 全量扫描 anima/tools ──────────────────────────

def _iter_tool_files():
    for p in sorted(TOOLS_DIR.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        yield p


def test_tool_returns_carry_no_internal_exception_detail():
    """核心判据：工具返回值不得把内部异常细节带给模型。"""
    offenders: list[str] = []
    for p in _iter_tool_files():
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
            for lineno, frag in _scan_source(src):
                offenders.append("%s:%d  %s" % (p.relative_to(ROOT), lineno, frag))
        except SyntaxError as exc:
            offenders.append("%s  [SyntaxError] %s" % (p.relative_to(ROOT), exc))

    assert not offenders, (
        "以下工具把内部异常细节写进了返回值（模型可见 ⇒ 可能出站）：\n  "
        + "\n  ".join(offenders)
        + "\n\n修法：原始错误进 logger，返回值用 tool_unavailable(<白名单原因码>) "
          "或明确的行为性降级文案（不含类名 / str(exc) / 堆栈）。"
    )


def test_scanner_covers_all_tool_files():
    """元守卫：扫描面必须真覆盖（防止 rglob 路径写错导致「零命中」假象）。"""
    files = list(_iter_tool_files())
    assert len(files) >= 12, "只扫到 %d 个文件 ⇒ 路径可能错了" % len(files)
    names = {p.name for p in files}
    for must in ("x_search.py", "agent_tools.py", "web.py", "bilibili.py", "owner.py"):
        assert must in names, "扫描面缺 %s" % must


def test_tool_unavailable_is_fail_closed():
    """机制守卫：非白名单原因（含异常文本）必须退化为通用串。"""
    from anima.tools import _SAFE_TOOL_FAILURE, tool_unavailable

    assert tool_unavailable("ValueError: boom") == _SAFE_TOOL_FAILURE
    assert tool_unavailable("Traceback (most recent call last)") == _SAFE_TOOL_FAILURE
    assert tool_unavailable(None) == _SAFE_TOOL_FAILURE
    # 白名单原因可保留
    from anima.tools import TOOL_REASON_X_DISABLED

    assert TOOL_REASON_X_DISABLED in tool_unavailable(TOOL_REASON_X_DISABLED)
