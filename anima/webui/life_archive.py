"""B6 一生档案(只读) —— 身份快照清点 / 遗忘连续性提示。

不写不删, 只列快照/档案文件供 Life 页展示(传承/恢复只读视图)。
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

_PATTERNS = ("*snapshot*.json", "identity_*.json", "persona_*.bak", "*identity*.json")


def snapshot_inventory(data_dir: str | os.PathLike = "data") -> list[dict[str, Any]]:
    root = Path(data_dir or "data")
    found: list[dict[str, Any]] = []
    if not root.is_dir():
        return found
    for pat in _PATTERNS:
        for p in root.glob(pat):
            if not p.is_file():
                continue
            st = p.stat()
            found.append({"name": p.name, "bytes": st.st_size,
                          "mtime": st.st_mtime, "age_days": round((time.time() - st.st_mtime) / 86400, 1)})
    # 去重(同一文件可能命中多个 pattern) + 按新到旧
    by_name = {f["name"]: f for f in found}
    return sorted(by_name.values(), key=lambda f: -f["mtime"])


def memory_continuity(memory_summary: dict | None) -> dict[str, Any]:
    """从记忆摘要提炼遗忘连续性提示(只读, fail-open)。"""
    if not isinstance(memory_summary, dict):
        return {"available": False}
    return {"available": True, "underlying": memory_summary}
