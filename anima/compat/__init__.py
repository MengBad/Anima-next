"""AstrBot 兼容层 (Tier 1) —— 垫片方式重实现 astrbot.* 子集。

Tier 1 覆盖（本期）:
- Star/@filter → Module + EventBus 映射
- AstrMessageEvent → 归一化消息事件
- Context → LLM/Channel/Config 注入
- Comp → 消息段构造
- on_llm_request → Pipeline 中间件
- 插件加载器：扫描 plugins/ 目录

不安装 AstrBot，纯垫片实现。
"""

from __future__ import annotations
import logging
from typing import Optional

from anima.compat.astrbot import (
    Comp,
    MessageChain,
    AstrMessageEvent,
    Context,
    Star,
    filter,
    hook,
    _dispatch_plugin_event,
    _build_llm_hook_stage,
)
from anima.compat.loader import load_plugins, unload_plugins, _install_astrbot_modules

logger = logging.getLogger("anima.compat")


def ensure_astrbot_modules() -> None:
    """确保 astrbot.* 命名空间可用。"""
    _install_astrbot_modules()


async def dispatch_compat(core, event: dict) -> Optional[str]:
    """将 Anima 内部事件分发给已加载的 AstrBot 插件。

    core 为 AnimaCore 实例（用于出站回调），与 llm/handler.py 的调用约定一致。
    """
    return await _dispatch_plugin_event(core, event)


__all__ = [
    "Comp",
    "MessageChain",
    "AstrMessageEvent",
    "Context",
    "Star",
    "filter",
    "hook",
    "load_plugins",
    "unload_plugins",
    "ensure_astrbot_modules",
    "dispatch_compat",
    "_build_llm_hook_stage",
]
