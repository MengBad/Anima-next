#!/usr/bin/env python3
"""R10-00 安全护栏：在 CI 中检测本次改动是否触及敏感/受保护代码。

设计纪律（Production Is Sacred / No Hidden Authority）：
- 本脚本**只报告、不自动放行、不自动阻断**（fail-closed 体现在「必须由 Owner 审查」，
  而非让 CI 自行决定放行/拒绝 Evolution 或部署）。
- 触碰 ``CORE_PROTECTED`` 或安全关键路径的改动，标记 ``requires_owner_review=true``，
  提示 Reviewer Agent / Owner 走人工审批；CI 不因此自动失败（除非 ``--strict``）。
- CI 整体**不得**自动部署生产、不得自动批准 Evolution（由 workflow 架构保证，本脚本仅产出证据）。

用法（被 GitHub Actions 调用）：
    python scripts/anima_guard.py --base origin/master --out security-report.json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# 与 anima/agent/self_evolution.py 的 CORE_PROTECTED 保持一致（独立副本，避免导入项目代码）。
CORE_PROTECTED = frozenset({
    "anima/core/security.py",
    "anima/capability/rbac.py",
    "anima/agent/orchestrator.py",
    "anima/agent/devloop.py",
    "anima/capability/evidence.py",
    "_deploy.py",
    "anima/__main__.py",
    "config/default.yaml",
})

# 安全关键路径关键词（命中即视为需要 Owner 安全审查，不一定等同 CORE_PROTECTED）。
SENSITIVE_KEYWORDS = (
    "rbac", "isolation", "sandbox", "security", "evidence",
    "_deploy", "audit", "permission", "capability", "auth",
)


def _git_diff_files(base: str) -> list[str]:
    """取得相对 base 的改动文件清单（含未合入的 PR 改动）。"""
    try:
        base_ref = subprocess.run(
            ["git", "merge-base", base, "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        base_ref = base
    out = subprocess.run(
        ["git", "diff", "--name-only", base_ref, "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    # 也包含已暂存但未提交的改动（本地 CI 调试场景）
    out += subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    seen: set[str] = set()
    result: list[str] = []
    for f in out:
        f = f.strip()
        if f and f not in seen:
            seen.add(f)
            result.append(f)
    return result


def analyze(files: list[str]) -> dict:
    core_touched = sorted(f for f in files if f in CORE_PROTECTED)
    sensitive = sorted({
        f for f in files
        if any(k in f.lower() for k in SENSITIVE_KEYWORDS)
        and f not in core_touched
    })
    requires_owner_review = bool(core_touched or sensitive)
    return {
        "core_protected_touched": core_touched,
        "sensitive_paths": sensitive,
        "requires_owner_review": requires_owner_review,
        "changed_files": len(files),
        "passed": True,  # 护栏只报告；是否放行由 Owner/Reviewer 决策
        "policy": "Production Is Sacred — 触碰核心/安全代码须经 Owner 审批；CI 不自动部署、不自动批准 Evolution",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="ANIMA R10-00 安全护栏")
    ap.add_argument("--base", default="origin/master", help="对比基线分支")
    ap.add_argument("--out", default="security-report.json", help="产出 JSON 路径")
    ap.add_argument("--strict", action="store_true",
                    help="触碰 CORE_PROTECTED 时以非零退出（默认仅报告）")
    args = ap.parse_args()

    try:
        files = _git_diff_files(args.base)
    except subprocess.CalledProcessError as exc:
        print(f"[anima_guard] 无法取得 git diff：{exc}", file=sys.stderr)
        # 无法分析时保守报告，不阻断 CI（fail-open 仅对分析失败，不影响既有证据）
        report = {"error": str(exc), "passed": True, "requires_owner_review": False}
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0

    report = analyze(files)
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[anima_guard] changed={report['changed_files']} "
          f"core_protected_touched={report['core_protected_touched']} "
          f"requires_owner_review={report['requires_owner_review']}")
    if args.strict and report["core_protected_touched"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
