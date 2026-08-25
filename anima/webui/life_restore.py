"""一生身份快照恢复 —— Owner 可逆(暂存)恢复。

门控: fail-closed。仅 Owner(Bearer+owner_id 双校验在 server 层) + 需 confirm 标志。
恢复产物一律落在 data/life_restore/(validated->原子替换入 restored/ 命名空间, 自动留 backups/ 带时间戳),
绝不碰 chat_memory.json / config / live 身份-记忆大文件。live 身份映射待域定明确后再补"生效"一步。
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from .life_archive import _PATTERNS

FORBIDDEN = {"chat_memory.json", "chat_memory.json.pre-isolation", "evolution_log.jsonl"}


def _matches_inventory(name: str) -> bool:
    from pathlib import Path as _P
    return any(_P(name).match(p) for p in _PATTERNS)


def restore_snapshot(data_dir: str | os.PathLike, snapshot_name: str, confirm: bool, owner_ok: bool) -> dict[str, Any]:
    """把清单内快照可逆暂存到 data/life_restore/. 成功则返回 staged/backup/mtime。"""
    if not owner_ok:
        return {"staged": False, "code": 403, "error": "需要 Owner 授权"}
    if not confirm:
        return {"staged": False, "code": 400, "error": "缺少 confirm 标志"}
    name = (snapshot_name or "").strip()
    if not name or "/" in name or "\\" in name or name.startswith(".") or ".." in name:
        return {"staged": False, "code": 400, "error": "非法的快照文件名"}
    if name in FORBIDDEN or not _matches_inventory(name):
        return {"staged": False, "code": 400, "error": "该文件不在可恢复快照清单内(拒绝碰大文件/配置)"}
    root = Path(data_dir or "data")
    src = root / name
    if not src.is_file():
        return {"staged": False, "code": 404, "error": "快照不存在"}
    if name.endswith(".json"):
        try:
            json.loads(src.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            return {"staged": False, "code": 400, "error": f"快照非合法 JSON: {exc}"}
    base = root / "life_restore"
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = base / "backups" / ts
    restored_dir = base / "restored"
    backup_dir.mkdir(parents=True, exist_ok=True)
    restored_dir.mkdir(parents=True, exist_ok=True)
    backup_file = backup_dir / name
    backup_file.write_bytes(src.read_bytes())  # 恢复前备份当前快照
    tmp = restored_dir / (f".tmp-{ts}-{name}")  # 原子: 同目录临时文件后 os.replace
    tmp.write_bytes(src.read_bytes())
    dst = restored_dir / name
    if dst.exists():
        dst.unlink()
    os.replace(tmp, dst)
    return {"staged": True, "restored_path": str(dst), "backup_path": str(backup_file),
            "name": name, "mtime": src.stat().st_mtime, "read_only": False}


def list_staged(data_dir: str | os.PathLike) -> list[str]:
    base = Path(data_dir or "data") / "life_restore" / "restored"
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_file())


def rollback_restore(data_dir: str | os.PathLike, name: str, confirm: bool, owner_ok: bool) -> dict[str, Any]:
    """把最近一次 backup 覆盖回 restored 命名空间(可逆)。"""
    if not owner_ok:
        return {"rolled_back": False, "code": 403, "error": "需要 Owner 授权"}
    if not confirm:
        return {"rolled_back": False, "code": 400, "error": "缺少 confirm 标志"}
    base = Path(data_dir or "data") / "life_restore"
    backups = sorted((base / "backups").glob("*"), key=lambda p: p.name) if (base / "backups").is_dir() else []
    if not backups:
        return {"rolled_back": False, "code": 404, "error": "没有可回滚的备份"}
    src = backups[-1] / name
    if not src.is_file():
        return {"rolled_back": False, "code": 404, "error": "备份中无该快照"}
    dst_dir = base / "restored"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / name
    tmp = dst_dir / (f".rb-{time.strftime(chr(37)+chr(89)+chr(37)+chr(109)+chr(37)+chr(100)+chr(45)+chr(37)+chr(72)+chr(37)+chr(77)+chr(37)+chr(83))}-{name}")
    tmp.write_bytes(src.read_bytes())
    if dst.exists():
        dst.unlink()
    os.replace(tmp, dst)
    return {"rolled_back": True, "restored_path": str(dst), "from_backup": str(src)}
