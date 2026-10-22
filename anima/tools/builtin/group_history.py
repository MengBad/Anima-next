"""读取群聊历史记录（2026-10-23）。

## 为什么必须有这个工具

在此之前，Agent 的 28 个工具里**没有任何一个能读群聊记录**：
- `read_file` 只能读 `data/` 目录里的文件
- `get_group_members` 只给成员列表
- `get_group_info` 只给群名/人数

⇒ **「帮我盯着群动态」「总结这周的讨论」这类最自然的长期目标结构上做不成。​**
生产实证（2026-10-23）：Goal「整理一次群动态」连续两轮，
Agent 都如实报告「缺少读取历史聊天记录的 API 或工具权限」——
**它没找借口，是真读不到**。

## 安全边界（重要）

| 约束 | 做法 |
|---|---|
| **仅 owner** | `min_level=LEVEL_OWNER` —— 群聊记录含他人发言，不给普通成员 |
| 只读指定群 | 桶 key 前缀 `group_{gid}_` 过滤，**绝不跨群聚合** |
| 都有硬上限 | 时间窗 ≤72h、条数 ≤200（默认 24h / 100 条） |
| 不含内部字段 | 输出只有 `{time, name, role, content}`，无 message_id |
| 不返回私聊 | 桶 key 前缀已排除 `private_*` |

**为什么 owner-only**：这是「读别人说过什么」的能力。虽然本项目的
owner 就是唯一用户，但把它设成 owner-only 能防止未来权限模型下被误开放。
"""
from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger("anima.tools.builtin.group_history")

#: 时间窗硬上限（小时）—— 防一次拉爆。
MAX_HOURS = 72.0
#: 条数硬上限。
MAX_ROWS = 200
#: 单条内容截断（字）。
MAX_LINE_CHARS = 300


def make_read_group_history(core):
    """创建「读取群聊历史」工具 handler。"""

    def _tool(group_id: int = 0, hours: float = 24.0, limit: int = 100) -> str:
        """读取指定群在时间窗内的聊天记录（仅 owner）。"""
        try:
            gid = int(group_id or 0)
        except (TypeError, ValueError):
            return "群号不对喵。"
        if gid <= 0:
            return "要指定群号喵（比如 727631526）。"

        # 夹取上限（不允许调用方传超大值）
        try:
            h = float(hours or 24.0)
        except (TypeError, ValueError):
            h = 24.0
        h = max(0.1, min(MAX_HOURS, h))
        try:
            n = int(limit or 100)
        except (TypeError, ValueError):
            n = 100
        n = max(1, min(MAX_ROWS, n))

        try:
            from anima.memory.chat_memory import get_chat_memory
            mem = get_chat_memory()
        except Exception:
            logger.debug("群历史工具：chat_memory 不可用", exc_info=True)
            return "查询失败：当前读不到聊天记录（记忆模块未就绪）。"

        try:
            rows = mem.get_group_history(gid, hours=h, limit=n) or []
        except Exception:
            logger.debug("群历史工具：读取失败", exc_info=True)
            # 🔴 失败文案**必须带可识别的失败前缀**（`_TOOL_FAILURE_PREFIXES`
            # 约定）。此前写的「读取聊天记录时出错了喵，稍后再试。」不含任何
            # 前缀 ⇒ 被 `tool_result_ok()` 判成**成功** ⇒ 模型以为拿到了数据。
            # 由 tests/test_tool_failure_completeness.py 抓出（它是完备性硬门）。
            return "查询失败：读取聊天记录时出错，稍后再试。"

        if not rows:
            return (f"群 {gid} 在最近 {h:g} 小时内本喵没有可读的记录喵"
                    f"（可能是这期间没消息，或记录已被清理）。")

        lines: list[str] = []
        for r in rows:
            _ts = time.strftime("%m-%d %H:%M", time.localtime(float(r.get("time") or 0)))
            _name = str(r.get("name") or "?")
            _txt = str(r.get("content") or "").replace("\n", " ").strip()
            if not _txt:
                continue
            if len(_txt) > MAX_LINE_CHARS:
                _txt = _txt[:MAX_LINE_CHARS] + "…"
            lines.append(f"[{_ts}] {_name}: {_txt}")

        if not lines:
            return f"群 {gid} 最近 {h:g} 小时内的记录都是空内容喵。"
        head = f"群 {gid} 最近 {h:g} 小时共 {len(lines)} 条记录（按时间正序）："
        return head + "\n" + "\n".join(lines)

    return _tool
