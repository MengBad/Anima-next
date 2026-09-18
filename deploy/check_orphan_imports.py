#!/usr/bin/env python3
"""孤儿导入扫描器（只读 · 纯静态 · 零副作用）。

为什么需要它
------------
`anima/capability/runtime_evidence_integration.py`（生产存在、仓库无）在第 49 行
`from anima.capability_runtime_observation import ...` —— 该模块在生产**不存在**：

    $ cd /opt/anima && ./venv/bin/python -c "import anima.capability.runtime_evidence_integration"
    ModuleNotFoundError: No module named 'anima.capability_runtime_observation'

项目里有 12 个这类「生产独有 · 零引用 · 内部 import 已悬空」的废弃模块。它们平时
不可达，但只要出现任何**遍历式加载**（`pkgutil.walk_packages`、`pytest` 扫目录、
`from anima.x import *`、工具化的 AST/反射扫描）就会当场 ImportError 打断整个进程。

本脚本用 **ast 静态解析**而不是逐个 import —— 后者会执行模块顶层代码（可能改配置、
建目录、连网），那就不再是只读操作了。静态法同样能给出确定的清单，且可以安全地
在生产上运行。

用法
----
    python deploy/check_orphan_imports.py                 # 扫仓库自身
    python deploy/check_orphan_imports.py --root /opt/anima   # 扫生产（只读）
    python deploy/check_orphan_imports.py --json          # 机读输出（CI 用）

退出码
------
    0  无**活跃**代码的悬空引用（仅归档目录里有 ⇒ 已知残留）
    1  存在活跃代码的悬空引用，或扫描本身失败
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import defaultdict
from pathlib import Path

# 这些目录下的引用视为「归档残留」：不阻断退出码，但与活跃引用分开统计。
_ARCHIVE_MARKERS = ("/_archive/", "\\_archive\\", "/archive/", "\\archive\\", "/deprecated/")


def _is_archived(rel: str) -> bool:
    p = rel.replace("\\", "/")
    return any(m.replace("\\", "/") in p for m in _ARCHIVE_MARKERS) or p.startswith("_archive/")


def _module_exists(root: Path, dotted: str) -> bool:
    """`anima.a.b` → 存在 `anima/a/b.py` 或 `anima/a/b/__init__.py`？"""
    parts = dotted.split(".")
    base = root.joinpath(*parts)
    return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()


def _scan_file(path: Path, root: Path) -> tuple[list[tuple[int, str]], str | None]:
    """返回 (悬空内部导入 [(行号, 缺失模块名)], 解析失败原因 | None)。

    相对导入按所在包解析。读文件用 ``utf-8-sig``：Python 的 tokenizer 接受 BOM，
    但 ``ast.parse(str)`` 不行 —— 用 utf-8 读会把 BOM 变成 U+FEFF 并误报语法错。
    """
    try:
        src = path.read_text(encoding="utf-8-sig", errors="replace")
        tree = ast.parse(src)
    except SyntaxError as exc:
        return [], f"line {exc.lineno or 0}: {exc.msg}"
    except Exception as exc:  # noqa: BLE001 - 读不了就跳过，不因单文件失败中断全扫描
        return [], f"{type(exc).__name__}: {exc}"

    rel = path.relative_to(root).with_suffix("")
    pkg_parts = list(rel.parts[:-1])          # 所在包的 dotted 部分
    missing: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:  # 相对导入
                base = pkg_parts[: len(pkg_parts) - (node.level - 1)] if node.level > 1 else pkg_parts
                dotted = ".".join([*base, *(node.module.split(".") if node.module else [])])
            else:
                dotted = node.module or ""
            if dotted.startswith("anima.") and not _module_exists(root, dotted):
                missing.append((node.lineno, dotted))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("anima.") and not _module_exists(root, alias.name):
                    missing.append((node.lineno, alias.name))
    return missing, None


def scan(root: Path) -> dict:
    pkg = root / "anima"
    if not pkg.is_dir():
        raise SystemExit(f"找不到包目录: {pkg}（用 --root 指定项目根）")

    by_module: dict[str, list[tuple[str, int]]] = defaultdict(list)
    parse_failures: list[tuple[str, str]] = []
    files = sorted(p for p in pkg.rglob("*.py") if "__pycache__" not in p.parts)
    for f in files:
        missing, err = _scan_file(f, root)
        rel = str(f.relative_to(root)).replace("\\", "/")  # 统一为正斜杠，跨平台可比
        if err:
            parse_failures.append((rel, err))
        for lineno, mod in missing:
            by_module[mod].append((rel, lineno))

    active, archived = {}, {}
    for mod, refs in by_module.items():
        (archived if all(_is_archived(r[0]) for r in refs) else active)[mod] = refs

    return {
        "root": str(root),
        "files_scanned": len(files),
        "active": active,
        "archived": archived,
        "parse_failures": parse_failures,
        "active_ref_count": sum(len(v) for v in active.values()),
        "archived_ref_count": sum(len(v) for v in archived.values()),
    }


def scan_tests(root: Path) -> list[str]:
    """返回**无法被 pytest 收集**的测试文件（其内部 import 的 ``anima.*`` 模块不存在）。

    生产实测（2026-09-18）：`pytest tests/` 在**收集阶段**就 12 个 ERROR 并
    `Interrupted` —— 生产跑不了全量测试。排除这些文件后 `3915 collected, 0 errors`。
    本函数就是产出那份 `--ignore=` 清单的机器（见 ``--pytest-ignore``）。
    """
    tdir = root / "tests"
    if not tdir.is_dir():
        return []
    broken: list[str] = []
    for f in sorted(tdir.rglob("*.py")):
        if "__pycache__" in f.parts or "_archive" in f.parts:
            continue  # 归档目录不是收集目标（与 norecursedirs 约定一致）
        missing, _err = _scan_file(f, root)
        if missing:
            broken.append(str(f.relative_to(tdir)).replace("\\", "/"))
    return broken


def _default_root() -> str:
    """默认扫描根：脚本所在目录的上一级；从 stdin 执行（``python -``）时退回 CWD。"""
    try:
        return str(Path(__file__).resolve().parents[1])
    except NameError:  # 无 __file__（stdin / exec）
        return str(Path.cwd())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="孤儿导入扫描器（只读）")
    ap.add_argument("--root", default=_default_root(),
                    help="项目根（含 anima/ 包的那个目录）")
    ap.add_argument("--json", action="store_true", help="机读输出")
    ap.add_argument("--pytest-ignore", action="store_true",
                    help="只输出可直接粘贴给 pytest 的 --ignore= 参数（覆盖无法收集的测试文件）")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()

    if args.pytest_ignore:
        broken = scan_tests(root)
        if not broken:
            print("# tests/ 下所有文件均可被 pytest 收集（无需 --ignore）")
            return 0
        print(" ".join(f"--ignore=tests/{name}" for name in broken))
        return 0

    report = scan(root)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report["active"] else 0

    print(f"扫描根目录: {report['root']}")
    print(f"已扫文件:   {report['files_scanned']}")
    print("-" * 72)
    if report["active"]:
        print(f"🔴 活跃代码的悬空内部导入 {report['active_ref_count']} 处"
              f"（{len(report['active'])} 个缺失模块）—— 遍历式加载会当场 ImportError：")
        for mod, refs in sorted(report["active"].items()):
            print(f"   ✗ {mod}")
            for rel, lineno in refs[:6]:
                print(f"       {rel}:{lineno}")
            if len(refs) > 6:
                print(f"       …另有 {len(refs) - 6} 处")
    else:
        print("✅ 活跃代码无悬空内部导入")

    if report["archived"]:
        print()
        print(f"ℹ️  归档目录内的悬空引用 {report['archived_ref_count']} 处"
              f"（{len(report['archived'])} 个缺失模块）—— 已知残留，不阻断：")
        for mod, refs in sorted(report["archived"].items()):
            print(f"   · {mod}  ← {len(refs)} 处（如 {refs[0][0]}:{refs[0][1]}）")

    if report["parse_failures"]:
        print()
        print(f"⚠️  {len(report['parse_failures'])} 个文件无法静态解析"
              f"（不影响退出码，但该文件的引用未被检查）：")
        for rel, err in report["parse_failures"][:10]:
            print(f"   · {rel} — {err}")

    broken_tests = scan_tests(Path(report["root"]))
    if broken_tests:
        print()
        print(f"🔴 {len(broken_tests)} 个测试文件无法被 pytest **收集**"
              f"（它们 import 的 anima.* 模块不存在 ⇒ 收集期即 ERROR）：")
        for name in broken_tests[:12]:
            print(f"   · tests/{name}")
        if len(broken_tests) > 12:
            print(f"   …另有 {len(broken_tests) - 12} 个")
        print("   → 缓解（不改任何文件）：用本工具 `--pytest-ignore` 输出的参数跑 pytest")

    print("-" * 72)
    return 1 if report["active"] else 0


if __name__ == "__main__":
    sys.exit(main())
