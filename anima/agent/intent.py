"""Phase 3 意图识别：任务 vs 闲聊。

关键约束（与审计 B0 一致）：判定仅在 permission_level >= 4（即真正的主人，
由 config.permissions.owners 判定，而非 QQ 群主角色）时生效。普通用户的消息
永远走旧路径，绝不进入 Agent 路径。
"""

from __future__ import annotations

import re

from anima.tools import LEVEL_OWNER

# 任务触发正则：含“帮忙/请/去...”+ 动作动词，意图明显的工程/检索诉求。
TASK_TRIGGER_RE = re.compile(
    r"(帮我|请|麻烦|去|给我|我想让你).{0,20}"
    r"(做|写|查|搜|下载|安装|部署|运行|执行|处理|分析|生成|创建|搭建|配置|监控)",
    re.IGNORECASE,
)


def is_agent_task(text: str, permission_level: int) -> bool:
    """仅当权限等级达到主人且文本命中任务触发正则时，才进入 Agent 路径。"""
    if permission_level < LEVEL_OWNER:
        return False
    return bool(TASK_TRIGGER_RE.search(text or ""))
