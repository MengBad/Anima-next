"""crossgroup 子系统：跨群用户记忆共享 + 群氛围差异化语调。

移植自 crossgroup_bot（G:/Anima/crossgroup_bot），已剥离其 LLM provider
（Anima-next 复用自身的 core.llm + ReAct）。对外主要入口：

- ``get_crossgroup_manager()`` / ``close_crossgroup_manager()``：进程级单例
- ``CrossGroupManager``：跨群记忆 + 群氛围画像 + 持久化
- ``build_crossgroup_fragment(atmosphere, user_memory, group_id=None)``：拼到 system_prompt 末尾的追加块；群消息传入 group_id 时默认注入本群来源事实（分层记忆）
- ``extract_user_facts(llm, text, ...)``：每条消息 LLM 结构化事实抽取（见 extraction.py）
"""
from .manager_singleton import (
    get_crossgroup_manager,
    close_crossgroup_manager,
    configure_crossgroup_manager,
)
from .memory.manager import CrossGroupManager, MemoryManager
from .memory.user_memory import UserMemory, UserFact
from .atmosphere.profiles import AtmosphereStyle, AtmosphereProfile
from .atmosphere.analyzer import AtmosphereAnalyzer, analyze, merge
from .generation.prompt_builder import (
    build_system_prompt,
    build_crossgroup_fragment,
    ATMOSPHERE_GUIDANCE,
)
from .extraction import extract_user_facts, EXTRACT_TOOLS, EXTRACT_SYSTEM

__all__ = [
    "get_crossgroup_manager",
    "close_crossgroup_manager",
    "configure_crossgroup_manager",
    "CrossGroupManager",
    "MemoryManager",
    "UserMemory",
    "UserFact",
    "AtmosphereStyle",
    "AtmosphereProfile",
    "AtmosphereAnalyzer",
    "analyze",
    "merge",
    "build_system_prompt",
    "build_crossgroup_fragment",
    "ATMOSPHERE_GUIDANCE",
    "extract_user_facts",
    "EXTRACT_TOOLS",
    "EXTRACT_SYSTEM",
]
