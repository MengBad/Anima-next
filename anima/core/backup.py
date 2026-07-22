"""Owner 控制的运行状态快照，不收集聊天正文或密钥。"""
from __future__ import annotations

import json
import os
import time
import zipfile
from pathlib import Path
from typing import Any

from anima.core.persistence import PersistenceProvider


SAFE_FILES = (
    "intent_ledger.json", "workflows.json", "episodes.json", "commitments.json",
    "agenda.json", "calendar_daily.json", "calendar_weekly.json", "stickers.json",
    "sticker_policy.json", "monitors/subscriptions.json", "health_history.json",
)


class BackupManager:
    def __init__(self, core: Any):
        self.core = core
        self.data_dir = Path(core.config.persistence.data_dir)
        self.root = self.data_dir / "backups"
        self.interval_sec = 24 * 3600
        self.retention = 30

    def create(self, reason: str = "owner_snapshot", *, created_at: float | None = None) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        created_at = time.time() if created_at is None else float(created_at)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(created_at))
        path = self.root / f"anima-state-{stamp}.zip"
        suffix = 1
        while path.exists():
            path = self.root / f"anima-state-{stamp}-{suffix}.zip"
            suffix += 1
        manifest: dict[str, Any] = {"created_at": created_at, "reason": str(reason)[:120], "files": []}
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in SAFE_FILES:
                source = self.data_dir / name
                if source.is_file():
                    archive.write(source, arcname=name)
                    manifest["files"].append(name)
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        os.utime(path, (created_at, created_at))
        return {"id": path.stem, "name": path.name, "created_at": manifest["created_at"],
                "files": manifest["files"], "size_bytes": path.stat().st_size}

    def list(self, limit: int = 30) -> list[dict[str, Any]]:
        if not self.root.exists(): return []
        rows=[]
        for path in sorted(self.root.glob("anima-state-*.zip"), key=lambda item:item.stat().st_mtime, reverse=True)[:limit]:
            rows.append({"id":path.stem,"name":path.name,"created_at":path.stat().st_mtime,"size_bytes":path.stat().st_size})
        return rows

    def maintain(self, *, now: float | None = None) -> dict[str, Any]:
        """Create at most one daily state snapshot and retain a bounded history."""
        now = time.time() if now is None else float(now)
        items = self.list(limit=10_000)
        latest_at = float(items[0]["created_at"]) if items else 0.0
        created = None
        if not latest_at or now - latest_at >= self.interval_sec:
            created = self.create("scheduled_state_snapshot", created_at=now)
            items = self.list(limit=10_000)
        removed = 0
        for item in items[self.retention:]:
            try:
                (self.root / item["name"]).unlink()
                removed += 1
            except OSError:
                continue
        return {**self.health(now=now), "created": created, "removed": removed}

    def health(self, *, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        items = self.list(limit=10_000)
        latest = items[0] if items else None
        latest_at = float(latest["created_at"]) if latest else 0.0
        return {"count": len(items), "retention": self.retention, "interval_sec": self.interval_sec,
                "latest_at": latest_at or None,
                "next_due_at": (latest_at + self.interval_sec) if latest_at else now,
                "overdue": not latest_at or now - latest_at >= self.interval_sec}
