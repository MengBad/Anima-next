"""P2-B —— 中央路径门面（全项目唯一读取 persistence.data_dir 的权威入口）。

背景：存量 data_dir 绑定 ~718 处散落各模块、各自 str(config.persistence.data_dir) 内联。
本模块收口为单一解析点：
- :func:set_data_dir 由 anima_core 初始化时从 config.persistence.data_dir 接线（唯一来源）。
- 所有**新代码**一律经 :func:data_dir / :func:Paths 取路径，不得再内联读 data_dir。
- 存量按文件分片迁移（每片 diff 极小 + 全量测试），本模块就是迁移落点。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

_cached_base: Path | None = None


def set_data_dir(value: Any) -> None:
    """钉住全局数据根目录（生产=config.persistence.data_dir）。幂等。"""
    global _cached_base
    _cached_base = Path(str(value))


def data_dir() -> Path:
    """当前数据根目录（未接线时回退 'data'，fail-safe）。"""
    global _cached_base
    if _cached_base is None:
        _cached_base = Path("data")
    return _cached_base


def child(*parts: str) -> Path:
    """data_dir 下相对子路径（如 child("crossgroup", gid)）。"""
    return data_dir().joinpath(*parts)


class Paths:
    """面向消费方的门面：给定数据根，提供具名子路径。

    新增具名路径（如 chat_memory/selfcode/owner_tasks）集中登记于此，
    替代散落的 f"{data_dir}/{name}"。
    """

    def __init__(self, base: str | Path | None = None) -> None:
        self._base = Path(base) if base is not None else data_dir()

    @property
    def base(self) -> Path:
        return self._base

    def resolve(self, *parts: str) -> Path:
        return self._base.joinpath(*parts)

    # ---- 具名子路径登记 ----
    @property
    def chat_memory(self) -> Path:
        return self._base / "chat_memory.json"

    @property
    def crossgroup(self) -> Path:
        return self._base / "crossgroup"
