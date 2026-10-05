# -*- coding: utf-8 -*-
"""外科手术式部署：本地 → 生产（备份 + 上传 + md5 双向校验）。

本地执行（不进 run_remote）：
    python .workbuddy/audit/deploy_upload.py --dry-run
    python .workbuddy/audit/deploy_upload.py

安全特性
--------
- **先备份**：`/opt/anima/_backup_r8_<stamp>/` 按原目录结构 `cp -p`，first-wins。
- **逐文件 md5 双验**：上传后本地 md5 与远端 md5 必须逐字节相等，否则报错退出。
- **不上传 config.yaml**：生产的活配置独立于 git，绝不覆盖。
- `--dry-run` 只打印计划与远端现状，不写任何东西。
"""
import argparse
import datetime
import hashlib
import os
import pathlib
import sys

import paramiko

# 🔴 本文件由 `.workbuddy/audit/deploy_upload.py` 迁入版本库（2026-10-05）。
#
# 为什么要迁：`.workbuddy/` 整个被 `.gitignore` 忽略 ⇒ 修好的属主对齐逻辑
# **不进版本库** ⇒ 下一个新增文件仍会落 root，属主缺陷必然复现。
# `deploy/` 是版本化且已有工具的正规位置（`vps.local.env` 仍被 ignore，凭据不外泄）。
# 原件保留在 `.workbuddy/audit/` 作为工作副本，**以本文件为准**。
ROOT = pathlib.Path(__file__).resolve().parents[1]
REMOTE_ROOT = "/opt/anima"

#: 本轮部署目标（相对项目根）。config/default.yaml 仅供 init 使用，不影响运行时，
#: 故不上传以收窄变更面；生产 effective 值由 pydantic 默认（45.0）给出。
FILES = [
    "anima/llm/__init__.py",
    "anima/agent/react.py",
    "anima/pipeline/stages/generation.py",
    "anima/core/config.py",
    "anima/core/autonomy.py",
]


def _load_cfg() -> dict:
    cfg = {}
    for line in (ROOT / "deploy" / "vps.local.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg


def md5_local(p: pathlib.Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--files", default="", help="逗号分隔的相对路径；缺省用内置 FILES")
    ap.add_argument("--backup-tag", default="r8", help="备份目录标签")
    ap.add_argument("--no-restart", action="store_true",
                    help="上传后**不**重启服务（默认会重启，见下方说明）")
    ap.add_argument("--verify-restart", default="",
                    help="重启后断言这些文件已被进程加载（生产绝对路径，逗号分隔）")
    args = ap.parse_args()

    files = [x.strip() for x in args.files.split(",") if x.strip()] or FILES

    cfg = _load_cfg()
    plan = []
    for f in files:
        lp = ROOT / f
        if not lp.exists():
            print(f"❌ 本地缺失: {f}")
            return 2
        plan.append((f, md5_local(lp), lp.stat().st_size))
    FILES_EFF = files

    print("=== 计划上传 ===")
    for f, h, n in plan:
        print(f"  {h}  {n:>8}  {f}")

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(cfg["ANIMA_DEPLOY_HOST"], port=22, username=cfg["ANIMA_DEPLOY_USER"],
                password=cfg["ANIMA_DEPLOY_PASS"], timeout=20, banner_timeout=30)

    def run(cmd: str, timeout: int = 120) -> tuple[int, str]:
        _i, o, e = ssh.exec_command(cmd, timeout=timeout)
        out = o.read().decode("utf-8", "replace")
        err = e.read().decode("utf-8", "replace")
        rc = o.channel.recv_exit_status()
        return rc, (out + (("\n[ERR] " + err) if err.strip() else ""))

    # 远端现状
    print("=== 远端现状 ===")
    rc, out = run("cd /opt/anima && md5sum " + " ".join(FILES_EFF))
    print(out.strip() or f"(rc={rc})")

    if args.dry_run:
        print("\n[dry-run] 未做任何改动。")
        ssh.close()
        return 0

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = f"{REMOTE_ROOT}/_backup_{args.backup_tag}_{stamp}"
    # 备份（保持目录结构）
    #
    # 🔴 2026-10-05：**新增**文件远端不存在 ⇒ `cp` 失败 ⇒ `set -e` 让整次部署中止
    # （r18 部署日志里那条 `ls: cannot access ... observability.py` 就是它的症状，
    #  当时靠"忽略 ERR"蒙过去，代价是新增文件永远走不到 chown 那一步）。
    # ⇒ 改为 `[ -f ] && cp`：**存在才备份**，新增文件跳过（无可备份 = 正常）。
    # 备份完成后断言"**所有原本存在的文件**都已备份"。
    bk_cmds = " && ".join(
        f"mkdir -p {backup}/$(dirname {f}) && ([ ! -f {REMOTE_ROOT}/{f} ] || cp -p {REMOTE_ROOT}/{f} {backup}/{f})"
        for f in FILES_EFF
    )
    rc, out = run(f"set -e; {bk_cmds}; echo BACKUP_OK", timeout=180)
    print("=== 备份 ===")
    print(out.strip() or "(无输出)")
    if "BACKUP_OK" not in out:
        print("❌ 备份失败，中止")
        ssh.close()
        return 3

    # 备份完整性断言：**部署前就存在的文件**必须有备份（新增文件豁免）
    rc, out = run(
        " && ".join(
            f"([ ! -f {REMOTE_ROOT}/{f} ] || [ -f {backup}/{f} ])"
            for f in FILES_EFF
        ) + " && echo BACKUP_ASSERT_OK",
        timeout=120,
    )
    if "BACKUP_ASSERT_OK" not in (out or ""):
        print(f"❌ 备份完整性断言失败（某个已存在文件没被备份）: {(out or '').strip()}")
        ssh.close()
        return 3
    print("  BACKUP_ASSERT_OK（新增文件已豁免）")

    # 上传
    print("=== 上传 ===")
    sftp = ssh.open_sftp()
    for f, h, n in plan:
        sftp.put(str(ROOT / f), f"{REMOTE_ROOT}/{f}")
        print(f"  put {f}")
    sftp.close()

    # 🔴 属主对齐（2026-10-05）：`sftp.put` 对**已存在**文件是覆盖（保留属主），
    # 对**新增**文件是新建 ⇒ 归 SFTP 登录用户（生产为 root）。实测 r18 部署后
    # `anima/pipeline/stages/observability.py` 落成 uid=0，而同批覆盖的两个文件
    # 仍是 uid=999(anima) ⇒ 不一致。
    #
    # 风险方向：记忆里那条「root 属主 → 服务读不到配置 → 起不来」，
    # `.py` 目录可读时不会立刻停服，但属主不一致是**静默的部署缺陷**，
    # 下次若新增的是 `config/*.yaml`（mode 600）就会真停服。
    #
    # 修法：**不猜 uid**，取同目录**已存在邻居**的属主对齐（生产统一 anima:anima）。
    # 找不到邻居（如全新目录）时保持原样并显式告警 —— 不猜。
    print("=== 属主对齐 ===")
    dirs = sorted({os.path.dirname(f) for f, _, _ in plan})
    chown_cmds = []
    for d in dirs:
        chown_cmds.append(
            f"d={REMOTE_ROOT}/{d}; "
            f"ref=$(ls -1 \"$d\" 2>/dev/null | head -50 | while read -r n; do "
            f"  [ -f \"$d/$n\" ] && stat -c '%u:%g' \"$d/$n\"; done | "
            f"  sort | uniq -c | sort -rn | head -1 | awk '{{print $2}}'); "
            f'[ -n "$ref" ] && echo "REF:$d=$ref" || echo "NOREF:$d"'
        )
    rc, out = run("; ".join(chown_cmds), timeout=120)
    ref_by_dir = {}
    for line in out.splitlines():
        if line.startswith("REF:"):
            body = line[4:]
            k, _, v = body.partition("=")
            if v.strip():
                ref_by_dir[k.strip()] = v.strip()

    fixed, warned = [], []
    for f, _, _ in plan:
        # 🔴 key 必须是**远端绝对路径**：REF 段里 `echo "REF:$d=$ref"` 展开的是
        #    `d=/opt/anima/...`，而 `os.path.dirname(f)` 是**相对路径** ⇒ 两者对不上，
        #    导致每次都落进「无可参考邻居」分支（新增文件仍不 chown）。实测踩过。
        d = f"{REMOTE_ROOT}/{os.path.dirname(f)}"
        ref = ref_by_dir.get(d)
        if not ref:
            warned.append(f)
            continue
        rc, out = run(
            f"stat -c '%u:%g' {REMOTE_ROOT}/{f} | grep -qx '{ref}' || "
            f"chown {ref} {REMOTE_ROOT}/{f}",   # 已是目标属主则 chown 也无害（幂等）
            timeout=60,
        )
        if rc != 0:
            warned.append(f + "(chown失败)")
            continue
        fixed.append(f)
    print(f"  已对齐 {len(fixed)} 个 → " + (", ".join(fixed) if fixed else "(无)"))
    if warned:
        print(f"  ⚠️ 未对齐 {len(warned)} 个（同目录无可参考邻居，已保持原样）: {warned}")

    # 🔴 属主断言：整批里出现 root 属主 = 失败（配置类文件同理）
    rc, out = run("cd /opt/anima && stat -c '%U %n' " +
                  " ".join(f"{REMOTE_ROOT}/{f}" for f, _, _ in plan), timeout=120)
    root_owned = [l.split(None, 1)[1] for l in out.splitlines()
                  if l.split(None, 1)[0] == "root"]
    print("=== 属主核验 ===")
    for line in out.splitlines():
        print("  " + line)
    if root_owned:
        print(f"❌ 仍为 root 属主: {root_owned}  —— 备份在 {backup}")
        ssh.close()
        return 5
    print("  ✓ 无 root 属主")

    # 校验
    print("=== md5 校验 ===")
    bad = []
    for f, h, n in plan:
        rc, out = run(f"md5sum {REMOTE_ROOT}/{f}")
        remote = out.split()[0] if out.strip() else "?"
        flag = "OK " if remote == h else "MISMATCH"
        if remote != h:
            bad.append(f)
        print(f"  [{flag}] {f}\n         local={h}\n         remote={remote}")
    if bad:
        print(f"❌ md5 不一致: {bad}  —— 备份在 {backup}")
        ssh.close()
        return 4

    print(f"\n✅ 上传与校验全部通过。备份目录: {backup}")

    # ------------------------------------------------------------------
    # 🔴 重启（2026-10-05 加，r19 踩坑的直接修法）
    # ------------------------------------------------------------------
    # 事故：r19 部署后 **没重启** ⇒ 磁盘是新码、**进程是旧码**，
    # 降级链修复完全没生效（15:16 仍在调 0% 模型）。
    # 根因：**本工具只上传、不重启**，而"要不要重启"当时靠人记 ⇒ 必然漏。
    # ⇒ 默认**重启**；`--no-restart` 才跳过，且**跳过时显式警告**。
    if args.dry_run:
        ssh.close()
        return 0

    if args.no_restart:
        print("\n⚠️⚠️ 已跳过重启（--no-restart）")
        print("   🔴 磁盘上已是新码，但**运行中的进程仍是旧码**！")
        print(f"   🔴 本次上传：{', '.join(f for f, _, _ in plan)}")
        print("   部署后**必须**手动执行：systemctl restart anima")
        ssh.close()
        return 0

    print("\n=== 重启 anima ===")
    rc, out = run("systemctl restart anima; sleep 6; "
                  "systemctl is-active anima; "
                  "systemctl show anima -p MainPID -p NRestarts -p ActiveEnterTimestamp "
                  "--value | tr '\\n' ' '", timeout=180)
    print("  " + (out or "").strip())
    if "active" not in (out or ""):
        print("❌ 重启后服务非 active —— 立刻回滚："
              f"把 {backup}/ 下的文件拷回 {REMOTE_ROOT}/ 再重启")
        ssh.close()
        return 6
    print("  ✓ 服务 active")

    # 🔴 生效断言：**文件 mtime 必须早于进程启动**，否则进程仍在跑旧码
    # （r19 失效形态：文件 15:02:56、进程 04:06:04）
    print("=== 生效断言（文件 mtime 必须 < 进程启动时刻）===")
    rc3, mt = run(f"stat -c '%Y' {REMOTE_ROOT}/{plan[0][0]}", timeout=60)
    rc4, act = run("systemctl show anima -p ActiveEnterTimestamp --value", timeout=60)
    # 把 `ActiveEnterTimestamp`（人类可读）转成 epoch 秒再比
    rc5, boot = run("systemctl show anima -p ExecMainStartTimestamp --value "
                    "--value 2>/dev/null | xargs -0 -I{} date -d {} +%s 2>/dev/null || "
                    "date -d \"$(systemctl show anima -p ActiveEnterTimestamp --value)\" +%s",
                    timeout=60)
    print(f"  文件 mtime(epoch)      = {mt.strip()}")
    print(f"  进程启动 ActiveEnter  = {act.strip()}")
    print(f"  进程启动 epoch        = {boot.strip()}")
    if mt.strip().isdigit() and boot.strip().isdigit():
        if int(mt.strip()) <= int(boot.strip()):
            print("  ✓ 文件不晚于进程启动 ⇒ **进程跑的是新码**")
        else:
            print("  🔴 文件比进程新 ⇒ 进程仍是旧码（这正是 r19 失效的形态）")
            ssh.close()
            return 7
    else:
        print("  ⚠️ 时间戳解析失败（工具链差异），请人工核对上面的两个时间")

    # 可选：锚点断言（补"进程自报"这一层）
    if args.verify_restart:
        print(f"=== 锚点断言 ===")
        for path in [x.strip() for x in args.verify_restart.split(",") if x.strip()]:
            rc2, o2 = run(f"grep -cF -- '{path}' "
                          f"{REMOTE_ROOT}/{path.rsplit('/', 1)[-1]}", timeout=60)
            print(f"  {path} → 远端命中 {(o2 or '').strip()}")

    ssh.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
