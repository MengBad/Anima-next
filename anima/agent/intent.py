"""Phase 3 意图识别：任务 vs 闲聊。

关键约束（与审计 B0 一致）：判定仅在 permission_level >= 4（即真正的主人，
由 config.permissions.owners 判定，而非 QQ 群主角色）时生效。普通用户的消息
永远走旧路径，绝不进入 Agent 路径。
"""

from __future__ import annotations

import re

from anima.tools import LEVEL_OWNER

# 任务触发正则：含“帮忙/请/去...”+ 动作动词，意图明显的工程/检索诉求。
# 修正误触发：原正则把「给我…做（为机器人没有机德）」中的「做」误判为动作，
# 实际是「做为/作为」的一部分。这里从动作集合中剔除裸「做」，并用 _FALSE_POSITIVE_RE
# 排除「做为/作为/叫做/叫作」等不构成真实动作指令的词汇巧合。
TASK_TRIGGER_RE = re.compile(
    r"(帮我|请|麻烦|去|给我|我想让你|帮我看|帮我查)"
    r".{0,18}"
    r"(写|查|搜|下[载载]|安[装装]|部署|运行|执行|处理|分析|生成|创建|搭建|配置|监控|写个|查下|搜下)",
    re.IGNORECASE,
)
# “做为/作为/叫做/叫作”等不构成真实动作指令，命中即排除。
_FALSE_POSITIVE_RE = re.compile(r"做[为作]|叫[做作]|称[为作]", re.IGNORECASE)


def is_agent_task(text: str, permission_level: int) -> bool:
    """仅当权限等级达到主人且文本命中任务触发正则时，才进入 Agent 路径。"""
    if permission_level < LEVEL_OWNER:
        return False
    t = (text or "").strip()
    if not TASK_TRIGGER_RE.search(t):
        return False
    # 仍命中但纯属「做为/作为」等词汇巧合时，不放行。
    if _FALSE_POSITIVE_RE.search(t):
        return False
    return True
