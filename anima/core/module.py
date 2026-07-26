"""Module —— 所有功能模块的抽象基类。

层级：
- Module: 最小生命周期接口（init/restore/persist/shutdown）
- CognitiveModule: 认知模块标准协议，增加状态快照 + prompt 片段注入
"""

from __future__ import annotations
import abc
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from anima.core.anima_core import AnimaCore
    from anima.core.persistence import PersistenceProvider
    from anima.pipeline import PipelineContext
    from anima.pipeline.context import PromptFragment

logger = logging.getLogger("anima.core.module")


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


class CognitiveModule(Module):
    """认知模块标准接口。

    所有 anima/cognitive/ 下的子系统应继承此类，由 AnimaCore 集中管理
    生命周期（init → restore → 运行期 persist 调度 → shutdown）。

    子类职责：
    - 设置 ``name`` 类属性（唯一标识，用于服务注册表 & WebUI 状态面板）
    - 实现 ``restore()`` / ``persist()`` 完成状态加载与落盘
    - 可选覆写 ``get_status()`` 暴露可观测指标
    - 可选覆写 ``get_fragments()`` 向 system prompt 注入认知片段
    """

    name: str = "cognitive"

    def __init__(self, data_dir: str = "data"):
        from anima.core.persistence import PersistenceProvider
        self._persist: "PersistenceProvider" = PersistenceProvider(data_dir)
        self._data_dir = data_dir
        self._core: "AnimaCore | None" = None
        self._dirty = False  # 脏标记：状态变更后置 True，批量 persist 后清零

    async def init(self, core: "AnimaCore") -> None:
        """绑定 AnimaCore 引用，子类可覆写以订阅事件。"""
        self._core = core

    async def restore(self) -> None:
        """从持久化恢复状态。子类必须实现。"""

    async def persist(self) -> None:
        """写回持久化。子类必须实现。成功后应清零 _dirty。"""

    async def shutdown(self) -> None:
        """关闭前最终落盘。"""
        if self._dirty:
            try:
                await self.persist()
            except Exception:
                logger.exception("%s shutdown persist 失败", self.name)

    # ------------------------------------------------------------------
    # 可观测性
    # ------------------------------------------------------------------

    def get_status(self) -> dict[str, Any]:
        """WebUI 可观测状态快照。子类覆写以暴露更多指标。"""
        return {"name": self.name, "ok": True, "dirty": self._dirty}

    # ------------------------------------------------------------------
    # Prompt 注入
    # ------------------------------------------------------------------

    def get_fragments(self, ctx: "PipelineContext") -> list["PromptFragment"]:
        """返回要注入 system prompt 的认知片段。

        默认返回空列表；子类按需覆写。priority 数值越小越先被裁剪。
        """
        return []

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def mark_dirty(self) -> None:
        """标记状态已变更，等待下次批量 persist。"""
        self._dirty = True

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r}>"
