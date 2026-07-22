"""Module —— 所有功能模块的抽象基类。"""

from __future__ import annotations
import abc
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from anima.core.anima_core import AnimaCore


class Module(abc.ABC):
    """注册到 AnimaCore 的最小接口。P0 占位，P1+ 扩展 lifecycle hooks。"""

    name: str = "module"

    async def init(self, core: "AnimaCore") -> None:
        """模块初始化（事件总线已就绪，可 subscribe）。"""

    async def restore(self) -> None:
        """从持久化恢复状态。"""

    async def persist(self) -> None:
        """写回持久化。"""

    async def shutdown(self) -> None:
        """关闭清理。"""
