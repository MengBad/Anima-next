#!/usr/bin/env python3
"""D-13 阶段 3 前置检查：`drive_gate` 能否翻转（2026-11-29）。

## 为什么需要这个脚本

翻转 `drive_gate` 的**后果是双向**的：
  - 对：主动发言有 Drive 依据，更"有自我"；
  - 错：traits 不足时 `_drive_gate_ok()` 判定 False ⇒ **主动发言被静默压死**。

所以"翻门"不能靠感觉，必须先用数据证明三件事（设计稿 §6 阶段 3）。

## 三条前置条件

  1. `peak_intensity` 在真实区间波动（不是恒 1.0 / 恒 0）
  2. CURIOSITY 有实际内容（至少一项 trait > 0.5）
  3. **翻转后主动发言数量不下降** —— 本脚本的核心

## 用法

    # 看当前 traits 状态（不改任何东西）
    python scripts/check_drive_gate_readiness.py --data-dir data

    # 对比两个时间窗的主动发言量（翻门前后各跑一次）
    python scripts/check_drive_gate_readiness.py --data-dir data \\
        --baseline-since "2026-11-29 00:00" --compare-since "2026-11-30 00:00"

退出码：0 = 可以翻；1 = 不建议翻（附原因）。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def _audit_lines(data_dir: Path, since: str | None, until: str | None):
    """从 journalctl 取审计相关日志行（无 journalctl 时退回空）。"""
    cmd = ["journalctl", "-u", "anima", "--no-pager", "-o", "cat"]
    if since:
        cmd += ["--since", since]
    if until:
        cmd += ["--until", until]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return out.stdout.splitlines()
    except Exception:
        return []


def count_speaks(lines: list[str]) -> int:
    """数「主动发言成功」的痕迹。

    口径：`_speak_locked` 成功出站会留 `message_sent` 且 `detail_type=group`；
    这里只数主动路径（日志含"主动"或宪法 Drive 相关）。
    保守起见两者都数，宁可多算。
    """
    n = 0
    for ln in lines:
        if "message_sent" in ln or "主动接话出站成功" in ln:
            n += 1
    return n


def check_traits(data_dir: Path) -> dict:
    """读宪法 traits 实况（不改任何状态）。"""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from anima.constitution.identity import derive_identity
        from anima.constitution.memory_authority import MemoryAuthority
        from anima.constitution.need import derive_need

        auth = MemoryAuthority()
        ident = derive_identity(auth.snapshot_memory())
        need = derive_need(ident)
        return {
            "traits": dict(ident.traits),
            "peak_intensity": ident.peak_intensity,
            "urgency": need.urgency,
            "curiosity": [str(x) for x in (list(need.vectors.values())[1] or [])],
        }
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--baseline-since", default=None)
    ap.add_argument("--compare-since", default=None)
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    info = check_traits(data_dir)

    print("=" * 68)
    print("D-13 阶段 3 前置检查：drive_gate 可否翻转")
    print("=" * 68)

    if "error" in info:
        print(f"读取 traits 失败：{info['error']}")
        return 1

    traits = info["traits"]
    peak = info["peak_intensity"]
    curiosity = [c for c in info["curiosity"] if c.startswith("explore:")]

    print(f"traits          : {traits}")
    print(f"peak_intensity  : {peak}")
    print(f"urgency         : {info['urgency']}")
    print(f"CURIOSITY       : {info['curiosity']}")
    print()

    fails: list[str] = []

    # 条件 1：peak 在真实区间波动
    if not traits:
        fails.append("traits 为空 —— 没有承载数据，或 constitution_reflection_loop 未开")
    elif peak >= 1.0:
        fails.append(f"peak_intensity={peak} 已饱和（恒 1.0 会让 urgency 失去区分度）")
    elif peak <= 0.0:
        fails.append(f"peak_intensity={peak} 为 0 —— traits 存在但强度为 0，异常")

    # 条件 2：CURIOSITY 有内容
    if not curiosity:
        fails.append(f"CURIOSITY 为空（需至少一项 trait > 0.5，当前 traits={traits}）")

    # 条件 3：主动发言量不下降（需要两个窗口都给了才有意义）
    if args.baseline_since and args.compare_since:
        base = count_speaks(_audit_lines(data_dir, args.baseline_since, args.compare_since))
        comp = count_speaks(_audit_lines(data_dir, args.compare_since, None))
        print(f"基线窗口主动发言 : {base}")
        print(f"对比窗口主动发言 : {comp}")
        if base > 0 and comp < base:
            fails.append(f"主动发言从 {base} 降到 {comp} —— 翻转后可能被压死")
        elif base == 0:
            print("注：基线窗口无样本，条件 3 无法判定（按 fail-closed 视为未验证）")
    else:
        print("注：未提供 --baseline-since/--compare-since，跳过条件 3")

    print()
    if fails:
        print("判定：不建议翻转 drive_gate")
        for i, f in enumerate(fails, 1):
            print(f"  {i}. {f}")
        return 1

    print("判定：三条前置条件均满足，可以考虑翻转")
    print("提示：翻转前请先在测试环境跑一轮，确认主动发言量不变。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
