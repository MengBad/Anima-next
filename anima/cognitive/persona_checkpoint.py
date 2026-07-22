"""persona_core 检查点：读取 / 快照 / 回滚。

为 WebUI「人格回滚」面板与 danger.maybe_core_mutation 提供轻量、原子的
persona_core.yaml 版本管理。每次写新版本前先快照当前版本，环形保留
DriftConfig.checkpoints_keep 份（默认 7），避免无界增长。

所有路径锚定到仓库 config/persona_core.yaml（与 pipeline/stages.load_persona_template 一致）。
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Optional

import yaml

_PKG_ROOT = Path(__file__).resolve().parent.parent.parent  # anima/cognitive -> 仓库根

# 可覆盖的基础目录（仅测试用；生产保持默认仓库 config/）。
_base_dir_override: Optional[Path] = None


def set_base_dir(path: Optional[str | Path]) -> None:
    """测试钩子：把 persona 相关文件重定向到临时目录，避免污染仓库 config/。"""
    global _base_dir_override
    _base_dir_override = Path(path) if path else None


def _base_dir() -> Path:
    return _base_dir_override if _base_dir_override is not None else (_PKG_ROOT / "config")


def persona_path() -> Path:
    return _base_dir() / "persona_core.yaml"


def proposed_path() -> Path:
    return persona_path().with_name("persona_core.proposed.yaml")


def _checkpoint_keep() -> int:
    try:
        from anima.core.config import load_config
        return max(1, int(load_config().drift.checkpoints_keep))
    except Exception:
        return 7


def read_persona_text() -> str:
    """返回 persona_core.yaml 原始文本；缺失返回空串。"""
    p = persona_path()
    try:
        return p.read_text(encoding="utf-8") if p.exists() else ""
    except Exception:
        return ""


def read_persona_prompt() -> str:
    """返回 system_prompt 字段（Prompt Debugger 用）。"""
    try:
        data = yaml.safe_load(read_persona_text()) or {}
        prompt = data.get("system_prompt")
        return prompt if isinstance(prompt, str) else ""
    except Exception:
        return ""


def read_proposed_text() -> Optional[str]:
    """返回核心突变提案文本；无提案返回 None。"""
    p = proposed_path()
    try:
        return p.read_text(encoding="utf-8") if p.exists() else None
    except Exception:
        return None


def list_checkpoints() -> list[dict]:
    """返回已存检查点（按时间倒序），每项含 ts / size / path。"""
    base = persona_path()
    out = []
    try:
        for f in base.parent.glob("persona_core.backup_*.yaml"):
            ts = f.name[len("persona_core.backup_"):-len(".yaml")]
            try:
                out.append({"ts": ts, "size": f.stat().st_size, "path": str(f)})
            except OSError:
                continue
    except Exception:
        return []
    out.sort(key=lambda c: c["ts"], reverse=True)
    return out


def snapshot_persona(reason: str = "") -> Optional[str]:
    """把当前 persona_core.yaml 复制为带时间戳的备份，并环形裁剪。

    Returns:
        新快照的 ts 字符串；当前文件不存在时返回 None。
    """
    src = persona_path()
    if not src.exists():
        return None
    ts = time.strftime("%Y%m%d-%H%M%S")
    if reason:
        ts = f"{ts}-{reason}"
    dst = src.with_name(f"persona_core.backup_{ts}.yaml")
    try:
        shutil.copy2(src, dst)
    except OSError:
        return None
    # 环形裁剪：保留最近 checkpoints_keep 个
    keep = _checkpoint_keep()
    try:
        others = sorted(
            (f for f in src.parent.glob("persona_core.backup_*.yaml")),
            key=lambda f: f.name,
            reverse=True,
        )
        for old in others[keep:]:
            try:
                old.unlink()
            except OSError:
                pass
    except Exception:
        pass
    return ts


def rollback_persona(ts: str) -> bool:
    """把指定检查点恢复为 persona_core.yaml。

    仅接受本模块生成的 backup 文件名（白名单 ts 字符），杜绝路径穿越。
    """
    if not ts or not all(c.isalnum() or c in "-_" for c in ts):
        return False
    src = persona_path().with_name(f"persona_core.backup_{ts}.yaml")
    if not src.exists():
        return False
    try:
        shutil.copy2(src, persona_path())
        return True
    except OSError:
        return False
