# -*- coding: utf-8 -*-
"""外科手术式部署：只推指定文件到生产，带备份 + 逐文件 md5 双验 + 重启 + 只读自检。

用法：python deploy_files.py <rel_path> [<rel_path> ...]
"""
import hashlib
import pathlib
import sys
import time

import paramiko

#: 仓库根：向上找含 pyproject.toml 的目录（本文件可从 deploy/ 或 .workbuddy/audit/ 运行）
def _find_root() -> pathlib.Path:
    for p in [pathlib.Path(__file__).resolve().parent, *pathlib.Path(__file__).resolve().parents]:
        if (p / "pyproject.toml").exists():
            return p
    return pathlib.Path(__file__).resolve().parents[1]


ROOT = _find_root()
REMOTE = "/opt/anima"

cfg = {}
for line in (ROOT / "deploy" / "vps.local.env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip()

rels = sys.argv[1:]
assert rels, "需指定待推文件"
for r in rels:
    assert (ROOT / r).is_file(), f"本地不存在: {r}"
    assert r.startswith("anima/"), f"只允许推 anima/ 下文件: {r}"

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(cfg["ANIMA_DEPLOY_HOST"], port=22, username=cfg["ANIMA_DEPLOY_USER"],
            password=cfg["ANIMA_DEPLOY_PASS"], timeout=20, banner_timeout=30)
sftp = ssh.open_sftp()


def run(cmd: str, timeout: int = 60) -> str:
    _i, o, e = ssh.exec_command(cmd, timeout=timeout)
    out = o.read().decode("utf-8", "replace")
    err = e.read().decode("utf-8", "replace")
    return out + (("\n[stderr] " + err.strip()) if err.strip() else "")


stamp = time.strftime("%Y%m%d_%H%M%S")
bkdir = f"{REMOTE}/_backup_r7_{stamp}"
print(f"[1/5] 备份原文件 -> {bkdir}")
# 🔴 2026-10-16（D3）：备份**保留目录结构**（旧版用 `tr '/' '_'` 扁平化 ⇒ 文件名里的
# 下划线造成歧义、不可逆，71 个历史备份因此无法自动回滚）；同时写 `_MANIFEST.json`
# （原路径 / md5 / 本地 commit），让 `deploy/rollback.py` 能精确恢复。
_bk_cmds = " && ".join(
    f"mkdir -p {bkdir}/$(dirname {r}) && cp -p {REMOTE}/{r} {bkdir}/{r}" for r in rels
)
print(run(f"{_bk_cmds} && find {bkdir} -type f | sort").strip())

print("\n[2/5] 上传前：生产原文件 md5")
before = {}
for r in rels:
    out = run(f"md5sum {REMOTE}/{r}").strip()
    before[r] = out.split()[0] if out else "MISSING"
    print(f"   {r}  {before[r]}")

print("\n[3/5] sftp 上传（经 /tmp 中转，保留属主/权限）")
for r in rels:
    local = ROOT / r
    tmp = f"/tmp/r7_{r.replace('/', '_')}"
    sftp.put(str(local), tmp)
    # 保留目标文件的 mode/owner
    run(f"chown --reference={REMOTE}/{r} {tmp} 2>/dev/null; "
        f"chmod --reference={REMOTE}/{r} {tmp} 2>/dev/null; "
        f"mv -f {tmp} {REMOTE}/{r}")
    print(f"   put {r}")

print("\n[4/5] 上传后逐文件 md5 双验（本地 vs 生产）")
ok = True
for r in rels:
    local_md5 = hashlib.md5((ROOT / r).read_bytes()).hexdigest()
    out = run(f"md5sum {REMOTE}/{r}").strip()
    remote_md5 = out.split()[0] if out else "MISSING"
    flag = "OK" if local_md5 == remote_md5 else "!!MISMATCH!!"
    if local_md5 != remote_md5:
        ok = False
    print(f"   [{flag}] {r}\n        local ={local_md5}\n        remote={remote_md5}")

print("\n[4.5/5] 写备份清单 _MANIFEST.json（供 deploy/rollback.py 精确恢复）")
import json  # noqa: E402
import subprocess  # noqa: E402

after = {}
for r in rels:
    out = run(f"md5sum {REMOTE}/{r}").strip()
    after[r] = out.split()[0] if out else "MISSING"
try:
    _commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT),
                             capture_output=True, text=True).stdout.strip()
except Exception:  # noqa: BLE001
    _commit = ""
manifest = {
    "stamp": stamp,
    "git_commit": _commit,
    "tool": "deploy_files.py",
    "files": [
        {"path": r, "backup": r,
         "md5_before": before.get(r), "md5_after": after.get(r)}
        for r in rels
    ],
}
_mf = pathlib.Path("/tmp") / f"_MANIFEST_{stamp}.json"
_mf.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
sftp.put(str(_mf), f"{bkdir}/_MANIFEST.json")
print(f"    {bkdir}/_MANIFEST.json  ({len(rels)} 文件, commit={_commit or '?'})")

if not ok:
    print("\n!! md5 不一致，拒绝重启。回滚：")
    print(f"   python deploy/rollback.py --to {bkdir} --yes")
    sys.exit(2)

print("\n[5/5] restart anima + 只读自检")
print(run("systemctl restart anima").strip())
time.sleep(4)
print(run("systemctl is-active anima anima-console anima-napcat").strip())
print(run("systemctl show anima -p MainPID -p NRestarts -p ActiveEnterTimestamp --no-pager").strip())

sftp.close()
ssh.close()
print("\n[done] 部署完成。备份目录:", bkdir)
