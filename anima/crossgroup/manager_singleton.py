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


def get_crossgroup_manager() -> CrossGroupManager:
    global _instance
    if _instance is None:
        data_dir = os.environ.get("ANIMA_CROSSGROUP_DIR", "data/crossgroup")
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
