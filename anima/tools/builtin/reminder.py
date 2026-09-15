"""P2-2 提醒工具 —— 声明式「记住这件事，到点提醒」。

刻意**复用 P1-1 的债账本**（``anima/cognitive/debt.py``），不另起第二套存储：
- 写：``DebtManager.add`` 落 ``data/debts.json``，时间线索由 ``parse_due_hint`` 解析
  （唯一时间口径，避免与 generation 的承诺检测双轨）；
- 到期：由 ``core/autonomy.py`` 日切低峰块里的 ``check_due`` 抬进主动议程
  （复用既有 propose_agenda 管线，继承 quiet_hours / 全局冷却 / 群 autonomy 策略）；
- 读：``cognition`` 每轮把 pending 债注入上下文。

因此本模块只提供「模型能可靠调用的入口」，不承担调度与出站。
"""

from __future__ import annotations

import time
from typing import Any

from anima.tools import LEVEL_USER, Tool


def _manager(core: Any):
    from anima.cognitive.debt import get_debt_manager

    data_dir = getattr(getattr(core.config, "persistence", None), "data_dir", "data")
    return get_debt_manager(data_dir)


def _enabled(core: Any) -> bool:
    cfg = getattr(core.config, "debt_tracking", None)
    return bool(getattr(cfg, "enabled", True))


def make_set_reminder(core: Any):
    async def set_reminder(what: str, when: str = "", *, group_id: str = "",
                           user_id: str = "") -> str:
        """创建一个到期提醒。``when`` 为空表示「不限时间，仅记账待跟进」。"""
        if not _enabled(core):
            return "提醒功能当前已关闭（debt_tracking.enabled=false）"
        text = (what or "").strip()
        if not text:
            return "错误: 缺少 what 参数（要提醒的内容）"
        hint = (when or "").strip()

        mgr = _manager(core)
        debt = mgr.add(
            text,
            source_group=str(group_id or ""),
            source_user=str(user_id or ""),
            urgency="normal",
            due_hint=hint,
            now=time.time(),
        )
        if debt.due_ts is None:
            if hint:
                return (f"已记下：{text}。但没从「{hint}」里识别出具体时间，"
                        f"只能按「合适时跟进」处理；如需准点提醒请给出明确时间。编号 {debt.id}")
            return f"已记下：{text}（未指定时间，将在合适时跟进）。编号 {debt.id}"
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(debt.due_ts))
        return f"已记下：{text}（{debt.due_hint or stamp} 到期提醒）。编号 {debt.id}"

    return set_reminder


def make_list_reminders(core: Any):
    async def list_reminders(group_id: str = "") -> str:
        """列出尚未完成的提醒。"""
        if not _enabled(core):
            return "提醒功能当前已关闭"
        mgr = _manager(core)
        items = mgr.pending(str(group_id) if group_id else None)
        if not items:
            return "当前没有待跟进的提醒。"
        now = time.time()
        lines = []
        for d in sorted(items, key=lambda x: (x.due_ts is None, x.due_ts or 0))[:15]:
            if d.due_ts is None:
                when = "无期限"
            else:
                when = time.strftime("%m-%d %H:%M", time.localtime(d.due_ts))
                if d.due_ts < now:
                    when += "（已到期）"
            lines.append(f"- [{d.id}] {d.content}｜{when}")
        return "待跟进提醒：\n" + "\n".join(lines)

    return list_reminders


def register_reminder_tools(registry: Any, core: Any) -> None:
    registry.register(Tool(
        "set_reminder",
        "当用户要求「提醒我 / 帮我记住 / 到点告诉我 / 帮我查一下（需要后续跟进）」时使用。"
        "写进承诺账本，到点会自动在群里跟进；不是秒级定时器。"
        "when 用自然语言时间（明天 9 点 / 3 天后 / 2026-09-20 18:00），无法确定时间时留空。",
        {
            "what": {"type": "string", "description": "要提醒的具体内容"},
            "when": {"type": "string", "description": "何时提醒（自然语言时间，可留空）"},
            "group_id": {"type": "string", "description": "目标群号（可留空，默认当前会话）"},
            "user_id": {"type": "string", "description": "被提醒的 QQ 号（可留空）"},
        },
        make_set_reminder(core),
        required=["what"], min_level=LEVEL_USER, priority=2,
    ))
    registry.register(Tool(
        "list_reminders",
        "查看尚未完成的提醒/承诺清单。用户问「你还欠我什么」「之前答应的那件事」时使用。",
        {"group_id": {"type": "string", "description": "限定群号（可留空）"}},
        make_list_reminders(core),
        min_level=LEVEL_USER, priority=2,
    ))
