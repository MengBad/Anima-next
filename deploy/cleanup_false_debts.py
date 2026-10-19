# -*- coding: utf-8 -*-
"""清存量：把「拒绝句被当成承诺」造出来的**假债**标为 cancelled（D-35）。

用法：
    python deploy/cleanup_false_debts.py            # dry-run（默认，只报告）
    python deploy/cleanup_false_debts.py --yes      # 真执行（停服→备份→改 JSON→起服）

判定口径 = **当前代码的** `anima.constants.commitment_match`
（即"这条债在今天还造得出来吗"），而不是字符串匹配某几句。

为什么必须停服改：
  `DebtManager` 是**进程内单例**（`get_debt_manager`），运行期直接改 JSON
  会被下一次 `save()` 用内存里的旧值覆盖掉。

为什么只动 debts，不动 agenda / intent_ledger：
  生产实测 90 条 agenda 的 status 全是 consumed/expired/cancelled（**已终态、不会再触发**），
  ledger 同理。它们是有价值的历史凭证，改动等于篡改账本；
  真正还会在 T+6h 变成 `debt_due` 议程继续回灌 prompt 的，只有 **pending 债**。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time

import paramiko

ROOT = pathlib.Path(__file__).resolve().parents[1]
REMOTE = "/opt/anima"


def _cfg() -> dict:
    raw = (ROOT / "deploy" / "vps.local.env").read_text(encoding="utf-8")
    out = {}
    for line in raw.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _connect(cfg):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(cfg["ANIMA_DEPLOY_HOST"], port=22, username=cfg["ANIMA_DEPLOY_USER"],
                password=cfg["ANIMA_DEPLOY_PASS"], timeout=20, banner_timeout=30,
                allow_agent=False, look_for_keys=False)
    return ssh


def _run(ssh, cmd: str, timeout: int = 120) -> str:
    _i, o, e = ssh.exec_command(cmd, timeout=timeout)
    out = o.read().decode("utf-8", "replace")
    err = e.read().decode("utf-8", "replace")
    return out + (("\n[stderr]" + err.strip()) if err.strip() else "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="真执行（默认 dry-run）")
    args = ap.parse_args()

    sys.path.insert(0, str(ROOT))
    from anima.constants import commitment_match

    cfg = _cfg()
    ssh = _connect(cfg)
    sftp = ssh.open_sftp()
    tmpdir = ROOT / ".workbuddy" / "tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    local = tmpdir / "debts.snapshot.json"
    sftp.get(f"{REMOTE}/data/debts.json", str(local))
    data = json.loads(local.read_text(encoding="utf-8"))
    print(f"快照: {len(local.read_bytes())} 字节")

    if isinstance(data, list):
        container, items = "list", data
    else:
        container = next(k for k in ("debts", "items") if isinstance(data.get(k), list))
        items = data[container]
    print(f"结构: {container}，条目 {len(items)}")

    pending = [d for d in items if str(d.get("status")) in ("pending", "open")]
    false_debts = [
        d for d in pending
        if commitment_match("%s %s" % (d.get("content") or "", d.get("due_hint") or "")) is None
    ]
    print(f"pending {len(pending)} 条，其中按当前判据**造不出来**的 {len(false_debts)} 条")
    for d in false_debts:
        print("  - %s | %s" % (d.get("id"), str(d.get("content"))[:60]))
    kept = [d for d in pending if d not in false_debts]
    print("保留（判据认为仍是承诺）:")
    for d in kept:
        print("  + %s | %s" % (d.get("id"), str(d.get("content"))[:60]))

    if not args.yes:
        print("\n[dry-run] 未改动任何东西；加 --yes 执行。")
        sftp.close(); ssh.close()
        return 0

    if not false_debts:
        print("无需改动。")
        sftp.close(); ssh.close()
        return 0

    stamp = time.strftime("%Y%m%d_%H%M%S")
    bk = f"{REMOTE}/_backup_d35_{stamp}"
    print(f"\n[1/5] 停服 + 备份 -> {bk}")
    print(_run(ssh, f"systemctl stop anima && mkdir -p {bk} && "
                     f"cp -p {REMOTE}/data/debts.json {bk}/debts.json && "
                     f"cp -p {REMOTE}/data/agenda.json {bk}/agenda.json 2>/dev/null; "
                     f"cp -p {REMOTE}/data/intent_ledger.json {bk}/intent_ledger.json 2>/dev/null; "
                     f"ls -l {bk}").strip())

    now = time.time()
    for d in false_debts:
        d["status"] = "cancelled"
        d["completed_at"] = now
    leftover = [d for d in tmpdir.glob("debts.snapshot.json")]
    payload = data if container == "list" else data
    out_path = tmpdir / "debts.cleaned.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print("[2/5] 上传（保留属主/权限）")
    up = f"/tmp/debts_cleaned_{stamp}.json"
    sftp.put(str(out_path), up)
    print(_run(ssh, f"chown --reference={REMOTE}/data/debts.json {up} && "
                     f"chmod --reference={REMOTE}/data/debts.json {up} && "
                     f"mv -f {up} {REMOTE}/data/debts.json && "
                     f"md5sum {REMOTE}/data/debts.json && ls -l {REMOTE}/data/debts.json").strip())

    print("[3/5] 起服")
    print(_run(ssh, "systemctl start anima").strip())
    time.sleep(4)
    print(_run(ssh, "systemctl is-active anima anima-console anima-napcat | tr '\\n' ' '; echo; "
                    "systemctl show anima -p MainPID -p NRestarts --no-pager").strip())

    print("[4/5] 改后核对")
    print(_run(ssh, "cd /opt/anima && .venv/bin/python - <<'PY'\n"
                    "import json\n"
                    "d=json.load(open('data/debts.json'))\n"
                    "items=d if isinstance(d,list) else d.get('debts') or d.get('items') or []\n"
                    "import collections\n"
                    "print('status:', collections.Counter(x.get('status') for x in items).most_common())\n"
                    "PY").strip())

    print("[5/5] 无异常检查")
    print("Traceback:", _run(ssh, "journalctl -u anima --since '-2min' | grep -acE 'Traceback|CRITICAL'").strip())

    sftp.close(); ssh.close()
    print("\n[done] 备份目录:", bk)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
