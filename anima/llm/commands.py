"""!anima 命令路由表（队列④第一刀，2026-09-12）。

原 ``LLMHandler._handle_command`` 的 if/elif 链整体迁入本模块：
- 分支体逐行原样搬运（缩进不变、行为不变），``self`` 仍指向 handler 实例；
- ``COMMAND_ROUTES`` 按原链顺序登记，**顺序即语义**（approve / task approve /
  agent 等前缀存在包含关系，不得重排）；
- 路由谓词 = 原条件表达式（cmd/raw_cmd 绑定到 CommandCtx），不改写语义。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from anima.core.gateway import OutboundMsg

logger = logging.getLogger(__name__)


# 🔴 2026-09-28 人视角审计 P0-1：**控制面命令的回执不进群**。
# `!anima agent …` 的回执携带任务 ID、`!anima` 命令语法与草稿/审批两步流，属内部
# 机制。生产实证（群 727631526，09-26 19:16–19:17）整群成员都看到了
# 「已创建 Agent 草稿 2157269a7b，回复「!anima agent approve 2157269a7b」后执行。」
# 与「已启动 Agent 任务 2157269a7b」。
# 判据：**群里其他人能不能从这一条推断出它不是人** —— 能，就是 P0。
# 群聊发起 ⇒ 回执只走私聊 owner，群里零出站；私聊发起 ⇒ 行为不变。
# 扩展方式：把其它控制面命令的方法名加进本集合（例如 `_cmd_task_approve`）。
_PRIVATE_ONLY_ROUTES = frozenset({"_cmd_agent"})


def _parse_beijing_reminder_time(value: str) -> float:
    """只接受无歧义的北京时间 ISO 分钟格式，避免自然语言日期误触发。"""
    parsed = datetime.strptime(value.strip(), "%Y-%m-%dT%H:%M")
    return parsed.replace(tzinfo=timezone(timedelta(hours=8))).timestamp()


@dataclass
class CommandCtx:
    """单条 !anima 命令的解析结果与回信上下文。"""
    event: dict
    raw_cmd: str
    cmd: str
    user_id: Any
    group_id: Optional[int]
    detail_type: str
    target_id: Any
    at_prefix: str = ""


class AnimaCommandsMixin:
    """!anima 命令分支实现（由 LLMHandler 继承；self 即 handler 实例）。"""

    async def _cmd_forget_me(self, user_id, group_id, detail_type):
        """forget_me（原样迁移）。"""
        from anima.memory.chat_memory import get_chat_memory
        short_count = get_chat_memory().forget_user(int(user_id))
        long_count = 0
        if getattr(self.core, "vector_memory", None):
            try:
                long_count = await self.core.vector_memory.delete_by_source_uin(int(user_id))
            except Exception:
                logger.exception("用户自助遗忘的向量清理失败")
        audit = getattr(self.core, "audit", None)
        if audit:
            audit.emit("user_forget_requested", user_id=int(user_id),
                       short_sessions=short_count, long_memories=long_count)
        target_id = group_id if detail_type == "group" else user_id
        prefix = f"[CQ:at,qq={user_id}] " if detail_type == "group" else ""
        await self.core.gateway.send(OutboundMsg(
            detail_type, target_id,
            f"{prefix}已清除本喵保存的你的对话记忆（短期档案 {short_count} 份、长期记录 {long_count} 条）。",
        ))
        return True
        return True

    async def _cmd_export_me(self, user_id):
        """export_me（原样迁移）。"""
        from anima.memory.chat_memory import get_chat_memory
        short_memory = get_chat_memory().export_user(int(user_id))
        long_memory = []
        if getattr(self.core, "vector_memory", None):
            try:
                long_memory = await self.core.vector_memory.export_by_source_uin(int(user_id))
            except Exception:
                logger.exception("用户数据导出的向量读取失败")
        payload = {"user_id": int(user_id), "exported_at": time.time(),
                   "chat_memory": short_memory, "long_memory": long_memory}
        output = Path(self.core.config.persistence.data_dir) / "output"
        output.mkdir(parents=True, exist_ok=True)
        path = output / f"anima_export_{int(user_id)}_{int(time.time())}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        audit = getattr(self.core, "audit", None)
        if audit:
            audit.emit("user_export_requested", user_id=int(user_id),
                       short_sessions=len(short_memory), long_memories=len(long_memory))
        await self.core.gateway.send_file(OutboundMsg(
            "private", int(user_id), "你的 Anima 数据导出", extra={"file_path": str(path), "name": path.name},
        ))
        return True
        return True

    async def _cmd_reject_non_owner(self, user_id, group_id, detail_type):
        """reject_non_owner（原样迁移）。"""
        # 非 owner 的 !anima 指令显式拒绝：不进 LLM 聊天（省 token），
        # 安全敏感命令（agent approve 等）不被戏谑化；自助分支
        # （forget me / export me）已在上方放行（2026-09-06 第三轮测试 B4）。
        target_id = group_id if detail_type == "group" else user_id
        prefix = f"[CQ:at,qq={user_id}] " if detail_type == "group" else ""
        await self.core.gateway.send(OutboundMsg(
            detail_type, target_id,
            f"{prefix}这些 !anima 指令只有主人能用喵，有事直接说就好啦～",
        ))
        return True

    async def _cmd_pause(self, ctx):
        """pause（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        self.core.config.autonomy.enabled = False
        if self.core.heartbeat:
            await self.core.heartbeat.stop()
        reply = f"{at_prefix}已暂停自主心跳。"
        return reply

    async def _cmd_resume(self, ctx):
        """resume（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        self.core.config.autonomy.enabled = True
        if self.core.heartbeat and not self.core.heartbeat._running:
            await self.core.heartbeat.start()
        reply = f"{at_prefix}已恢复自主心跳。"
        return reply

    async def _cmd_status(self, ctx):
        """status（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        hb = self.core.heartbeat
        status = "运行中" if (hb and hb._running) else "已停止"
        reply = f"{at_prefix}自主心跳: {status} | 自主开关: {self.core.config.autonomy.enabled}"
        return reply

    async def _cmd_ignore(self, ctx):
        """ignore（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        scope = str(group_id) if detail_type == "group" else "global"
        tokens = [t for t in raw_cmd[len("ignore "):].replace(",", " ").replace("，", " ").split() if t.strip().isdigit()]
        if tokens:
            got = self._ignore_store().add(scope, [int(t) for t in tokens])
            reply = f"{at_prefix}已把 {tokens} 加入忽略名单（{got} 个）。"
        elif detail_type == "group":
            spk = (getattr(self, "_recent_spk", {}) or {}).get(str(group_id), {})
            recent = [int(u) for u, _ in sorted(spk.items(), key=lambda kv: kv[1], reverse=True)]
            if not recent:
                reply = f"{at_prefix}用法：!anima ignore <qq...>（本群最近无发言者可忽略）"
            else:
                self._ignore_store().add(scope, recent[:2])
                reply = f"{at_prefix}已把最近发言的 {recent[:2]} 加入忽略名单。"
        else:
            reply = f"{at_prefix}私聊无法用最近发言解析，请用 !anima ignore <qq...>"
        return reply

    async def _cmd_unignore(self, ctx):
        """unignore（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        scope = str(group_id) if detail_type == "group" else "global"
        tokens = [t for t in raw_cmd[len("unignore "):].replace(",", " ").replace("，", " ").split() if t.strip().isdigit()]
        removed = [t for t in tokens if self._ignore_store().remove(scope, int(t))]
        reply = f"{at_prefix}已移除忽略 {removed}。" if removed else f"{at_prefix}没有需要移除的（可用 !anima ignore list 查看）。"
        return reply

    async def _cmd_ignore_list(self, ctx):
        """ignore_list（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        scope = str(group_id) if detail_type == "group" else "global"
        lst = self._ignore_store().list(scope)
        reply = f"{at_prefix}忽略名单: " + (", ".join(lst) if lst else "空")
        return reply

    async def _cmd_tasks(self, ctx):
        """tasks（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        rows = [f"{x.id} [{x.status}] {x.request[:70]}" for x in self.core.tasks.list()[:10]]
        reply = f"{at_prefix}" + ("\n".join(rows) if rows else "暂无 Owner 任务")
        return reply

    async def _cmd_task_create(self, ctx):
        """task_create（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        request_text = raw_cmd[len("task create "):].strip()
        if not request_text:
            reply = f"{at_prefix}用法：!anima task create <目标>"
        else:
            plan = ["收集日志、配置和相关代码证据", "给出最小修复方案并等待修改确认", "修改后运行测试", "部署/重启前等待部署确认", "验证结果并记录任务结论"]
            task = self.core.tasks.create(request_text, user_id, plan)
            reply = f"{at_prefix}已创建任务 {task.id}（planned）。\n" + "\n".join(f"{i+1}. {v}" for i,v in enumerate(plan)) + f"\n用 !anima task approve {task.id} 进入确认队列。"
        return reply

    async def _cmd_task_approve(self, ctx):
        """task_approve（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        task_id = raw_cmd[len("task approve "):].strip()
        try:
            task = self.core.tasks.get(task_id)
            if task is None:
                raise ValueError("未找到任务")
            result = await self.core.task_agent.execute(task)
            reply = f"{at_prefix}任务 {task.id} 已执行：\n{result[:1800]}"
        except ValueError:
            reply = f"{at_prefix}未找到任务"
        return reply

    async def _cmd_task_cancel(self, ctx):
        """task_cancel（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        try:
            task = self.core.tasks.set_status(raw_cmd[len("task cancel "):].strip(), "cancelled", "Owner 取消")
            reply = f"{at_prefix}任务 {task.id} 已取消"
        except ValueError:
            reply = f"{at_prefix}未找到任务"
        return reply

    async def _cmd_agent(self, ctx):
        """agent（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        sub = raw_cmd[len("agent "):].strip()
        if sub.startswith("approve "):
            tid = sub[len("approve "):].strip()
            reply = f"{at_prefix}{await self.core.agent.approve(tid)}"
        elif sub.startswith("cancel "):
            tid = sub[len("cancel "):].strip()
            reply = f"{at_prefix}{self.core.agent.cancel(tid)}"
        elif sub in ("list", "tasks"):
            reply = f"{at_prefix}" + self.core.agent.status_text()
        elif sub:
            # 主人专属：把整句作为 Agent 请求直接提交执行
            # 09-26：记录会话，产物（生图等）交付回这里。
            task = await self.core.agent.submit(
                sub, int(user_id), auto_run=True,
                session={"detail_type": detail_type, "target_id": target_id},
            )
            reply = f"{at_prefix}已创建 Agent 任务 {task.id}，开始执行（进度将推送）。"
        else:
            reply = (f"{at_prefix}用法：!anima agent <请求> | "
                     f"!anima agent approve <id> | !anima agent cancel <id> | !anima agent list")
        return reply

    async def _cmd_agenda_list(self, ctx):
        """agenda_list（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        items = self.core.agenda.pending()
        if not items:
            reply = f"{at_prefix}当前没有待执行的议程"
        else:
            rows = [f"{item.id}: 群{item.group_id} p={item.priority:.1f} {item.text_intent}" for item in items]
            reply = f"{at_prefix}" + "\n".join(rows[:10])
        return reply

    async def _cmd_calendar_list(self, ctx):
        """calendar_list（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        items = [*self.core.calendar.items, *self.core.calendar.weekly_items]
        items = [item for item in items if item.enabled]
        if not items:
            reply = f"{at_prefix}当前没有每日历事件"
        else:
            rows = [f"{item.id}: {'每周'+str(item.weekday) if hasattr(item, 'weekday') else '每天'} "
                    f"{item.hour:02d}:{item.minute:02d} 群{item.group_id} {item.text}" for item in items]
            reply = f"{at_prefix}" + "\n".join(rows[:10])
        return reply

    async def _cmd_calendar_daily(self, ctx):
        """calendar_daily（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        try:
            _, _, group_text, hhmm, reminder = raw_cmd.split(" ", 4)
            item = self.core.calendar.add_daily(int(group_text), hhmm, reminder)
            audit = getattr(self.core, "audit", None)
            if audit:
                audit.emit("calendar_event_created", calendar_id=item.id, group_id=item.group_id)
            reply = (f"{at_prefix}已添加每日历事件 {item.id}："
                     f"每天北京时间 {item.hour:02d}:{item.minute:02d} 提醒群 {item.group_id}。")
        except (TypeError, ValueError):
            reply = f"{at_prefix}用法：!anima calendar daily <群号> <HH:MM> <提醒内容>"
        return reply

    async def _cmd_calendar_weekly(self, ctx):
        """calendar_weekly（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        try:
            _, _, group_text, weekday, hhmm, reminder = raw_cmd.split(" ", 5)
            item = self.core.calendar.add_weekly(int(group_text), int(weekday), hhmm, reminder)
            reply = (f"{at_prefix}已添加每周日历事件 {item.id}：周{item.weekday} "
                     f"{item.hour:02d}:{item.minute:02d} 提醒群 {item.group_id}。")
        except (TypeError, ValueError):
            reply = f"{at_prefix}用法：!anima calendar weekly <群号> <星期1-7> <HH:MM> <提醒内容>"
        return reply

    async def _cmd_calendar_cancel(self, ctx):
        """calendar_cancel（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        event_id = raw_cmd[len("calendar cancel "):].strip()
        reply = f"{at_prefix}{'已取消日历事件' if self.core.calendar.cancel(event_id) else '未找到日历事件'}：{event_id}"
        return reply

    async def _cmd_agenda_add(self, ctx):
        """agenda_add（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        try:
            _, _, group_text, intent = raw_cmd.split(" ", 3)
            item = self.core.propose_agenda(int(group_text), intent)
            if item is None:
                raise ValueError("内容无效")
            reply = f"{at_prefix}已加入议程 {item.id}，会受主动发言频率与预算限制"
        except (TypeError, ValueError):
            reply = f"{at_prefix}用法：!anima agenda add <群号> <表达意图>"
        return reply

    async def _cmd_agenda_cancel(self, ctx):
        """agenda_cancel（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        agenda_id = raw_cmd[len("agenda cancel "):].strip()
        reply = f"{at_prefix}{'已取消议程' if self.core.agenda.cancel(agenda_id) else '未找到待执行议程'}：{agenda_id}"
        return reply

    async def _cmd_remind_at(self, ctx):
        """remind_at（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        try:
            _, _, group_text, at_text, reminder = raw_cmd.split(" ", 4)
            target_group = int(group_text)
            not_before = _parse_beijing_reminder_time(at_text)
            now = time.time()
            if target_group <= 0 or not reminder.strip() or not (now < not_before <= now + 366 * 86400):
                raise ValueError
            item = self.core.propose_agenda(
                target_group, reminder.strip(), priority=0.9,
                not_before_at=not_before, expires_at=not_before + 6 * 3600,
            )
            if item is None:
                raise ValueError("内容无效")
            reply = (f"{at_prefix}已设定北京时间 {at_text} 的群提醒，议程 {item.id}；"
                     "到点后仍会遵守全局发言冷却与日预算。")
        except (TypeError, ValueError):
            reply = (f"{at_prefix}用法：!anima remind at <群号> "
                     "<YYYY-MM-DDTHH:MM(北京时间)> <提醒内容>")
        return reply

    async def _cmd_remind(self, ctx):
        """remind（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        try:
            _, group_text, minutes_text, reminder = raw_cmd.split(" ", 3)
            target_group, minutes = int(group_text), int(minutes_text)
            if target_group <= 0 or not 1 <= minutes <= 10080 or not reminder.strip():
                raise ValueError
            not_before = time.time() + minutes * 60
            # 到点后留 6 小时交给心跳的冷却/预算闸门发送，过期后绝不补发旧提醒。
            item = self.core.propose_agenda(
                target_group, reminder.strip(), priority=0.9,
                not_before_at=not_before, expires_at=not_before + 6 * 3600,
            )
            if item is None:
                raise ValueError("内容无效")
            reply = (f"{at_prefix}已设定 {minutes} 分钟后的群提醒，议程 {item.id}；"
                     "到点后仍会遵守全局发言冷却与日预算。")
        except (TypeError, ValueError):
            reply = f"{at_prefix}用法：!anima remind <群号> <分钟(1-10080)> <提醒内容>"
        return reply

    async def _cmd_skills_list(self, ctx):
        """skills_list / abilities（P2-2：列出技能及其真实可用等级）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        skills = getattr(self.core, "skills", None)
        if skills is None:
            reply = f"{at_prefix}Skills 未启用"
        elif not skills.manifests:
            reply = f"{at_prefix}当前没有已加载的 Skills"
        else:
            rows = skills.describe()
            lines = []
            for row in rows:
                mark = "" if row.get("installed", True) else "（未安装）"
                extra = f" ←{row['composes']}" if row.get("composes") else ""
                lines.append(f"{row['id']}({row['name']})[{row['execution']}]{extra}{mark}")
            reply = (f"{at_prefix}已加载 {len(rows)} 个 Skills："
                     + "、".join(lines)
                     + "\n（方括号是执行方式：prompt=提示词 / http_get=固定网页探查 / tool=组合已有工具；"
                       "箭头后是被组合的工具）")
            if getattr(skills, "errors", None):
                bad = "；".join(f"{str(k).split('/')[-2]}: {v}" for k, v in
                               list(skills.errors.items())[:3])
                reply += f"\n被跳过的技能：{bad}"
        return reply

    async def _cmd_skills_reload(self, ctx):
        """skills_reload（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        skills = getattr(self.core, "skills", None)
        if skills is None:
            reply = f"{at_prefix}Skills 未启用"
        else:
            count = skills.reload()
            suffix = f"；{len(skills.errors)} 个无效文件已跳过" if skills.errors else ""
            reply = f"{at_prefix}Skills 已热重载：{count} 个{suffix}"
        return reply

    async def _cmd_drafts_list(self, ctx):
        """drafts_list（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        drafts = getattr(self.core, "skill_drafts", None)
        items = drafts.pending() if drafts else []
        if not items:
            reply = f"{at_prefix}当前没有待审核的 SkillDraft"
        else:
            rows = [f"{item.id}: {item.manifest.get('name', '未命名')} | {item.request[:80]}"
                    for item in items]
            reply = f"{at_prefix}" + "\n".join(rows[:10])
        return reply

    async def _cmd_draft_create(self, ctx):
        """draft_create（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        drafts = getattr(self.core, "skill_drafts", None)
        try:
            draft = await drafts.create_from_request(raw_cmd[len("draft create "):])
            errors = "；".join(draft.errors) if draft.errors else "静态验证通过"
            reply = f"{at_prefix}已创建草案 {draft.id}（{errors}）。请先 !anima drafts 查看，再用 approve/reject 审核。"
        except (ValueError, RuntimeError, AttributeError) as exc:
            reply = f"{at_prefix}创建 SkillDraft 失败：{str(exc)[:160]}"
        return reply

    async def _cmd_draft_preview(self, ctx):
        """draft_preview（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        drafts = getattr(self.core, "skill_drafts", None)
        try:
            draft = await drafts.preview(raw_cmd[len("draft preview "):].strip())
            result = draft.preview_result or {}
            reply = (f"{at_prefix}预览完成：{result.get('name', '网页')} | "
                     f"{result.get('title', '')} | {result.get('state', '')}。"
                     "确认无误后再使用 !anima draft approve <id>。")
        except (ValueError, RuntimeError, AttributeError) as exc:
            reply = f"{at_prefix}草案预览失败：{str(exc)[:180]}"
        return reply

    async def _cmd_draft_approve(self, ctx):
        """draft_approve（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        drafts = getattr(self.core, "skill_drafts", None)
        try:
            draft = drafts.approve(raw_cmd[len("draft approve "):].strip(), user_id)
            reply = f"{at_prefix}已批准并安装 SkillDraft {draft.id}：{draft.manifest.get('name')}"
        except (ValueError, AttributeError) as exc:
            reply = f"{at_prefix}批准失败：{str(exc)[:180]}"
        return reply

    async def _cmd_draft_reject(self, ctx):
        """draft_reject（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        drafts = getattr(self.core, "skill_drafts", None)
        try:
            draft = drafts.reject(raw_cmd[len("draft reject "):].strip(), user_id)
            reply = f"{at_prefix}已拒绝 SkillDraft {draft.id}"
        except (ValueError, AttributeError) as exc:
            reply = f"{at_prefix}拒绝失败：{str(exc)[:180]}"
        return reply

    async def _cmd_approvals(self, ctx):
        """approvals（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        items = self.core.approvals.pending()
        if not items:
            reply = f"{at_prefix}当前没有待审批的高危工具请求"
        else:
            rows = [f"{item.id}: {item.tool} 参数摘要={item.args_digest}"
                    for item in items]
            reply = f"{at_prefix}" + "\n".join(rows[:10])
        return reply

    async def _cmd_approve(self, ctx):
        """approve（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        try:
            approval_id = raw_cmd[len("approve "):].strip()
            result = await self.core.tools.approve_and_execute(approval_id, user_id)
            reply = f"{at_prefix}已批准并执行 {approval_id}：{result[:300]}"
        except (ValueError, AttributeError) as exc:
            reply = f"{at_prefix}审批执行失败：{str(exc)[:180]}"
        return reply

    async def _cmd_deny(self, ctx):
        """deny（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        try:
            approval_id = raw_cmd[len("deny "):].strip()
            self.core.tools.reject_approval(approval_id, user_id)
            reply = f"{at_prefix}已拒绝高危请求 {approval_id}"
        except (ValueError, AttributeError) as exc:
            reply = f"{at_prefix}拒绝失败：{str(exc)[:180]}"
        return reply

    async def _cmd_subscriptions(self, ctx):
        """subscriptions（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        monitors = getattr(self.core, "monitors", None)
        if monitors is None or not monitors.store.items:
            reply = f"{at_prefix}当前没有直播订阅"
        else:
            rows = [f"{s.id}: {s.platform}/{s.subject_id} -> {','.join(map(str, s.target_groups))} ({'开' if s.enabled else '关'})" for s in monitors.store.items.values()]
            reply = f"{at_prefix}" + "\n".join(rows[:10])
        return reply

    async def _cmd_subscribe_bili(self, ctx):
        """subscribe_bili（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        parts = raw_cmd.split()
        monitors = getattr(self.core, "monitors", None)
        try:
            room_id = parts[2]
            target_group = int(parts[3]) if len(parts) > 3 else int(group_id)
            if not room_id.isdigit() or target_group <= 0:
                raise ValueError
            from anima.monitors import Subscription
            sub = monitors.store.add(Subscription(platform="bilibili_live", subject_id=room_id, target_groups=[target_group]))
            reply = f"{at_prefix}已订阅 B站直播间 {room_id}，开播通知群 {target_group}，id={sub.id}"
        except (ValueError, IndexError, TypeError, AttributeError):
            reply = f"{at_prefix}用法：!anima subscribe bili <直播间号> [目标群号]"
        return reply

    async def _cmd_subscribe_http(self, ctx):
        """subscribe_http（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        parts = raw_cmd.split()
        monitors = getattr(self.core, "monitors", None)
        try:
            url = parts[2]
            target_group = int(parts[3]) if len(parts) > 3 else int(group_id)
            from anima.monitors.http_page import validate_public_url
            from anima.monitors import Subscription
            url = await validate_public_url(url)
            sub = monitors.store.add(Subscription(
                platform="http_page", subject_id=url, target_groups=[target_group],
                poll_interval_sec=300,
                template="本喵监控到 {name} 状态或内容变了：{title}\n{url}",
            ))
            reply = f"{at_prefix}已创建公开网页监控 {sub.id}；首轮只建基线，后续变化才通知群 {target_group}"
        except (ValueError, IndexError, TypeError, AttributeError) as exc:
            reply = f"{at_prefix}创建网页监控失败：{str(exc)[:120]}"
        return reply

    async def _cmd_unsubscribe(self, ctx):
        """unsubscribe（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        sub_id = raw_cmd[len("unsubscribe "):].strip()
        monitors = getattr(self.core, "monitors", None)
        reply = f"{at_prefix}{'已删除订阅' if monitors and monitors.store.remove(sub_id) else '未找到订阅'}：{sub_id}"
        return reply

    async def _cmd_memory_show(self, ctx):
        """memory_show（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        try:
            target_user = int(raw_cmd[len("memory show "):].strip())
            from anima.memory.chat_memory import get_chat_memory
            from anima.crossgroup import get_crossgroup_manager
            short = get_chat_memory().export_user(target_user)
            facts = get_crossgroup_manager().get_user(str(target_user)).facts
            vectors = []
            if getattr(self.core, "vector_memory", None):
                vectors = await self.core.vector_memory.export_by_source_uin(target_user)
            rows = [
                f"用户 {target_user}：短期会话 {len(short)} 份，跨群事实 {len(facts)} 条，向量记忆 {len(vectors)} 条"
            ]
            rows.extend(
                f"F{index}. {fact.content[:120]}（群{fact.source_group}）"
                for index, fact in enumerate(facts[:10], 1)
            )
            rows.extend(
                f"V{item['rowid']}. {str(item.get('text', ''))[:120]}"
                for item in vectors[:10]
            )
            reply = f"{at_prefix}" + "\n".join(rows)
        except (TypeError, ValueError):
            reply = f"{at_prefix}用法：!anima memory show <QQ号>"
        return reply

    async def _cmd_memory_replace(self, ctx):
        """memory_replace（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        try:
            _, _, user_text, index_text, content = raw_cmd.split(" ", 4)
            from anima.crossgroup import get_crossgroup_manager
            ok = get_crossgroup_manager().replace_user_fact(
                str(int(user_text)), int(index_text), content,
            )
            reply = f"{at_prefix}{'已纠正' if ok else '未找到'}跨群事实 F{index_text}"
        except (TypeError, ValueError):
            reply = f"{at_prefix}用法：!anima memory replace <QQ号> <事实序号> <新内容>"
        return reply

    async def _cmd_memory_delete_fact(self, ctx):
        """memory_delete_fact（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        try:
            _, _, _, user_text, index_text = raw_cmd.split(" ", 4)
            from anima.crossgroup import get_crossgroup_manager
            ok = get_crossgroup_manager().delete_user_fact(str(int(user_text)), int(index_text))
            reply = f"{at_prefix}{'已删除' if ok else '未找到'}跨群事实 F{index_text}"
        except (TypeError, ValueError):
            reply = f"{at_prefix}用法：!anima memory delete fact <QQ号> <事实序号>"
        return reply

    async def _cmd_memory_delete_vector(self, ctx):
        """memory_delete_vector（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        try:
            rowid = int(raw_cmd[len("memory delete vector "):].strip())
            if not getattr(self.core, "vector_memory", None):
                raise ValueError
            ok = await self.core.vector_memory.delete(rowid)
            reply = f"{at_prefix}{'已删除' if ok else '未找到'}向量记忆 V{rowid}"
        except (TypeError, ValueError):
            reply = f"{at_prefix}用法：!anima memory delete vector <rowid>"
        return reply

    async def _cmd_memory_clear(self, ctx):
        """memory_clear（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        try:
            target_user = int(raw_cmd[len("memory clear "):].strip())
            from anima.memory.chat_memory import get_chat_memory
            from anima.crossgroup import get_crossgroup_manager
            short_count = get_chat_memory().forget_user(target_user)
            fact_count = get_crossgroup_manager().forget_user(str(target_user))
            vector_count = 0
            if getattr(self.core, "vector_memory", None):
                vector_count = await self.core.vector_memory.delete_by_source_uin(target_user)
            reply = (f"{at_prefix}已清除用户 {target_user} 的记忆：短期 {short_count} 份、"
                     f"跨群事实 {fact_count} 条、向量 {vector_count} 条")
        except (TypeError, ValueError):
            reply = f"{at_prefix}用法：!anima memory clear <QQ号>"
        return reply

    async def _cmd_quality(self, ctx):
        """quality（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        try:
            parts = raw_cmd.split()
            if len(parts) > 2:
                raise ValueError
            day = parts[1] if len(parts) == 2 else None
            if day:
                datetime.strptime(day, "%Y-%m-%d")
            quality = getattr(self.core, "reply_quality", None)
            if quality is None:
                from anima.cognitive.reply_quality import get_reply_quality
                quality = get_reply_quality(self.core.config.persistence.data_dir)
            reply = f"{at_prefix}" + quality.format_report(day)
        except (TypeError, ValueError):
            reply = f"{at_prefix}用法：!anima quality [YYYY-MM-DD]"
        return reply

    async def _cmd_iterate(self, ctx):
        """iterate（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        # B/C 受控迭代：故障工单 + 白名单 core_patch（审批与 apply 分离）
        from anima.cognitive.controlled_iteration import get_controlled_iteration
        it = get_controlled_iteration(self.core.config.persistence.data_dir)
        parts = raw_cmd.split()
        sub = parts[1].lower() if len(parts) >= 2 else "status"
        if sub == "status" or cmd == "iterate status":
            reply = f"{at_prefix}" + it.format_status(self.core)
        elif sub == "auto" and len(parts) >= 3:
            mode = parts[2].lower()
            if mode in {"on", "1", "true", "enable"}:
                it.set_auto_repair_override(True)
                reply = f"{at_prefix}档3自动修复：已开启（运行时覆盖）。" + it.format_status(self.core)
            elif mode in {"off", "0", "false", "disable"}:
                it.set_auto_repair_override(False)
                reply = f"{at_prefix}档3自动修复：已关闭（运行时覆盖）。" + it.format_status(self.core)
            elif mode in {"default", "clear", "reset"}:
                it.set_auto_repair_override(None)
                reply = f"{at_prefix}档3自动修复：已恢复配置默认。" + it.format_status(self.core)
            else:
                reply = f"{at_prefix}用法：!anima iterate auto on|off|default"
        elif sub == "repair":
            # 立即尝试一条自动修复（仍受配额/冷却约束）
            msg = await it.maybe_auto_repair(self.core)
            reply = f"{at_prefix}" + (msg or ("未执行：" + it.format_status(self.core)))
        elif sub == "scan":
            opened = it.scan_quality_and_open_tickets(self.core, force=True)
            if not opened:
                reply = f"{at_prefix}扫描完成：无新故障工单（或信号未达阈值/冷却中）。\n" + it.format_status(self.core)
            else:
                rows = [f"{t.id} {t.request.splitlines()[0][:80]}" for t in opened]
                reply = f"{at_prefix}新开 {len(opened)} 个故障工单：\n" + "\n".join(rows)
        elif sub == "escalate" and len(parts) >= 3:
            try:
                task = it.escalate_to_core_patch(self.core, parts[2].strip(), owner_id=int(user_id))
                files = (task.proposal or {}).get("target_files") or []
                reply = (
                    f"{at_prefix}已升级为 core_patch {task.id}（awaiting_approval）。\n"
                    f"白名单目标：{', '.join(files)}\n"
                    f"审批：WebUI 批准 或 !anima iterate approve {task.id}\n"
                    f"落地：!anima iterate apply {task.id}（不自动重启）"
                )
            except ValueError as exc:
                reply = f"{at_prefix}升级失败：{exc}"
        elif sub == "approve" and len(parts) >= 3:
            try:
                task = it.approve_core_patch(self.core, parts[2].strip(), int(user_id))
                reply = f"{at_prefix}core_patch {task.id} 已批准，状态={task.status}。用 !anima iterate apply {task.id} 落地。"
            except ValueError as exc:
                reply = f"{at_prefix}审批失败：{exc}"
        elif sub == "apply" and len(parts) >= 3:
            try:
                msg = it.apply_core_patch(self.core, parts[2].strip(), int(user_id))
                reply = f"{at_prefix}{msg}"
            except ValueError as exc:
                reply = f"{at_prefix}apply 失败：{exc}"
            except Exception as exc:
                reply = f"{at_prefix}apply 异常：{type(exc).__name__}: {str(exc)[:160]}"
        else:
            reply = (
                f"{at_prefix}用法：!anima iterate status|scan|auto on|off|"
                "repair|escalate <id>|approve <id>|apply <id>"
            )
        return reply

    async def _cmd_digest(self, ctx):
        """digest（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        # Owner 按需周报：只读聚合指标，写入 data/digests，不改配置/不装 SelfCode
        try:
            parts = raw_cmd.split()
            days = 7
            if len(parts) == 2:
                days = int(parts[1])
            elif len(parts) > 2:
                raise ValueError
            from anima.cognitive.weekly_digest import save_digest, format_digest_text
            digest = save_digest(self.core, days=days)
            reply = f"{at_prefix}" + format_digest_text(digest)
        except (TypeError, ValueError):
            reply = f"{at_prefix}用法：!anima digest [天数1-31]"
        return reply

    async def _cmd_selfcode(self, ctx):
        """selfcode（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        # SelfCode 效果：列表 / 评估建议卸载 / 驳回建议 / 禁用 / 删除（删除需 Owner 明确）
        from anima.cognitive.selfcode import get_selfcode_manager
        mgr = get_selfcode_manager(self.core)
        parts = raw_cmd.split()
        sub = parts[1].lower() if len(parts) >= 2 else "list"
        if sub in ("list", "ls", "status"):
            items = mgr.list_installed()
            if not items:
                reply = f"{at_prefix}尚无已安装 SelfCode 工具。"
            else:
                rows = []
                for it in items[:15]:
                    st = it.get("stats") or {}
                    flag = " ⚠建议卸" if st.get("suggest_uninstall") else ""
                    en = "开" if it.get("enabled") else "关"
                    rows.append(
                        f"{it['name']}[{en}] 调{st.get('calls', 0)} "
                        f"成{st.get('ok', 0)}/败{st.get('fail', 0)}{flag}"
                    )
                reply = f"{at_prefix}SelfCode {len(items)} 个：\n" + "\n".join(rows)
        elif sub == "eval" or sub == "evaluate":
            sug = mgr.evaluate_tools()
            if not sug:
                reply = f"{at_prefix}评估完成：暂无建议卸载的工具。"
            else:
                rows = [f"{s['name']}: {s['reason']}" for s in sug[:12]]
                reply = (
                    f"{at_prefix}建议卸载 {len(sug)} 个（不会自动删）：\n"
                    + "\n".join(rows)
                    + f"\n确认删除：!anima selfcode delete <名>\n"
                    f"驳回建议：!anima selfcode keep <名>"
                )
        elif sub == "keep" and len(parts) >= 3:
            name = parts[2].strip()
            mgr.dismiss_uninstall_suggestion(name)
            reply = f"{at_prefix}已驳回对 {name} 的卸载建议。"
        elif sub == "disable" and len(parts) >= 3:
            try:
                r = mgr.set_tool_enabled(parts[2].strip(), False)
                reply = f"{at_prefix}{r.get('message', '已禁用')}"
            except ValueError as exc:
                reply = f"{at_prefix}禁用失败：{exc}"
        elif sub == "enable" and len(parts) >= 3:
            try:
                r = mgr.set_tool_enabled(parts[2].strip(), True)
                reply = f"{at_prefix}{r.get('message', '已启用')}"
            except ValueError as exc:
                reply = f"{at_prefix}启用失败：{exc}"
        elif sub == "delete" and len(parts) >= 3:
            try:
                r = mgr.delete_tool(parts[2].strip())
                reply = f"{at_prefix}已删除 {r.get('name')}（备份 {r.get('backup') or '—'}）"
            except ValueError as exc:
                reply = f"{at_prefix}删除失败：{exc}"
        else:
            reply = (
                f"{at_prefix}用法：!anima selfcode list|eval|"
                "keep <名>|disable <名>|enable <名>|delete <名>"
            )
        return reply

    async def _cmd_drift(self, ctx):
        """drift（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        # 人格漂移状态 / 暂停反刍 / 笔记维护（Owner 主权）
        from anima.cognitive.learning import get_learning
        sl = get_learning(self.core.config.persistence.data_dir)
        parts = raw_cmd.split()
        sub = parts[1].lower() if len(parts) >= 2 else "status"
        if sub in ("status", "stat", "s"):
            st = sl.drift_status()
            paused = "是" if st.get("rumination_paused") else "否"
            lr = st.get("last_reject") or {}
            lr_s = lr.get("reason", "—") if isinstance(lr, dict) else "—"
            reply = (
                f"{at_prefix}漂移 general {st['daily_spent']:.2f}/{st['daily_budget']:.2f} · "
                f"纠错 {st['self_correction_spent']:.2f}/{st['self_correction_daily_budget']:.2f} · "
                f"7日窗 {st['window_spent']:.2f}/{st['window_ceiling']:.2f}\n"
                f"笔记 {st.get('notes_lines', 0)} 行 · 反刍暂停 {paused} · 最近拒因 {lr_s}"
            )
        elif sub == "pause":
            hours = 24.0
            if len(parts) >= 3:
                try:
                    hours = float(parts[2])
                except ValueError:
                    hours = 24.0
            until = sl.pause_rumination(hours)
            import time as _t
            left_h = max(0.0, (until - _t.time()) / 3600.0)
            reply = f"{at_prefix}已暂停反刍约 {left_h:.1f} 小时（不写新自我笔记）。"
        elif sub == "resume":
            was = sl.resume_rumination()
            reply = f"{at_prefix}{'已恢复反刍。' if was else '反刍本来就没暂停。'}"
        elif sub == "notes":
            n = sl.count_note_lines()
            sample = sl.get_notes(400)
            reply = f"{at_prefix}自我笔记 {n} 行。\n{sample or '（空）'}"
        elif sub == "drop":
            n = 1
            if len(parts) >= 3:
                try:
                    n = int(parts[2])
                except ValueError:
                    n = 1
            dropped = sl.delete_last_notes(n)
            reply = f"{at_prefix}已删除最近 {dropped} 条自我笔记。"
        elif sub == "clear":
            dropped = sl.clear_notes()
            reply = f"{at_prefix}已清空自我笔记（原 {dropped} 行）。"
        elif sub == "purge":
            dropped = sl.purge_sticky_notes()
            reply = f"{at_prefix}已清粘滞笔记 {dropped} 行。"
        else:
            reply = (
                f"{at_prefix}用法：!anima drift [status|pause [小时]|resume|"
                "notes|drop [n]|clear|purge]"
            )
        return reply

    async def _cmd_remember(self, ctx):
        """remember（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        # 手动补种/纠偏跨群用户事实（保留原始大小写）
        fact_text = raw_cmd[len("remember "):].strip()
        if not fact_text:
            reply = f"{at_prefix}用法：!anima remember <事实>"
        else:
            from anima.crossgroup import get_crossgroup_manager
            cgm = get_crossgroup_manager()
            from anima.util.name_clean import sender_display_name
            # 群名片优先于 QQ 昵称（同 handler：跨群事实要记在"群里怎么叫他"名下）
            sender_name = sender_display_name(event.get("raw", {}).get("sender"), user_id)
            src = str(group_id) if detail_type == "group" else "private"
            # P1-2：手动补种/纠偏是 Owner 显式声明的事实，给满置信度 1.0，
            # 绝不参与夜间衰减淘汰（private 源本就在保护名单；群内手动种也应高于 LLM 猜测的 0.5）。
            f = cgm.add_user_fact(str(user_id), fact_text, source_group=src,
                                  name=sender_name, confidence=1.0)
            reply = (f"{at_prefix}已记住：{fact_text}" if f
                     else f"{at_prefix}这条已经记过了：{fact_text}")
        return reply

    async def _cmd_stickers(self, ctx):
        """stickers（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        # 表情库状态（空库时暴露诊断，定位取图失败）
        try:
            from anima.cognitive.stickers import get_sticker_manager
            mgr = get_sticker_manager(self.core.config.persistence.data_dir)
            n = mgr.count()
            if n == 0 and hasattr(mgr, "stats"):
                st = mgr.stats()
                reply = (f"{at_prefix}表情库为空（上限 {self.core.config.sticker.max_stickers}）。"
                         f"取图失败累计 {st.get('fetch_fail_total', 0)} 次；"
                         f"若群里有图却一直空，多为 NapCat 取图失败——"
                         f"检查 resolver/get_image GUID 解析是否可用。")
            else:
                reply = f"{at_prefix}表情库共 {n} 张（上限 {self.core.config.sticker.max_stickers}）"
        except Exception:
            reply = f"{at_prefix}表情库状态查询失败"
        return reply

    async def _cmd_clear_stickers(self, ctx):
        """clear_stickers（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        # 清空表情库（隐私/重置用）
        try:
            from anima.cognitive.stickers import get_sticker_manager
            n = get_sticker_manager(self.core.config.persistence.data_dir).clear()
            reply = f"{at_prefix}已清空表情库（删除 {n} 张）"
        except Exception:
            reply = f"{at_prefix}清空失败"
        return reply

    async def _cmd_stickers_pending(self, ctx):
        """stickers_pending（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        from anima.cognitive.stickers import get_sticker_manager
        items = get_sticker_manager(self.core.config.persistence.data_dir).pending()
        reply = (f"{at_prefix}没有待审核表情" if not items else
                 f"{at_prefix}" + "\n".join(f"{h[:12]} 来源群{m.get('src_group')} 标签{m.get('labels', [])}" for h, m in items[:10]))
        return reply

    async def _cmd_sticker_review(self, ctx):
        """sticker_review（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        from anima.cognitive.stickers import get_sticker_manager
        approve = raw_cmd.startswith("sticker approve ")
        sticker_hash = raw_cmd.split()[-1].strip()
        manager = get_sticker_manager(self.core.config.persistence.data_dir)
        ok = manager.approve(sticker_hash) if approve else manager.reject(sticker_hash)
        reply = f"{at_prefix}{'已批准' if approve else '已拒绝'}" if ok else f"{at_prefix}未找到待审核表情"
        return reply

    async def _cmd_sticker_reclassify(self, ctx):
        """sticker_reclassify（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        from anima.cognitive.stickers import get_sticker_manager
        mgr = get_sticker_manager(self.core.config.persistence.data_dir)
        if not getattr(self.core.config.sticker, "classify_at_collect", True):
            reply = f"{at_prefix}分类闸门已关闭（classify_at_collect=false），无法重新分类"
        else:
            n = mgr.count()
            asyncio.create_task(self._reclassify_stickers(mgr, detail_type, target_id))
            reply = (f"{at_prefix}开始用 LLM 重新分类现有 {n} 张表情库"
                     f"（非表情包将被剔除），完成后汇报结果~")
        return reply

    async def _cmd_meme(self, ctx):
        """meme（原样迁移）。"""
        cmd, raw_cmd = ctx.cmd, ctx.raw_cmd
        at_prefix, target_id = ctx.at_prefix, ctx.target_id
        user_id, group_id, detail_type, event = (
            ctx.user_id, ctx.group_id, ctx.detail_type, ctx.event)
        from anima.cognitive.meme_commands import handle_meme_command
        rep = handle_meme_command(raw_cmd, at_prefix, self.core)
        if rep is None:
            return None
        reply = rep
        return reply

    async def _cmd_os(self, ctx):
        """!anima os <自然语言> —— 受限 NL 指令解析（P3-3）。

        把 R14 控制面的**确定性**解析器接到 QQ 主信道（此前它只服务 WebUI
        `/api/os/command`，QQ 用户拿不到）。三条边界，全部 fail-closed：

        1. **只解析、不猜测**：解析器不认识的输入返回 unknown + 用法提示，绝不
           「大概是要查健康吧」地执行一个动作；
        2. **特权意图只产出待确认**：批准/拒绝提案返回需确认的动作与**既有** Owner
           授权端点，QQ 侧不执行——self-improvement ≠ self-authorization；
        3. **只跑只读白名单**：会落 SkillDraft（skill）或触发多轮 LLM（society）的
           意图在 QQ 侧不执行，只提示去 WebUI 控制面——QQ 是低带宽信道，不该在聊天
           里产生未预期的写入与成本。
        """
        text = ctx.raw_cmd[2:].strip()  # 去掉 "os"
        if not text:
            return ("用法：!anima os <自然语言>\n"
                    "例：查看健康｜近 30 天稳不稳｜观察环境趋势｜演化影响评分｜"
                    "依赖自检｜列出演化提案｜你还记得我的习惯吗")

        from anima.os.control_surface import HumanAIControlSurface, format_readonly_reply

        surf = HumanAIControlSurface(self.core)
        cmd = surf.parse_command(text)

        if cmd.needs_confirmation:
            return (f"⚠️ 「{text[:60]}」是特权动作，需要你确认。\n"
                    f"{cmd.preview}\n"
                    f"请走既有 Owner 授权端点：{cmd.confirmation_endpoint}\n"
                    f"（QQ 侧只解析、不批准：self-improvement ≠ self-authorization）")
        if cmd.intent == "unknown":
            return (f"没识别出这条指令要做什么：「{text[:60]}」\n"
                    f"可用：查看健康｜近 30 天稳不稳｜观察环境趋势｜演化影响评分｜"
                    f"依赖自检｜列出演化提案｜你还记得我的习惯吗")
        if cmd.intent not in HumanAIControlSurface.READONLY_INTENTS:
            return (f"「{cmd.preview}」不是只读查询（intent={cmd.intent}），QQ 侧不执行；"
                    f"请用 WebUI 控制面（/api/os/command）。")

        data = await surf.run_readonly(cmd, text)
        if not data:
            return f"{cmd.preview}\n（本次没有取到只读数据——如实报告，未编造）"
        return format_readonly_reply(cmd, data)


# 路由表：谓词 = 原条件表达式；顺序 = 原 if/elif 链顺序（不得重排）
COMMAND_ROUTES = [
    (lambda c: c.cmd == "pause", '_cmd_pause'),  # cmd == "pause"
    (lambda c: c.cmd == "resume", '_cmd_resume'),  # cmd == "resume"
    (lambda c: c.cmd == "status", '_cmd_status'),  # cmd == "status"
    (lambda c: c.raw_cmd.startswith("ignore "), '_cmd_ignore'),  # raw_cmd.startswith("ignore ")
    (lambda c: c.raw_cmd.startswith("unignore "), '_cmd_unignore'),  # raw_cmd.startswith("unignore ")
    (lambda c: c.cmd in ("ignore", "ignore list"), '_cmd_ignore_list'),  # cmd in ("ignore", "ignore list")
    (lambda c: c.cmd in ("tasks", "task list"), '_cmd_tasks'),  # cmd in ("tasks", "task list")
    (lambda c: c.raw_cmd.startswith("task create "), '_cmd_task_create'),  # raw_cmd.startswith("task create ")
    (lambda c: c.raw_cmd.startswith("task approve "), '_cmd_task_approve'),  # raw_cmd.startswith("task approve ")
    (lambda c: c.raw_cmd.startswith("task cancel "), '_cmd_task_cancel'),  # raw_cmd.startswith("task cancel ")
    (lambda c: c.raw_cmd.startswith("agent "), '_cmd_agent'),  # raw_cmd.startswith("agent ")
    (lambda c: c.cmd == "agenda" or c.cmd == "agenda list", '_cmd_agenda_list'),  # cmd == "agenda" or cmd == "agenda list"
    (lambda c: c.cmd == "calendar" or c.cmd == "calendar list", '_cmd_calendar_list'),  # cmd == "calendar" or cmd == "calendar list"
    (lambda c: c.raw_cmd.startswith("calendar daily "), '_cmd_calendar_daily'),  # raw_cmd.startswith("calendar daily ")
    (lambda c: c.raw_cmd.startswith("calendar weekly "), '_cmd_calendar_weekly'),  # raw_cmd.startswith("calendar weekly ")
    (lambda c: c.raw_cmd.startswith("calendar cancel "), '_cmd_calendar_cancel'),  # raw_cmd.startswith("calendar cancel ")
    (lambda c: c.raw_cmd.startswith("agenda add "), '_cmd_agenda_add'),  # raw_cmd.startswith("agenda add ")
    (lambda c: c.raw_cmd.startswith("agenda cancel "), '_cmd_agenda_cancel'),  # raw_cmd.startswith("agenda cancel ")
    (lambda c: c.raw_cmd.startswith("remind at "), '_cmd_remind_at'),  # raw_cmd.startswith("remind at ")
    (lambda c: c.raw_cmd.startswith("remind "), '_cmd_remind'),  # raw_cmd.startswith("remind ")
    (lambda c: c.cmd in ("skills", "skills list", "abilities", "abilities list"), '_cmd_skills_list'),  # P2-2: abilities 为别名
    (lambda c: c.cmd == "skills reload", '_cmd_skills_reload'),  # cmd == "skills reload"
    (lambda c: c.cmd in ("drafts", "drafts list"), '_cmd_drafts_list'),  # cmd in ("drafts", "drafts list")
    (lambda c: c.raw_cmd.startswith("draft create "), '_cmd_draft_create'),  # raw_cmd.startswith("draft create ")
    (lambda c: c.raw_cmd.startswith("draft preview "), '_cmd_draft_preview'),  # raw_cmd.startswith("draft preview ")
    (lambda c: c.raw_cmd.startswith("draft approve "), '_cmd_draft_approve'),  # raw_cmd.startswith("draft approve ")
    (lambda c: c.raw_cmd.startswith("draft reject "), '_cmd_draft_reject'),  # raw_cmd.startswith("draft reject ")
    (lambda c: c.cmd in ("approvals", "approvals list"), '_cmd_approvals'),  # cmd in ("approvals", "approvals list")
    (lambda c: c.raw_cmd.startswith("approve "), '_cmd_approve'),  # raw_cmd.startswith("approve ")
    (lambda c: c.raw_cmd.startswith("deny "), '_cmd_deny'),  # raw_cmd.startswith("deny ")
    (lambda c: c.cmd == "subscriptions" or c.cmd == "subscriptions list", '_cmd_subscriptions'),  # cmd == "subscriptions" or cmd == "subscriptions list"
    (lambda c: c.raw_cmd.startswith("subscribe bili "), '_cmd_subscribe_bili'),  # raw_cmd.startswith("subscribe bili ")
    (lambda c: c.raw_cmd.startswith("subscribe http "), '_cmd_subscribe_http'),  # raw_cmd.startswith("subscribe http ")
    (lambda c: c.raw_cmd.startswith("unsubscribe "), '_cmd_unsubscribe'),  # raw_cmd.startswith("unsubscribe ")
    (lambda c: c.raw_cmd.startswith("memory show "), '_cmd_memory_show'),  # raw_cmd.startswith("memory show ")
    (lambda c: c.raw_cmd.startswith("memory replace "), '_cmd_memory_replace'),  # raw_cmd.startswith("memory replace ")
    (lambda c: c.raw_cmd.startswith("memory delete fact "), '_cmd_memory_delete_fact'),  # raw_cmd.startswith("memory delete fact ")
    (lambda c: c.raw_cmd.startswith("memory delete vector "), '_cmd_memory_delete_vector'),  # raw_cmd.startswith("memory delete vector ")
    (lambda c: c.raw_cmd.startswith("memory clear "), '_cmd_memory_clear'),  # raw_cmd.startswith("memory clear ")
    (lambda c: c.cmd == "quality" or c.raw_cmd.startswith("quality "), '_cmd_quality'),  # cmd == "quality" or raw_cmd.startswith("quality ")
    (lambda c: c.cmd in ("iterate", "iterate status") or c.raw_cmd.startswith("iterate "), '_cmd_iterate'),  # cmd in ("iterate", "iterate status") or raw_cmd.startswith("iterate ")
    (lambda c: c.cmd == "digest" or c.raw_cmd.startswith("digest "), '_cmd_digest'),  # cmd == "digest" or raw_cmd.startswith("digest ")
    (lambda c: c.cmd == "selfcode" or c.cmd.startswith("selfcode "), '_cmd_selfcode'),  # cmd == "selfcode" or cmd.startswith("selfcode ")
    (lambda c: c.cmd == "drift" or c.cmd.startswith("drift "), '_cmd_drift'),  # cmd == "drift" or cmd.startswith("drift ")
    (lambda c: c.cmd.startswith("remember "), '_cmd_remember'),  # cmd.startswith("remember ")
    (lambda c: c.cmd == "stickers", '_cmd_stickers'),  # cmd == "stickers"
    (lambda c: c.cmd == "clear_stickers", '_cmd_clear_stickers'),  # cmd == "clear_stickers"
    (lambda c: c.cmd in ("stickers pending", "sticker pending"), '_cmd_stickers_pending'),  # cmd in ("stickers pending", "sticker pending")
    (lambda c: c.raw_cmd.startswith("sticker approve ") or c.raw_cmd.startswith("sticker reject "), '_cmd_sticker_review'),  # raw_cmd.startswith("sticker approve ") or raw_cmd.startswith("sticker reject ")
    (lambda c: c.raw_cmd.strip() == "sticker reclassify", '_cmd_sticker_reclassify'),  # raw_cmd.strip() == "sticker reclassify"
    (lambda c: c.raw_cmd.startswith("meme"), '_cmd_meme'),  # raw_cmd.startswith("meme")
    (lambda c: c.raw_cmd == "os" or c.raw_cmd.startswith("os "), '_cmd_os'),  # P3-3: 受限 NL OS 指令
]


async def dispatch_anima_command(handler, event: dict, text: str, user_id: int,
                                group_id: Optional[int], detail_type: str) -> bool:
    """!anima 命令总入口：门控 → 自助分支 → owner 校验 → 路由表。

    返回 True 表示已消费该消息。
    """
    if not text.startswith("!anima "):
        return False
    raw_cmd = text[len("!anima "):].strip()
    if raw_cmd.lower() in {"forget me", "delete my data"}:
        return await handler._cmd_forget_me(user_id, group_id, detail_type)
    if raw_cmd.lower() in {"export me", "export my data"}:
        return await handler._cmd_export_me(user_id)
    if not handler._is_owner(user_id):
        await handler._cmd_reject_non_owner(user_id, group_id, detail_type)
        return True

    ctx = CommandCtx(
        event=event,
        raw_cmd=raw_cmd,
        cmd=raw_cmd.lower(),
        user_id=user_id,
        group_id=group_id,
        detail_type=detail_type,
        target_id=group_id if detail_type == "group" else user_id,
        at_prefix="",
    )
    for pred, method_name in COMMAND_ROUTES:
        if not pred(ctx):
            continue
        reply = await getattr(handler, method_name)(ctx)
        if not reply:
            return False
        if detail_type == "group" and method_name in _PRIVATE_ONLY_ROUTES:
            # 控制面回执不进群（见 `_PRIVATE_ONLY_ROUTES` 注释）。
            await handler.core.gateway.send(
                OutboundMsg("private", int(user_id), reply))
            return True
        await handler.core.gateway.send(OutboundMsg(detail_type, ctx.target_id, reply))
        return True
    return False
