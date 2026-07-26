"""公共常量 —— 避免多处重复定义导致不一致。"""

import re

# OneBot/CQ 传输格式正则：匹配所有已知 CQ 类型（含未闭合的尾部 `]?`）。
# 用于出站清理、记忆落盘清洗等场景，确保所有模块使用同一份定义。
CQ_TRANSPORT_RE = re.compile(
    r"\[CQ:(?:image|face|bface|sface|record|video|poke|share|music|location|at)[^\]]*\]?",
    re.I,
)
