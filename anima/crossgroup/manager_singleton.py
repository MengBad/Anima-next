"""跨群记忆/氛围管理器单例（仿 anima.memory.chat_memory.get_chat_memory）。

进程级单例，data_dir 默认 ``data/crossgroup``，可用环境变量
``ANIMA_CROSSGROUP_DIR`` 覆盖。AnimaCore.stop() 调用 close_crossgroup_manager()
做幂等收尾（JSON 已增量落盘）。
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from .memory.manager import CrossGroupManager

logger = logging.getLogger("anima.crossgroup")

_instance: Optional[CrossGroupManager] = None
_base_dir: Optional[str] = None


def configure_crossgroup_manager(base_dir: str) -> None:
    """核心初始化时把跨群数据钉到配置的 persistence.data_dir 之下。

    修复 M2.5：此前仅 env/默认 "data/crossgroup"，与其它数据目录分裂。
    环境变量 ANIMA_CROSSGROUP_DIR 仍优先（显式覆盖）。
    """
    global _base_dir
    _base_dir = base_dir


def get_crossgroup_manager() -> CrossGroupManager:
    global _instance, _base_dir
    if _instance is None:
        data_dir = os.environ.get("ANIMA_CROSSGROUP_DIR")
        if not data_dir:
            data_dir = os.path.join(_base_dir, "crossgroup") if _base_dir else "data/crossgroup"
        _instance = CrossGroupManager(data_dir=data_dir)
        logger.info("CrossGroupManager 已初始化: data_dir=%s", data_dir)
    return _instance


def close_crossgroup_manager() -> None:
    global _instance
    if _instance is not None:
        try:
            _instance.flush()
        except Exception:
            logger.exception("close_crossgroup_manager flush 失败")
        _instance = None
