#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""版本级回滚：把生产文件恢复到某次部署**之前**的状态。

背景（V3 北极星四体验 D3「回滚链路」）
------------------------------------
此前生产只有「被覆盖文件的碎片备份」，且：

  * 命名**扁平化**（`tr '/' '_'`）⇒ `anima_tools_builtin_agent_tools.py` 无法可靠
    反推回 `anima/tools/builtin/agent_tools.py`（文件名自身含下划线，歧义有损）；
  * **无清单** ⇒ 不知道某个备份来自哪次部署、包含哪些文件；
  * **无脚本** ⇒ 出事只能手工 `cp` 猜路径。

2026-10-16 起 `deploy_files.py` 改为**保留目录结构** + 写 `_MANIFEST.json`。
本脚本据此提供一键回滚。

用法
----
    python deploy/rollback.py --list
    python deploy/rollback.py --to 20261016_231312 --dry-run
    python deploy/rollback.py --to /opt/anima/_backup_r7_20261016_231312 --yes

安全设计
--------
  * **默认 dry-run**；必须显式 `--yes` 才写生产。
  * 恢复前自动把**当前**状态另存为新备份 ⇒ **回滚本身也可回滚**。
  * 按 `_MANIFEST.json` 精确恢复并**校验 md5**（应等于清单里的 `md5_before`）。
  * **无清单的旧备份拒绝自动回滚**（扁平命名有损，强行猜测会写错文件）——
    只列出并给出人工路径，绝不猜。
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time

import paramiko

ROOT = pathlib.Path(__file__).resolve().parents[1]
REMOTE = "/opt/anima"


def _load_cfg() -> dict:
    p = ROOT / "deploy" / "vps.local.env"
    cfg: dict[str, str] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg


class Remote:
    def __init__(self) -> None:
        cfg = _load_cfg()
        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.ssh.connect(cfg["ANIMA_DEPLOY_HOST"], port=22, username=cfg["ANIMA_DEPLOY_USER"],
                         password=cfg["ANIMA_DEPLOY_PASS"], timeout=20, banner_timeout=30)

    def run(self, cmd: str, timeout: int = 120) -> str:
        _i, o, e = self.ssh.exec_command(cmd, timeout=timeout)
        out = o.read().decode("utf-8", "replace")
        err = e.read().decode("utf-8", "replace")
        return out + (("\n[stderr] " + err.strip()) if err.strip() else "")

    def md5(self, path: str) -> str:
        out = self.run(f"md5sum {path} 2>/dev/null").strip()
        return out.split()[0] if out and "No such" not in out else "MISSING"

    def close(self) -> None:
        self.ssh.close()


def _list_backups(r: Remote) -> None:
    dirs = [d for d in r.run(f"ls -d {REMOTE}/_backup_* 2>/dev/null").split() if d.strip()]
    print(f"共 {len(dirs)} 个备份\n")
    print("%-46s %-8s %-10s %s" % ("备份目录", "清单", "文件数", "commit"))
    for d in dirs:
        has = r.run(f"test -f {d}/_MANIFEST.json && echo Y || echo N").strip() == "Y"
        if has:
            raw = r.run(f"cat {d}/_MANIFEST.json")
            import json

            try:
                m = json.loads(raw)
                n = len(m.get("files", []))
                c = m.get("git_commit", "?")
            except Exception:  # noqa: BLE001
                n, c = "?", "?"
            print("%-46s %-8s %-10s %s" % (d, "有", n, c))
        else:
            print("%-46s %-8s %-10s %s" % (d, "无", "?", "—（旧扁平格式，不可自动回滚）"))


def _rollback(r: Remote, target: str, dry: bool) -> int:
    import json

    if not target.startswith("/"):
        cands = [d for d in r.run(f"ls -d {REMOTE}/_backup_*{target}* 2>/dev/null").split() if d.strip()]
        if len(cands) != 1:
            print(f"!! 无法唯一匹配 '{target}'（命中 {len(cands)} 个）")
            return 2
        target = cands[0]
    if not r.run(f"test -d {target} && echo Y").strip() == "Y":
        print(f"!! 目录不存在: {target}")
        return 2

    raw = r.run(f"cat {target}/_MANIFEST.json 2>/dev/null")
    try:
        m = json.loads(raw)
    except Exception:  # noqa: BLE001
        print(f"!! {target} 无 _MANIFEST.json ⇒ **拒绝自动回滚**（旧扁平命名有损，猜测会写错文件）。")
        print("   请人工确认备份内文件与目标路径的对应关系后再操作。")
        return 3

    files = m.get("files", [])
    print(f"目标备份 : {target}")
    print(f"部署时间 : {m.get('stamp')}   commit={m.get('git_commit', '?')}")
    print(f"待恢复   : {len(files)} 个文件\n")

    plan = []
    for f in files:
        path = f["path"]
        want = f.get("md5_before")
        cur = r.md5(f"{REMOTE}/{path}")
        action = "恢复" if want and cur != want else ("已是目标态" if want and cur == want else "恢复(无基准)")
        plan.append((path, want, cur, action))
        print("  %-46s cur=%s  ->  want=%s  %s" % (path, (cur or "?")[:10], (want or "?")[:10], action))

    if dry:
        print("\n[dry-run] 未写入。加 --yes 执行。")
        return 0

    # 恢复前：把当前状态另存（回滚本身也可回滚）
    stamp = time.strftime("%Y%m%d_%H%M%S")
    pre = f"{REMOTE}/_backup_r7_pre-rollback_{stamp}"
    print(f"\n[1/3] 先备份当前状态 -> {pre}")
    cmds = " && ".join(
        f"mkdir -p {pre}/$(dirname {p}) && cp -p {REMOTE}/{p} {pre}/{p}" for p, *_ in plan
    )
    r.run(cmds)
    r.run("python3 -c \"import json,os\" 2>/dev/null")  # no-op 保持连接
    import json as _json

    mf = _json.dumps({"stamp": stamp, "tool": "rollback.py",
                      "files": [{"path": p, "backup": p, "md5_before": cur} for p, _, cur, _ in plan]},
                     ensure_ascii=False, indent=2)
    r.run(f"cat > {pre}/_MANIFEST.json <<'EOF'\n{mf}\nEOF")

    print(f"[2/3] 从 {target} 恢复")
    for path, want, _cur, _a in plan:
        r.run(f"cp -p {target}/{path} {REMOTE}/{path}")
        got = r.md5(f"{REMOTE}/{path}")
        flag = "OK" if (not want or got == want) else "!! MISMATCH !!"
        print("  [%s] %-44s %s" % (flag, path, got[:10]))

    print("[3/3] restart anima + 健康")
    r.run("systemctl restart anima")
    time.sleep(4)
    print("  ", r.run("systemctl is-active anima anima-console anima-napcat").strip().replace("\n", " "))
    print("  ", r.run("systemctl show anima -p MainPID -p NRestarts --no-pager").strip().replace("\n", " "))
    print(f"\n[done] 已回滚到 {target}")
    print(f"       当前状态已另存：{pre}（可再次回滚）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="版本级回滚（默认 dry-run）")
    ap.add_argument("--list", action="store_true", help="列出所有备份")
    ap.add_argument("--to", help="备份目录全路径，或时间戳片段")
    ap.add_argument("--dry-run", action="store_true", help="只显示计划（默认行为）")
    ap.add_argument("--yes", action="store_true", help="确认执行写入")
    a = ap.parse_args()

    r = Remote()
    try:
        if a.list or not a.to:
            _list_backups(r)
            if not a.to:
                print("\n用 --to <时间戳> [--yes] 执行回滚。")
            return 0
        return _rollback(r, a.to, dry=not a.yes)
    finally:
        r.close()


if __name__ == "__main__":
    sys.exit(main())
