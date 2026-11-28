#!/usr/bin/env python3
"""文档健康检查（2026-11-27）。

只做两件在仓库里已经出过问题的事：

1. 失效链接：docs/archive/ 迁移后，MAINTENANCE.md 里两处链接指向已移走的文件
   —— 这类断裂只能靠人肉发现，加进 CI 才算收口。
2. 文档时效：标了 [HISTORICAL] 却仍在活跃目录里的文档，会让读者误以为它是现状。
   这里只报告不失败，因为是否归档是人的判断。

退出码：仅「失效链接」返回 1；时效问题只打印告警。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 只查这些入口文档的链接（全仓扫描会把 archive 里的历史链接也挖出来，噪声太大）
LINK_CHECK_FILES = [
    "README.md",
    "MAINTENANCE.md",
    "CONTRIBUTING.md",
    "ENGINEERING_DEBT_LEDGER.md",
    "DEPLOYMENT_OPERATOR_GUIDE.md",
    "docs/VISION.md",
    "docs/ARCHITECTURE_CURRENT.md",
    "docs/PHASE_COORDINATES.md",
    "docs/ROADMAP.md",
]

#: 不应出现 [HISTORICAL] 的活跃目录
ACTIVE_DIRS = ("", "docs", "docs/engineering", "design")

#: 即使标了 [HISTORICAL] 也必须留在原位的文件。
#: AGENTS.md 是给 AI 编码代理的工作指引，工具按根目录路径找它，
#: 归档会导致代理读不到 —— 它的 [HISTORICAL] 指的是"内容可能滞后"，
#: 不是"可以去 archive"。
STALENESS_ALLOWLIST = {"AGENTS.md"}

_LINK_RE = re.compile(r"\]\(([^)\s]+\.md)\)")
_HIST_RE = re.compile(r"^>\s*\[HISTORICAL\]", re.M)


def check_links() -> list[str]:
    broken: list[str] = []
    for rel in LINK_CHECK_FILES:
        path = ROOT / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in _LINK_RE.finditer(text):
            target = m.group(1)
            if target.startswith(("http://", "https://", "#")):
                continue
            resolved = (path.parent / target).resolve()
            if not resolved.exists():
                broken.append(f"{rel} -> {target}")
    return broken


def check_staleness() -> list[str]:
    stale: list[str] = []
    for d in ACTIVE_DIRS:
        base = ROOT / d if d else ROOT
        if not base.is_dir():
            continue
        for md in sorted(base.glob("*.md")):
            if md.parent.name == "archive":
                continue
            head = md.read_text(encoding="utf-8", errors="replace")[:400]
            if _HIST_RE.search(head):
                rel = str(md.relative_to(ROOT))
                if rel not in STALENESS_ALLOWLIST:
                    stale.append(rel)
    return stale


def main() -> int:
    broken = check_links()
    stale = check_staleness()

    if broken:
        print(f"::error::发现 {len(broken)} 处失效链接")
        for b in broken:
            print("  BROKEN:", b)
    else:
        print(f"[OK] {len(LINK_CHECK_FILES)} 个入口文档链接全部有效")

    if stale:
        print(f"::warning::活跃目录里有 {len(stale)} 个文档标着 [HISTORICAL]（建议移入 docs/archive/）")
        for s in stale:
            print("  STALE:", s)
    else:
        print("[OK] 活跃目录无 [HISTORICAL] 残留")

    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
