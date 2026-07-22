"""PersistenceProvider —— 统一原子写（tempfile + os.replace），杜绝半写入损坏。"""

from __future__ import annotations
import json
import os
import tempfile
from pathlib import Path
from typing import Any


class PersistenceProvider:
    """
    所有磁盘写操作必须经此类，禁止模块各自 open() 写文件。
    原子写：先写临时文件 → fsync → os.replace。
    """

    def __init__(self, data_dir: str = "data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    # ---- JSON -----------------------------------------------------------

    def write_json(self, rel_path: str, obj: Any) -> Path:
        """原子写 JSON 文件。返回目标路径。"""
        target = self.data_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        return self._atomic_write(target, json.dumps(obj, ensure_ascii=False, indent=2))

    def read_json(self, rel_path: str, default: Any = None) -> Any:
        """读取 JSON 文件；不存在则返回 default。"""
        p = self.data_dir / rel_path
        if not p.exists():
            return default
        with open(p, "r", encoding="utf-8") as fh:
            return json.load(fh)

    # ---- 纯文本 ---------------------------------------------------------

    def write_text(self, rel_path: str, text: str) -> Path:
        target = self.data_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        return self._atomic_write(target, text)

    def read_text(self, rel_path: str, default: str = "") -> str:
        p = self.data_dir / rel_path
        if not p.exists():
            return default
        return p.read_text(encoding="utf-8")

    # ---- 原子写核心 -----------------------------------------------------

    def _atomic_write(self, target: Path, content: str) -> Path:
        fd, tmp = tempfile.mkstemp(
            dir=str(target.parent), prefix=".tmp_", suffix=target.suffix
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return target
