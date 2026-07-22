"""启动期配置变更恢复。

配置变更在 WebUI 中应用后可能触发 systemd 重启。若候选配置让进程在
``AnimaCore.start`` 前崩溃，单纯依赖 systemd 会把同一份坏配置反复拉起。
本模块用 data/ 中的轻量 marker 记录一次候选启动：

``awaiting_start`` → ``attempting`` → ``healthy``

第二次看到仍是 ``attempting``，说明上一次没有到达健康点，立即恢复
ConfigService 保存的上一份 YAML 备份，并把变更记录标为自动回滚。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger("anima.config_recovery")

MARKER_NAME = "config_recovery.json"


def _data_dir(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def _marker_path(data_dir: str | Path) -> Path:
    return _data_dir(data_dir) / MARKER_NAME


def _read(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _clear(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def arm(data_dir: str | Path, change_id: str, config_path: str | Path,
        backup_path: str | Path | None) -> bool:
    """Arm one candidate restart and return whether the marker was written."""
    if not backup_path:
        return False
    data = _data_dir(data_dir)
    config = Path(config_path).expanduser().resolve()
    backup = Path(backup_path).expanduser().resolve()
    try:
        backup.relative_to(data)
    except ValueError as exc:
        raise ValueError("配置备份必须位于 data/config_backups 内") from exc
    marker = _marker_path(data)
    current = _read(marker)
    if current and current.get("state") in {"awaiting_start", "attempting"}:
        raise RuntimeError(f"已有配置变更正在等待启动确认：{current.get('change_id', 'unknown')}")
    _write(marker, {
        "state": "awaiting_start",
        "change_id": str(change_id),
        "config_path": str(config),
        "backup_path": str(backup),
        "armed_at": time.time(),
        "attempt_id": uuid.uuid4().hex[:12],
    })
    return True


def clear(data_dir: str | Path, change_id: str | None = None) -> None:
    """Clear recovery state, optionally only when it belongs to ``change_id``."""
    marker = _marker_path(data_dir)
    current = _read(marker)
    if current is None or change_id is None or str(current.get("change_id")) == str(change_id):
        _clear(marker)


def _candidate_data_dirs(config_path: Path) -> list[Path]:
    """Find recovery storage before Pydantic config loading is possible."""
    base = config_path.expanduser().resolve().parent
    candidates = [base / "data"]
    try:
        import yaml
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        configured = ((raw.get("persistence") or {}).get("data_dir")) if isinstance(raw, dict) else None
        if configured:
            value = Path(str(configured)).expanduser()
            candidates.insert(0, value if value.is_absolute() else base / value)
    except Exception:
        pass
    unique: list[Path] = []
    for candidate in candidates:
        path = candidate.resolve()
        if path not in unique:
            unique.append(path)
    return unique


def _restore(config_path: Path, backup_path: Path) -> None:
    if not backup_path.is_file():
        raise FileNotFoundError(f"找不到配置回滚备份：{backup_path}")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    # systemd ProtectSystem 可能不允许在 config.yaml 同目录创建临时文件；
    # 备份本身位于 data/，先复制并 fsync，再覆盖被 ReadWritePaths 放行的目标。
    with backup_path.open("rb") as source, config_path.open("wb") as target:
        shutil.copyfileobj(source, target)
        target.flush()
        os.fsync(target.fileno())


def _mark_change_rolled_back(data_dir: Path, change_id: str, reason: str) -> None:
    path = data_dir / "config_changes.json"
    records = _read(path)
    if not records or not isinstance(records.get(change_id), dict):
        return
    item = records[change_id]
    item["status"] = "rolled_back"
    item["rolled_back_at"] = time.time()
    item["rollback_reason"] = reason
    _write(path, records)


def prepare_start(config_path: str | Path) -> str:
    """Mark the next process attempt, or recover after a failed attempt.

    This function is intentionally safe to call before loading the config. It
    never changes anything when no recovery marker exists.
    """
    config = Path(config_path).expanduser().resolve()
    for data in _candidate_data_dirs(config):
        marker_path = _marker_path(data)
        current = _read(marker_path)
        if not current:
            continue
        state = str(current.get("state", ""))
        if state == "healthy":
            _clear(marker_path)
            return "healthy_cleared"
        if state == "awaiting_start":
            current["state"] = "attempting"
            current["attempt_started_at"] = time.time()
            _write(marker_path, current)
            return "attempting"
        if state != "attempting":
            _clear(marker_path)
            return "stale_cleared"

        marker_config = Path(str(current.get("config_path") or config)).expanduser().resolve()
        backup = Path(str(current.get("backup_path") or "")).expanduser().resolve()
        try:
            backup.relative_to(data)
            if marker_config != config:
                raise ValueError("恢复目标配置路径不一致")
            _restore(config, backup)
            change_id = str(current.get("change_id") or "")
            if change_id:
                _mark_change_rolled_back(data, change_id, "startup_health_failed")
            _clear(marker_path)
            logger.error("候选配置未通过启动健康点，已自动回滚 change_id=%s", change_id or "unknown")
            return "rolled_back"
        except Exception:
            logger.exception("配置自动回滚失败，保留 recovery marker 以便下一次尝试")
            raise
    return "none"


def mark_healthy(data_dir: str | Path) -> bool:
    """Mark a candidate healthy after AnimaCore finished initialization."""
    marker = _marker_path(data_dir)
    current = _read(marker)
    if not current or current.get("state") != "attempting":
        return False
    current["state"] = "healthy"
    current["healthy_at"] = time.time()
    _write(marker, current)
    logger.info("配置候选已通过启动健康点 change_id=%s", current.get("change_id", "unknown"))
    return True


def health(data_dir: str | Path) -> dict[str, Any]:
    """Return a WebUI-safe view of the recovery state."""
    current = _read(_marker_path(data_dir))
    if not current:
        return {"state": "idle"}
    return {
        "state": str(current.get("state", "unknown")),
        "change_id": str(current.get("change_id", "")),
        "armed_at": current.get("armed_at"),
        "attempt_started_at": current.get("attempt_started_at"),
        "healthy_at": current.get("healthy_at"),
        "rollback_reason": current.get("rollback_reason"),
    }
