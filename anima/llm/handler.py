"""LLM 对话处理器 —— 通过 Pipeline 处理消息。"""

from __future__ import annotations
import asyncio
import base64
from datetime import datetime, timedelta, timezone
import json
import logging
import os
import random
import re
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from anima.core.gateway import OutboundMsg
from anima.pipeline import Pipeline, PipelineContext
# 群消息门控逻辑已拆分到 group_gate.py，此处重导出保持向后兼容
from anima.llm.group_gate import (  # noqa: F401
    BOT_DISPLAY_NAME,
    is_direct_group_call,
    resolve_group_reply_mode,
    resolve_other_bot_names,
    is_addressed_to_other_bot,
    is_familiar_idle_unaddressed,
    classify_group_reply_skip,
    should_reply_in_group,
)
from anima.util.name_clean import clean_display_name

if TYPE_CHECKING:
    from anima.core.anima_core import AnimaCore

logger = logging.getLogger("anima.llm.handler")


def _onebot_message_sender_id(message: dict | None) -> Any:
    """兼容不同 NapCat/get_msg 返回形状，提取被引用消息发送者 QQ。"""
    if not isinstance(message, dict):
        return None
    sender = message.get("sender") or {}
    return (
        message.get("user_id")
        or message.get("sender_id")
        or (sender.get("user_id") if isinstance(sender, dict) else None)
    )

_TEXT_ATTACHMENT_SUFFIXES = {".txt", ".md", ".json", ".csv", ".yaml", ".yml", ".py", ".log"}
_DOCUMENT_ATTACHMENT_SUFFIXES = {".pdf", ".docx", ".xlsx"}
_MAX_ATTACHMENT_BYTES = 2 * 1024 * 1024


def _parse_beijing_reminder_time(value: str) -> float:
    """只接受无歧义的北京时间 ISO 分钟格式，避免自然语言日期误触发。"""
    parsed = datetime.strptime(value.strip(), "%Y-%m-%dT%H:%M")
    return parsed.replace(tzinfo=timezone(timedelta(hours=8))).timestamp()


class LLMHandler:
    """对话处理器：构建 Pipeline，逐阶段处理消息。"""

    def __init__(self, core: "AnimaCore"):
        self.core = core
        self.pipeline = self._build_pipeline()
        self._sticker_last: dict = {}  # user_id -> 上次收集时间（节流）
        self._sticker_auto_last: dict = {}  # group_id -> 上次被动自动带图时间（冷却）
        self._audio_last: dict[int, float] = {}
        self._audio_metrics: dict[str, Any] = {
            "requests": 0, "successes": 0, "failures": 0,
            "throttled": 0, "unavailable": 0,
            "last_success_at": 0.0, "last_failure_at": 0.0,
        }
        # 同一会话的模型回合必须串行：连续文本/图片/引用在收束窗口之外到达时，
        # 后一回合也要等前一回合完成记忆写入，不能并行抢写上下文。
        self._turn_locks: dict[tuple[str, int | None, int], asyncio.Lock] = {}

        self._assembler = None
        conversation = core.config.conversation
        if conversation.enabled:
            from anima.core.conversation_assembler import ConversationAssembler
            self._assembler = ConversationAssembler(
                wait_sec=conversation.wait_sec,
                max_wait_sec=conversation.max_wait_sec,
            )

        # B1: 群聊防抖
        self._group_debouncer = None
        debounce_cfg = getattr(core.config, "debounce", None)
        if debounce_cfg and getattr(debounce_cfg, "group_enabled", True):
            from anima.core.conversation_assembler import GroupDebouncer
            self._group_debouncer = GroupDebouncer(
                wait_sec=getattr(debounce_cfg, "group_wait_sec", 2.0),
                max_wait_sec=getattr(debounce_cfg, "group_max_wait_sec", 6.0),
                adaptive=getattr(debounce_cfg, "adaptive", True),
                short_msg_threshold=getattr(debounce_cfg, "short_msg_threshold", 10),
            )

    def _record_audio_metric(self, name: str) -> None:
        metrics = getattr(self, "_audio_metrics", None)
        if not isinstance(metrics, dict):
            metrics = self._audio_metrics = {
                "requests": 0, "successes": 0, "failures": 0,
                "throttled": 0, "unavailable": 0,
                "last_success_at": 0.0, "last_failure_at": 0.0,
            }
        metrics[name] = int(metrics.get(name, 0) or 0) + 1
        if name == "successes":
            metrics["last_success_at"] = time.time()
        elif name == "failures":
            metrics["last_failure_at"] = time.time()

    def audio_health(self) -> dict[str, Any]:
        metrics = dict(getattr(self, "_audio_metrics", {}) or {})
        requests = int(metrics.get("requests", 0) or 0)
        successes = int(metrics.get("successes", 0) or 0)
        return {
            "enabled": bool(getattr(getattr(self.core.config, "audio", None), "transcription_enabled", False)),
            "requests": requests,
            "successes": successes,
            "failures": int(metrics.get("failures", 0) or 0),
            "throttled": int(metrics.get("throttled", 0) or 0),
            "unavailable": int(metrics.get("unavailable", 0) or 0),
            "success_rate": successes / requests if requests else 0.0,
            "last_success_at": metrics.get("last_success_at", 0.0) or None,
            "last_failure_at": metrics.get("last_failure_at", 0.0) or None,
        }

    def _build_pipeline(self) -> Pipeline:
        from anima.pipeline.stages import (
            permission_stage,
            memory_stage,
            cognition_stage,
            generation_stage,
            guardrail_stage,
        )
        pipe = (Pipeline()
                .use(permission_stage)
                .use(memory_stage)
                .use(cognition_stage)
                .use(generation_stage)
                .use(guardrail_stage))

        # 插件 LLM hook（AstrBot 兼容层）
        if self.core.config.compat.astrbot_enabled:
            try:
                from anima.compat.astrbot import _build_llm_hook_stage
                hook_stage = _build_llm_hook_stage(self.core)
                # 在 cognition 和 generation 之间插入，允许插件修改提示词
                pipe = (Pipeline()
                        .use(permission_stage)
                        .use(memory_stage)
                        .use(cognition_stage)
                        .use(hook_stage)
                        .use(generation_stage)
                        .use(guardrail_stage))
            except Exception:
                logger.exception("AstrBot 兼容层 hook 阶段初始化失败")

        return pipe

    async def close(self) -> None:
        """停止尚在等待的私聊收束任务，供核心生命周期对称关闭。"""
        if self._assembler is not None:
            await self._assembler.close()

    def _is_owner(self, user_id: int) -> bool:
        # 防御式访问：测试桩 core 可能没有 permissions 配置段。
        perms = getattr(self.core.config, "permissions", None)
        owners = getattr(perms, "owners", None) or ()
        return user_id in owners

    def _ignore_store(self):
        """懒加载忽略名单存储（data/ignore_list.json）。"""
        s = getattr(self, "_ignore_store_obj", None)
        if s is None:
            dd = getattr(getattr(self.core.config, "persistence", None), "data_dir", "data")
            from anima.llm.ignore_list import IgnoreStore
            s = self._ignore_store_obj = IgnoreStore(dd or "data")
        return s

    def _track_group_speaker(self, gid: int, uid: int) -> None:
        """记录该群最近发言的非 owner 用户（供“别理那两人”解析目标）。"""
        spk = getattr(self, "_recent_spk", None)
        if spk is None:
            spk = self._recent_spk = {}
        dq = spk.setdefault(str(gid), {})
        dq[str(int(uid))] = time.time()
        if len(dq) > 20:
            for k in sorted(dq, key=dq.get)[: len(dq) - 20]:
                dq.pop(k, None)

    def _maybe_apply_ignore_directive(self, text: str, gid: int, uid: int) -> None:
        """识别 owner 的“别再理那两人/别理某人”并持久化到忽略名单。"""
        from anima.llm.ignore_list import matches_ignore_directive, resolve_ignore_targets
        if not matches_ignore_directive(text):
            return
        spk = (getattr(self, "_recent_spk", {}) or {}).get(str(gid), {})
        recent = [int(u) for u, _ in sorted(spk.items(), key=lambda kv: kv[1], reverse=True)]
        targets = resolve_ignore_targets(text, recent)
        if not targets:
            return
        got = self._ignore_store().add(str(gid), targets)
        logger.info("[ignore] owner=%s 于群 %s 持久化忽略 %s（共 %d）", uid, gid, targets, got)

    # ── 唤醒会话窗：被 keyword「哈基米」/ @ / 引用唤醒后，窗内可自然接续 ──
    WAKE_SESSION_TTL = 240.0  # 秒；可经 autonomy.wake_session_ttl_sec 覆盖

    def _wake_session_ttl(self) -> float:
        try:
            v = getattr(self.core.config.autonomy, "wake_session_ttl_sec", None)
            if v is not None:
                return float(v)
        except Exception:
            pass
        return float(self.WAKE_SESSION_TTL)

    def _mark_wake(self, gid: int) -> None:
        try:
            s = getattr(self, "_wake_sessions", None)
            if s is None:
                s = self._wake_sessions = {}
            s[str(int(gid))] = time.time() + self._wake_session_ttl()
        except Exception:
            logger.debug("mark wake failed", exc_info=True)

    def _wake_session_active(self, gid: Any) -> bool:
        try:
            s = getattr(self, "_wake_sessions", None)
            if not s:
                return False
            return bool(s.get(str(int(gid)))) and time.time() < s[str(int(gid))]
        except Exception:
            return False

    def _recent_speaker_uids(self, gid: Any) -> list[int]:
        spk = (getattr(self, "_recent_spk", {}) or {}).get(str(gid), {})
        return [int(u) for u, _ in sorted(spk.items(), key=lambda kv: kv[1], reverse=True)]

    def _request_executor(self):
        """通用口头发话执行器（记住/提醒/别再理/查询/代办）。"""
        ex = getattr(self, "_request_executor_obj", None)
        if ex is None:
            dd = getattr(getattr(self.core.config, "persistence", None), "data_dir", "data")
            from anima.llm.request_executor import RequestExecutor
            from anima.llm.ignore_list import IgnoreStore
            ex = self._request_executor_obj = RequestExecutor(
                self.core, data_dir=dd or "data",
                recent_speakers=lambda gid: self._recent_speaker_uids(gid),
                ignore_store=IgnoreStore(dd or "data"),
            )
        return ex

    async def _try_verbal_request(self, text, user_id, group_id, detail_type, event) -> str | None:
        """owner 或被@ 的一句话要求 → 就地办好并回执；否则返回 None 走正常回复。"""
        try:
            if not (text or "").strip():
                return None
            is_owner = self._is_owner(int(user_id))
            if detail_type == "private":
                if not is_owner:
                    return None
            else:
                from anima.llm.group_gate import is_direct_group_call
                if not (is_owner or is_direct_group_call(event, text)):
                    return None
            return await self._request_executor().handle(
                text, user_id=int(user_id),
                group_id=int(group_id) if group_id is not None else None,
                detail_type=detail_type,
            )
        except Exception:
            logger.exception("口头发话执行异常（已隔离）")
            return None

    def _looks_like_owner_task(self, text: str, user_id: int, detail_type: str) -> bool:
        if detail_type != "private" or not self._is_owner(user_id):
            return False
        text = (text or "").strip()
        action = any(word in text for word in ("修复", "解决", "调试", "排查", "检查并改"))
        scope = any(word in text for word in ("bot", "项目", "代码", "日志", "服务", "VPS", "WebUI", "问题"))
        return action and scope and len(text) >= 8

    async def _create_owner_task(self, text: str, user_id: int) -> str:
        plan = ["收集日志、配置和相关代码证据", "给出最小修复方案并等待修改确认", "修改后运行测试", "部署/重启前等待部署确认", "验证结果并记录任务结论"]
        task = self.core.tasks.create(text, user_id, plan)
        return f"已创建 Owner 任务 {task.id}。请回复「!anima task approve {task.id}」开始诊断；修改、重启和部署会单独请求审批。"

    def _turn_lock(self, detail_type: str, group_id: Any, user_id: Any) -> asyncio.Lock:
        """返回按（私聊用户）或（群、用户）隔离的回合锁。"""
        key = (str(detail_type), int(group_id) if group_id is not None else None, int(user_id))
        # 兼容测试桩或旧扩展用 object.__new__ 构造 Handler 的场景。
        locks = getattr(self, "_turn_locks", None)
        if locks is None:
            locks = self._turn_locks = {}
        lock = locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            locks[key] = lock
        return lock

    async def _handle_command(self, event: dict, text: str, user_id: int,
                              group_id: Optional[int], detail_type: str) -> bool:
        """处理 owner 命令。返回 True 表示已消费该消息。"""
        if not text.startswith("!anima "):
            return False
        raw_cmd = text[len("!anima "):].strip()
        if raw_cmd.lower() in {"forget me", "delete my data"}:
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
        if raw_cmd.lower() in {"export me", "export my data"}:
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
        if not self._is_owner(user_id):
            return False

        cmd = raw_cmd.lower()
        target_id = group_id if detail_type == "group" else user_id
        at_prefix = f"[CQ:at,qq={user_id}] " if detail_type == "group" else ""

        if cmd == "pause":
            self.core.config.autonomy.enabled = False
            if self.core.heartbeat:
                await self.core.heartbeat.stop()
            reply = f"{at_prefix}已暂停自主心跳。"
        elif cmd == "resume":
            self.core.config.autonomy.enabled = True
            if self.core.heartbeat and not self.core.heartbeat._running:
                await self.core.heartbeat.start()
            reply = f"{at_prefix}已恢复自主心跳。"
        elif cmd == "status":
            hb = self.core.heartbeat
            status = "运行中" if (hb and hb._running) else "已停止"
            reply = f"{at_prefix}自主心跳: {status} | 自主开关: {self.core.config.autonomy.enabled}"
        elif raw_cmd.startswith("ignore "):
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
        elif raw_cmd.startswith("unignore "):
            scope = str(group_id) if detail_type == "group" else "global"
            tokens = [t for t in raw_cmd[len("unignore "):].replace(",", " ").replace("，", " ").split() if t.strip().isdigit()]
            removed = [t for t in tokens if self._ignore_store().remove(scope, int(t))]
            reply = f"{at_prefix}已移除忽略 {removed}。" if removed else f"{at_prefix}没有需要移除的（可用 !anima ignore list 查看）。"
        elif cmd in ("ignore", "ignore list"):
            scope = str(group_id) if detail_type == "group" else "global"
            lst = self._ignore_store().list(scope)
            reply = f"{at_prefix}忽略名单: " + (", ".join(lst) if lst else "空")
        elif cmd in ("tasks", "task list"):
            rows = [f"{x.id} [{x.status}] {x.request[:70]}" for x in self.core.tasks.list()[:10]]
            reply = f"{at_prefix}" + ("\n".join(rows) if rows else "暂无 Owner 任务")
        elif raw_cmd.startswith("task create "):
            request_text = raw_cmd[len("task create "):].strip()
            if not request_text:
                reply = f"{at_prefix}用法：!anima task create <目标>"
            else:
                plan = ["收集日志、配置和相关代码证据", "给出最小修复方案并等待修改确认", "修改后运行测试", "部署/重启前等待部署确认", "验证结果并记录任务结论"]
                task = self.core.tasks.create(request_text, user_id, plan)
                reply = f"{at_prefix}已创建任务 {task.id}（planned）。\n" + "\n".join(f"{i+1}. {v}" for i,v in enumerate(plan)) + f"\n用 !anima task approve {task.id} 进入确认队列。"
        elif raw_cmd.startswith("task approve "):
            task_id = raw_cmd[len("task approve "):].strip()
            try:
                task = self.core.tasks.get(task_id)
                if task is None:
                    raise ValueError("未找到任务")
                result = await self.core.task_agent.execute(task)
                reply = f"{at_prefix}任务 {task.id} 已执行：\n{result[:1800]}"
            except ValueError:
                reply = f"{at_prefix}未找到任务"
        elif raw_cmd.startswith("task cancel "):
            try:
                task = self.core.tasks.set_status(raw_cmd[len("task cancel "):].strip(), "cancelled", "Owner 取消")
                reply = f"{at_prefix}任务 {task.id} 已取消"
            except ValueError:
                reply = f"{at_prefix}未找到任务"
        elif raw_cmd.startswith("agent "):
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
                task = await self.core.agent.submit(sub, int(user_id), auto_run=True)
                reply = f"{at_prefix}已创建 Agent 任务 {task.id}，开始执行（进度将推送）。"
            else:
                reply = (f"{at_prefix}用法：!anima agent <请求> | "
                         f"!anima agent approve <id> | !anima agent cancel <id> | !anima agent list")
        elif cmd == "agenda" or cmd == "agenda list":
            items = self.core.agenda.pending()
            if not items:
                reply = f"{at_prefix}当前没有待执行的议程"
            else:
                rows = [f"{item.id}: 群{item.group_id} p={item.priority:.1f} {item.text_intent}" for item in items]
                reply = f"{at_prefix}" + "\n".join(rows[:10])
        elif cmd == "calendar" or cmd == "calendar list":
            items = [*self.core.calendar.items, *self.core.calendar.weekly_items]
            items = [item for item in items if item.enabled]
            if not items:
                reply = f"{at_prefix}当前没有每日历事件"
            else:
                rows = [f"{item.id}: {'每周'+str(item.weekday) if hasattr(item, 'weekday') else '每天'} "
                        f"{item.hour:02d}:{item.minute:02d} 群{item.group_id} {item.text}" for item in items]
                reply = f"{at_prefix}" + "\n".join(rows[:10])
        elif raw_cmd.startswith("calendar daily "):
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
        elif raw_cmd.startswith("calendar weekly "):
            try:
                _, _, group_text, weekday, hhmm, reminder = raw_cmd.split(" ", 5)
                item = self.core.calendar.add_weekly(int(group_text), int(weekday), hhmm, reminder)
                reply = (f"{at_prefix}已添加每周日历事件 {item.id}：周{item.weekday} "
                         f"{item.hour:02d}:{item.minute:02d} 提醒群 {item.group_id}。")
            except (TypeError, ValueError):
                reply = f"{at_prefix}用法：!anima calendar weekly <群号> <星期1-7> <HH:MM> <提醒内容>"
        elif raw_cmd.startswith("calendar cancel "):
            event_id = raw_cmd[len("calendar cancel "):].strip()
            reply = f"{at_prefix}{'已取消日历事件' if self.core.calendar.cancel(event_id) else '未找到日历事件'}：{event_id}"
        elif raw_cmd.startswith("agenda add "):
            try:
                _, _, group_text, intent = raw_cmd.split(" ", 3)
                item = self.core.propose_agenda(int(group_text), intent)
                if item is None:
                    raise ValueError("内容无效")
                reply = f"{at_prefix}已加入议程 {item.id}，会受主动发言频率与预算限制"
            except (TypeError, ValueError):
                reply = f"{at_prefix}用法：!anima agenda add <群号> <表达意图>"
        elif raw_cmd.startswith("agenda cancel "):
            agenda_id = raw_cmd[len("agenda cancel "):].strip()
            reply = f"{at_prefix}{'已取消议程' if self.core.agenda.cancel(agenda_id) else '未找到待执行议程'}：{agenda_id}"
        elif raw_cmd.startswith("remind at "):
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
        elif raw_cmd.startswith("remind "):
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
        elif cmd in ("skills", "skills list"):
            skills = getattr(self.core, "skills", None)
            if skills is None:
                reply = f"{at_prefix}Skills 未启用"
            elif not skills.manifests:
                reply = f"{at_prefix}当前没有已加载的 Skills"
            else:
                names = "、".join(f"{m.id}({m.name})" for m in skills.manifests.values())
                reply = f"{at_prefix}已加载 {len(skills.manifests)} 个 Skills：{names}"
        elif cmd == "skills reload":
            skills = getattr(self.core, "skills", None)
            if skills is None:
                reply = f"{at_prefix}Skills 未启用"
            else:
                count = skills.reload()
                suffix = f"；{len(skills.errors)} 个无效文件已跳过" if skills.errors else ""
                reply = f"{at_prefix}Skills 已热重载：{count} 个{suffix}"
        elif cmd in ("drafts", "drafts list"):
            drafts = getattr(self.core, "skill_drafts", None)
            items = drafts.pending() if drafts else []
            if not items:
                reply = f"{at_prefix}当前没有待审核的 SkillDraft"
            else:
                rows = [f"{item.id}: {item.manifest.get('name', '未命名')} | {item.request[:80]}"
                        for item in items]
                reply = f"{at_prefix}" + "\n".join(rows[:10])
        elif raw_cmd.startswith("draft create "):
            drafts = getattr(self.core, "skill_drafts", None)
            try:
                draft = await drafts.create_from_request(raw_cmd[len("draft create "):])
                errors = "；".join(draft.errors) if draft.errors else "静态验证通过"
                reply = f"{at_prefix}已创建草案 {draft.id}（{errors}）。请先 !anima drafts 查看，再用 approve/reject 审核。"
            except (ValueError, RuntimeError, AttributeError) as exc:
                reply = f"{at_prefix}创建 SkillDraft 失败：{str(exc)[:160]}"
        elif raw_cmd.startswith("draft preview "):
            drafts = getattr(self.core, "skill_drafts", None)
            try:
                draft = await drafts.preview(raw_cmd[len("draft preview "):].strip())
                result = draft.preview_result or {}
                reply = (f"{at_prefix}预览完成：{result.get('name', '网页')} | "
                         f"{result.get('title', '')} | {result.get('state', '')}。"
                         "确认无误后再使用 !anima draft approve <id>。")
            except (ValueError, RuntimeError, AttributeError) as exc:
                reply = f"{at_prefix}草案预览失败：{str(exc)[:180]}"
        elif raw_cmd.startswith("draft approve "):
            drafts = getattr(self.core, "skill_drafts", None)
            try:
                draft = drafts.approve(raw_cmd[len("draft approve "):].strip(), user_id)
                reply = f"{at_prefix}已批准并安装 SkillDraft {draft.id}：{draft.manifest.get('name')}"
            except (ValueError, AttributeError) as exc:
                reply = f"{at_prefix}批准失败：{str(exc)[:180]}"
        elif raw_cmd.startswith("draft reject "):
            drafts = getattr(self.core, "skill_drafts", None)
            try:
                draft = drafts.reject(raw_cmd[len("draft reject "):].strip(), user_id)
                reply = f"{at_prefix}已拒绝 SkillDraft {draft.id}"
            except (ValueError, AttributeError) as exc:
                reply = f"{at_prefix}拒绝失败：{str(exc)[:180]}"
        elif cmd in ("approvals", "approvals list"):
            items = self.core.approvals.pending()
            if not items:
                reply = f"{at_prefix}当前没有待审批的高危工具请求"
            else:
                rows = [f"{item.id}: {item.tool} 参数摘要={item.args_digest}"
                        for item in items]
                reply = f"{at_prefix}" + "\n".join(rows[:10])
        elif raw_cmd.startswith("approve "):
            try:
                approval_id = raw_cmd[len("approve "):].strip()
                result = await self.core.tools.approve_and_execute(approval_id, user_id)
                reply = f"{at_prefix}已批准并执行 {approval_id}：{result[:300]}"
            except (ValueError, AttributeError) as exc:
                reply = f"{at_prefix}审批执行失败：{str(exc)[:180]}"
        elif raw_cmd.startswith("deny "):
            try:
                approval_id = raw_cmd[len("deny "):].strip()
                self.core.tools.reject_approval(approval_id, user_id)
                reply = f"{at_prefix}已拒绝高危请求 {approval_id}"
            except (ValueError, AttributeError) as exc:
                reply = f"{at_prefix}拒绝失败：{str(exc)[:180]}"
        elif cmd == "subscriptions" or cmd == "subscriptions list":
            monitors = getattr(self.core, "monitors", None)
            if monitors is None or not monitors.store.items:
                reply = f"{at_prefix}当前没有直播订阅"
            else:
                rows = [f"{s.id}: {s.platform}/{s.subject_id} -> {','.join(map(str, s.target_groups))} ({'开' if s.enabled else '关'})" for s in monitors.store.items.values()]
                reply = f"{at_prefix}" + "\n".join(rows[:10])
        elif raw_cmd.startswith("subscribe bili "):
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
        elif raw_cmd.startswith("subscribe http "):
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
        elif raw_cmd.startswith("unsubscribe "):
            sub_id = raw_cmd[len("unsubscribe "):].strip()
            monitors = getattr(self.core, "monitors", None)
            reply = f"{at_prefix}{'已删除订阅' if monitors and monitors.store.remove(sub_id) else '未找到订阅'}：{sub_id}"
        elif raw_cmd.startswith("memory show "):
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
        elif raw_cmd.startswith("memory replace "):
            try:
                _, _, user_text, index_text, content = raw_cmd.split(" ", 4)
                from anima.crossgroup import get_crossgroup_manager
                ok = get_crossgroup_manager().replace_user_fact(
                    str(int(user_text)), int(index_text), content,
                )
                reply = f"{at_prefix}{'已纠正' if ok else '未找到'}跨群事实 F{index_text}"
            except (TypeError, ValueError):
                reply = f"{at_prefix}用法：!anima memory replace <QQ号> <事实序号> <新内容>"
        elif raw_cmd.startswith("memory delete fact "):
            try:
                _, _, _, user_text, index_text = raw_cmd.split(" ", 4)
                from anima.crossgroup import get_crossgroup_manager
                ok = get_crossgroup_manager().delete_user_fact(str(int(user_text)), int(index_text))
                reply = f"{at_prefix}{'已删除' if ok else '未找到'}跨群事实 F{index_text}"
            except (TypeError, ValueError):
                reply = f"{at_prefix}用法：!anima memory delete fact <QQ号> <事实序号>"
        elif raw_cmd.startswith("memory delete vector "):
            try:
                rowid = int(raw_cmd[len("memory delete vector "):].strip())
                if not getattr(self.core, "vector_memory", None):
                    raise ValueError
                ok = await self.core.vector_memory.delete(rowid)
                reply = f"{at_prefix}{'已删除' if ok else '未找到'}向量记忆 V{rowid}"
            except (TypeError, ValueError):
                reply = f"{at_prefix}用法：!anima memory delete vector <rowid>"
        elif raw_cmd.startswith("memory clear "):
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
        elif cmd == "quality" or raw_cmd.startswith("quality "):
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
        elif cmd in ("iterate", "iterate status") or raw_cmd.startswith("iterate "):
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
        elif cmd == "digest" or raw_cmd.startswith("digest "):
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
        elif cmd == "selfcode" or cmd.startswith("selfcode "):
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
        elif cmd == "drift" or cmd.startswith("drift "):
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
        elif cmd.startswith("remember "):
            # 手动补种/纠偏跨群用户事实（保留原始大小写）
            fact_text = raw_cmd[len("remember "):].strip()
            if not fact_text:
                reply = f"{at_prefix}用法：!anima remember <事实>"
            else:
                from anima.crossgroup import get_crossgroup_manager
                cgm = get_crossgroup_manager()
                sender_name = event.get("raw", {}).get("sender", {}).get("nickname", str(user_id))
                src = str(group_id) if detail_type == "group" else "private"
                f = cgm.add_user_fact(str(user_id), fact_text, source_group=src, name=sender_name)
                reply = (f"{at_prefix}已记住：{fact_text}" if f
                         else f"{at_prefix}这条已经记过了：{fact_text}")
        elif cmd == "stickers":
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
        elif cmd == "clear_stickers":
            # 清空表情库（隐私/重置用）
            try:
                from anima.cognitive.stickers import get_sticker_manager
                n = get_sticker_manager(self.core.config.persistence.data_dir).clear()
                reply = f"{at_prefix}已清空表情库（删除 {n} 张）"
            except Exception:
                reply = f"{at_prefix}清空失败"
        elif cmd in ("stickers pending", "sticker pending"):
            from anima.cognitive.stickers import get_sticker_manager
            items = get_sticker_manager(self.core.config.persistence.data_dir).pending()
            reply = (f"{at_prefix}没有待审核表情" if not items else
                     f"{at_prefix}" + "\n".join(f"{h[:12]} 来源群{m.get('src_group')} 标签{m.get('labels', [])}" for h, m in items[:10]))
        elif raw_cmd.startswith("sticker approve ") or raw_cmd.startswith("sticker reject "):
            from anima.cognitive.stickers import get_sticker_manager
            approve = raw_cmd.startswith("sticker approve ")
            sticker_hash = raw_cmd.split()[-1].strip()
            manager = get_sticker_manager(self.core.config.persistence.data_dir)
            ok = manager.approve(sticker_hash) if approve else manager.reject(sticker_hash)
            reply = f"{at_prefix}{'已批准' if approve else '已拒绝'}" if ok else f"{at_prefix}未找到待审核表情"
        elif raw_cmd.strip() == "sticker reclassify":
            from anima.cognitive.stickers import get_sticker_manager
            mgr = get_sticker_manager(self.core.config.persistence.data_dir)
            if not getattr(self.core.config.sticker, "classify_at_collect", True):
                reply = f"{at_prefix}分类闸门已关闭（classify_at_collect=false），无法重新分类"
            else:
                n = mgr.count()
                asyncio.create_task(self._reclassify_stickers(mgr, detail_type, target_id))
                reply = (f"{at_prefix}开始用 LLM 重新分类现有 {n} 张表情库"
                         f"（非表情包将被剔除），完成后汇报结果~")
        else:
            return False

        await self.core.gateway.send(OutboundMsg(detail_type, target_id, reply))
        return True

    # ------------------------------------------------------------------
    # G1：跨群氛围观察 + 事实抽取（前置到回复闸门之前）
    # ------------------------------------------------------------------

    async def _observe_crossgroup(self, text: str, user_id: int,
                                  group_id: Optional[int], detail_type: str,
                                  sender_name: str) -> None:
        """对所有入站消息做跨群观察：氛围（本地、零成本）+ 事实抽取（后台异步）。"""
        cfg = self.core.config.crossgroup
        if not cfg.enabled:
            return
        if not text or text.startswith("!"):
            return
        # owner 忽略名单：记录最近群发言者；识别 owner 的“别理那两人”并持久化。
        if detail_type == "group" and group_id is not None:
            try:
                self._track_group_speaker(int(group_id), int(user_id))
            except Exception:
                logger.debug("ignore-track speaker failed", exc_info=True)
            if self._is_owner(int(user_id)):
                try:
                    self._maybe_apply_ignore_directive(text, int(group_id), int(user_id))
                except Exception:
                    logger.debug("ignore-directive apply failed", exc_info=True)
        from anima.crossgroup import get_crossgroup_manager
        cgm = get_crossgroup_manager()
        cgm.max_facts_per_user = cfg.max_facts_per_user

        # 氛围观察（仅群聊，纯启发式，不调 LLM）
        if detail_type == "group" and group_id is not None:
            try:
                cgm.observe_message(str(group_id), text)
            except Exception:
                logger.exception("跨群氛围观察异常")

        # 事实抽取（后台异步，不阻塞回复；失败静默）
        if self.core.llm is not None and len(text) >= cfg.min_fact_text_len:
            asyncio.create_task(
                self._extract_and_store(user_id, sender_name, group_id, text, cfg)
            )

    async def _extract_and_store(self, user_id: int, name: str,
                                 group_id: Optional[int], text: str, cfg) -> None:
        """后台事实抽取任务：调 LLM 抽事实/外号/关系 → 写入记忆。绝不抛出。"""
        try:
            from anima.crossgroup import get_crossgroup_manager, extract_user_facts
            cgm = get_crossgroup_manager()
            result = await extract_user_facts(
                self.core.llm, text, name, min_len=cfg.min_fact_text_len
            )
            src = str(group_id) if group_id is not None else "private"
            facts = result.get("facts", [])
            for f in facts:
                cgm.add_user_fact(
                    str(user_id), f["content"],
                    source_group=src, tags=f.get("tags", []), name=name,
                )
            # 外号独立于「事实」落库：即使本次没有抽到事实，只要出现了别人
            # 对 TA 的称呼（如「大家都叫他企鹅仔」），也必须存下来，否则外号会丢失。
            if result.get("nicknames"):
                cgm.add_nicknames(str(user_id), result["nicknames"], name=name)
            # C: 用户间关系边
            relations = result.get("relations", [])
            if relations:
                from anima.crossgroup.memory.user_relations import (
                    get_user_relation_store,
                )
                store = get_user_relation_store(
                    self.core.config.persistence.data_dir
                )
                for r in relations:
                    store.add(name or str(user_id), r["target"], r["relation"],
                              note=r.get("note", ""), source=src)
                logger.info("跨群抽取到 %d 条关系边 user=%s", len(relations), user_id)
            if facts or result.get("nicknames") or relations:
                logger.info(
                    "跨群抽取 user=%s: 事实%d 外号%d 关系%d",
                    user_id, len(facts), len(result.get("nicknames", [])), len(relations),
                )
        except Exception:
            logger.exception("跨群事实抽取后台任务异常")

    def _periodic_dict_cleanup(self) -> None:
        """每 10 分钟清理一次无界字典，防止长期运行 OOM。"""
        now = time.time()
        last = getattr(self, "_last_dict_cleanup", 0)
        if now - last < 600:
            return
        self._last_dict_cleanup = now
        # 清理 30 分钟前的节流记录（getattr 兜底：测试可能跳过 __init__）
        for attr in ("_sticker_last", "_sticker_auto_last", "_audio_last"):
            d = getattr(self, attr, None)
            if d:
                stale_keys = [k for k, v in d.items() if now - v > 1800]
                for k in stale_keys:
                    d.pop(k, None)
        # 清理未锁住的旧 turn_locks（超过 5000 个时裁剪，避免并发窗口内删除正在使用的锁）
        locks = getattr(self, "_turn_locks", None)
        if locks and len(locks) > 5000:
            unlocked = [k for k, lock in locks.items() if not lock.locked()]
            # 只删除 1/4，降低竞态窗口内删除活跃锁的概率
            for k in unlocked[:len(unlocked) // 4]:
                locks.pop(k, None)

    # ------------------------------------------------------------------
    # A1 睡眠模式：凌晨被 @ 时迷糊回一句（不调工具 / 不跑 ReAct）
    # ------------------------------------------------------------------
    async def _sleepy_reply(self, group_id, user_id, text, acfg) -> None:
        """睡眠时段被 @ 时的迷糊回复：短、含糊、绝不动工具。

        优先用一次极低 max_tokens 的 LLM 调用（temperature 偏低，含糊），
        LLM 不可用或结果异常时回退到模板，保证夜间永远有回应且不崩。
        """
        import random as _rnd

        _canned = [
            "呜…干嘛…本喵睡着了喵…",
            "唔…谁啊…本喵在睡觉…",
            "喵…（翻个身）…别闹…",
            "哼…大半夜的…本喵困了…",
            "呜…干嘛啦…明天再说喵…",
        ]
        reply = None
        if getattr(acfg, "sleepy_llm_enabled", True) and self.core.llm is not None:
            try:
                _prompt = (
                    "你是猫娘 Bot 哈基米，现在凌晨正在睡觉，被人叫醒了。"
                    "用半睡半醒、含糊、爱理不理的语气回一句话"
                    "（不超过 20 字，可带喵/呜/唔）。"
                    f"对方刚才说：{text[:50]}"
                )
                reply = await self.core.llm.chat(
                    [{"role": "user", "content": _prompt}],
                    temperature=getattr(acfg, "sleepy_temperature", 0.4),
                    max_tokens=getattr(acfg, "sleepy_max_tokens", 20),
                    module="sleep", operation="sleepy_reply",
                )
                reply = (reply or "").strip().strip('"\'')
                if not reply or len(reply) > 60:
                    reply = None
            except Exception:
                logger.debug("[睡眠] 迷糊回复 LLM 失败，回退模板", exc_info=True)
        if not reply:
            reply = _rnd.choice(_canned)
        try:
            await self.core.gateway.send(OutboundMsg("group", int(group_id), reply))
        except Exception:
            logger.exception("[睡眠] 迷糊回复发送失败")

    def _maybe_record_feedback(self, text: str, *, sender_name: str, user_id, group_id,
                               detail_type: str, message_id, audit) -> Any:
        """P3 —— 识别工程反馈并记审计（只读，绝不自动修复）。

        仍只审计 + 记日志；返回值供调用方决定是否在主回复末尾追加一句轻量确认
        （仅由 _maybe_append_feedback_ack 消费，须显式开启，默认关）。
        """
        try:
            from anima.observation.feedback_detect import detect_engineering_feedback
            fb = detect_engineering_feedback(
                text,
                sender_name=sender_name,
                message_id=message_id,
                conversation_id=group_id or user_id,
                source="qq_group" if detail_type == "group" else "qq_private",
            )
            if fb is not None:
                if audit:
                    audit.emit(
                        "engineering_feedback_detected",
                        kind=fb.kind,
                        source=fb.source,
                        user_id=int(user_id),
                        group_id=int(group_id) if group_id else None,
                        message_id=message_id,
                        severity=fb.severity,
                        problem=fb.problem,
                    )
                logger.info("识别到工程反馈 kind=%s severity=%s from=%s：%s",
                            fb.kind, fb.severity, fb.source, fb.problem)
            return fb
        except Exception:  # noqa: BLE001 - 反馈识别失败绝不阻断正常回复
            logger.debug("工程反馈识别失败（忽略）", exc_info=True)
            return None

    async def handle(self, event: dict) -> None:
        # 提取事件信息
        text = event.get("text", "").strip()
        user_id = event.get("user_id")
        group_id = event.get("group_id")
        raw = event.get("raw", {})
        detail_type = raw.get("message_type", "private")
        sender_name = clean_display_name(raw.get("sender", {}).get("nickname", str(user_id)))

        if not user_id:
            return

        # 周期性清理无界字典，防止长期运行内存泄漏
        self._periodic_dict_cleanup()

        # NapCat 的 reply 段常常只有消息 id，没有被回复者 QQ。群聊闸门在这种
        # 情况下不能仅靠 reply 段判断；提前 get_msg 一次并复用其图片内容，既补全
        # 「回复哈基米」触发，也避免后面为引用图片重复请求。
        replied_message = None
        if detail_type == "group" and event.get("reply_message_id"):
            replied_message = await self._fetch_reply_message(
                event["reply_message_id"], current_group_id=event.get("group_id"))
            if not event.get("reply_to_self"):
                qq_cfg = getattr(getattr(self.core, "config", None), "qq", None)
                self_id = event.get("self_id") or raw.get("self_id") or getattr(qq_cfg, "self_id", None)
                reply_sender = _onebot_message_sender_id(replied_message)
                if self_id is not None and reply_sender is not None:
                    event["reply_to_self"] = str(reply_sender) == str(self_id)
            # 引用回复正文：reply 段若只带 id 未内联正文，用已拉到的消息提取纯文本，
            # 让 bot 真正「看到」被引用的话（图片走另一条 _fetch_reply_images 路径）。
            if not event.get("reply_text"):
                rt = self._extract_reply_text(replied_message)
                if rt:
                    event["reply_text"] = rt[:1500]

        if detail_type == "group" and group_id:
            try:
                presence = self.core.group_presence.observe(
                    int(group_id), summoned=bool(event.get("mentions_self") or event.get("reply_to_self")),
                )
                event["group_presence"] = presence.state
            except Exception:
                logger.exception("群在场状态更新失败")
            # I2: 社交疲劳检测（5 分钟内被 @ 超过 6 次触发 overwhelmed）
            if event.get("mentions_self") or event.get("reply_to_self"):
                try:
                    from anima.cognitive.mood import get_mood_system
                    # F6：按群隔离计数，A 群被刷屏的疲劳不拖累 B 群
                    cnt = self._record_group_mention(group_id)
                    if cnt > 6:
                        data_dir = str(self.core.config.persistence.data_dir)
                        get_mood_system(data_dir).trigger("overwhelmed", 0.5)
                        logger.info("社交疲劳触发：群%s 5分钟内被@%d次", group_id, cnt)
                        # 疲劳 gate：发简短回复并跳过完整 pipeline，避免被刷屏时每条都走 LLM
                        if cnt > 8:
                            import random as _rnd
                            _tired_replies = [
                                "本喵累了喵…先歇会儿…",
                                "呜…本喵的脑袋转不过来了喵…",
                                "你们能不能让本喵喘口气喵…",
                                "本喵要午睡一会儿…别戳了喵…",
                            ]
                            _tired = _rnd.choice(_tired_replies)
                            try:
                                await self.core.gateway.send(
                                    OutboundMsg("group", int(group_id), _tired))
                            except Exception:
                                pass
                            return
                except Exception:
                    pass

        audit = getattr(self.core, "audit", None)
        if audit:
            audit.emit(
                "message_received",
                user_id=int(user_id),
                group_id=int(group_id) if group_id else None,
                detail_type=detail_type,
                message_id=event.get("message_id"),
                text_length=len(text),
                media=event.get("media_observation"),
            )

        # P3：识别工程反馈（用户指出 Bot 出错）。只审计 + 记日志，绝不改变回复、
        # 绝不自动修复；真正修复走既有 R8 闭环 + Owner 审批门（self-improvement
        # ≠ self-authorization）。返回值供 §17 在主回复末尾追加轻量确认（默认关）。
        _fb_detected = self._maybe_record_feedback(
            text, sender_name=sender_name, user_id=user_id,
            group_id=group_id, detail_type=detail_type,
            message_id=event.get("message_id"), audit=audit)

        # P1-8：每次入站生成短 trace_id，串起本次处理的全部日志（命令/观察/出站），
        # 便于线上按 trace 追踪「收到 → 处理 → 出站」全链路。
        trace = secrets.token_hex(4)

        # 处理 owner 命令（不进入 LLM Pipeline）
        if await self._handle_command(event, text, user_id, group_id, detail_type):
            return
        if self._looks_like_owner_task(text, int(user_id), detail_type):
            await self.core.gateway.send(OutboundMsg("private", int(user_id), await self._create_owner_task(text, int(user_id))))
            return

        # P2 通用口头发话执行器：owner（或群内被@）的一句话要求就地办好并回执。
        vr = await self._try_verbal_request(text, int(user_id), group_id, detail_type, event)
        if vr:
            _vtgt = int(group_id) if detail_type == "group" else int(user_id)
            await self.core.gateway.send(OutboundMsg(detail_type, _vtgt, vr))
            return

        # Phase 3/4：主人多步 Agent 任务自动路由（仅真正主人，permission_level>=4）。
        # 审计 B0：is_agent_task 内部已强制 permission_level>=4，QQ 群主角色不会越权进入。
        from anima.agent.intent import is_agent_task
        if (self._is_owner(int(user_id)) and is_agent_task(text, 4)
                and getattr(self.core, "agent", None) is not None):
            # 群里用「引用原消息」而不是裸 @：@ 会渲染成群昵称，容易看起来
            # 像 bot 在乱发无关内容；引用能明确指向触发的那句话。
            # 引用 / @ 用结构化消息段，避免 [CQ:reply,...] 被当纯文本渲染。
            _msg_id = event.get("message_id")
            target_id = int(group_id) if detail_type == "group" else int(user_id)
            if detail_type == "group":
                # 审计 B2：群聊主人任务只建草稿 + 审批，避免打扰群；私聊则直接执行。
                task = self.core.agent.store.create(text, int(user_id))
                segments: list[dict] = []
                if _msg_id:
                    segments.append({"type": "reply", "data": {"id": str(_msg_id)}})
                else:
                    segments.append({"type": "at", "data": {"qq": str(user_id)}})
                segments.append({"type": "text", "data": {
                    "text": f"已创建 Agent 草稿 {task.id}，回复「!anima agent approve {task.id}」后执行。"}})
                await self.core.gateway.send(OutboundMsg(detail_type, target_id, segments))
            else:
                # 审计 B0-asym：私聊与群聊收敛为同一策略——只建草稿、需
                # 「!anima agent approve <id>」才执行。严禁无确认 auto_run
                # 高危工具（agent_exec/agent_browser/run_python 等）。
                task = self.core.agent.store.create(text, int(user_id))
                await self.core.gateway.send(OutboundMsg(
                    "private", int(user_id),
                    f"已创建 Agent 草稿 {task.id}，回复「!anima agent approve {task.id}」后执行。",
                ))
            return

        # 私聊连续消息收束为一个语义回合，避免逐条抢答破坏语境。
        if detail_type == "private" and self._assembler is not None:
            assembled = await self._assembler.ingest(event)
            if assembled is None:
                return
            event = assembled
            text = str(event.get("raw_message") or event.get("message") or "")

        # B1: 群聊防抖——同一用户 2s 内连续消息合并为一个语义回合
        _group_debouncer = getattr(self, "_group_debouncer", None)
        if detail_type == "group" and _group_debouncer is not None:
            assembled = await _group_debouncer.ingest(event)
            if assembled is None:
                return  # 已被合并，不单独处理
            event = assembled
            text = str(event.get("raw_message") or event.get("message") or "")

        # G1：跨群氛围观察 + 事实抽取（前置到回复闸门之前，对所有入站消息生效，
        # 这样"未 @bot 的群消息"也能学到氛围/事实）。氛围用本地启发式（零成本），
        # 事实抽取用 asyncio.create_task 后台跑，绝不拖慢回复。
        await self._observe_crossgroup(text, user_id, group_id, detail_type, sender_name)

        # 未 @ bot 的群消息同样进入仅内存、短 TTL 的群上下文；不写个人档案和向量库。
        if detail_type == "group" and group_id is not None:
            try:
                from anima.memory.chat_memory import get_chat_memory
                get_chat_memory().observe_group_message(
                    int(group_id), int(user_id), sender_name, text, event.get("message_id"),
                )
            except Exception:
                logger.exception("群短时上下文记录失败（已隔离）")

        # 引用回复取图：用户「引用图片 + @bot」时，NapCat 的 reply 段只带被引用
        # 消息 id 而不内联原图；这里主动 get_msg 拉回原消息，提取其中的 image 段
        # 合并进 event["images"]，让 bot 能真正「理解图片」（及表情收集）。仅在含
        # reply_message_id 时触发，不影响普通消息延迟。
        if event.get("reply_message_id"):
            try:
                extra = await self._fetch_reply_images(
                    event["reply_message_id"], message=replied_message,
                    current_group_id=event.get("group_id"),
                )
                if extra:
                    base = event.get("images") or []
                    seen = {i.get("file") for i in base}
                    for im in extra:
                        if im.get("file") and im["file"] in seen:
                            continue
                        base.append(im)
                        seen.add(im.get("file"))
                    event["images"] = base
            except Exception:
                logger.exception("引用回复取图失败（忽略，不阻塞回复）")

        # 表情包自收集：从群消息 image 段后台收集（不阻塞回复，类似 G1 模式）。
        # 用 try/except 包裹：收集链路任何异常都绝不拖垮主消息处理（否则 bot 不回复）。
        try:
            await self._observe_stickers(event, text, user_id, group_id, detail_type)
        except Exception:
            logger.exception("表情收集前置观察异常（已隔离，不阻塞回复）")

        # AstrBot 插件事件分发（如果插件消费了消息，不走 LLM）
        if self.core.config.compat.astrbot_enabled:
            try:
                from anima.compat import dispatch_compat
                plugin_result = await dispatch_compat(self.core, event)
                if plugin_result is not None:
                    logger.info("AstrBot 插件消费了消息 result_len=%d", len(str(plugin_result)))
                    return
            except Exception:
                logger.exception("AstrBot 插件分发异常")

        # A1 睡眠模式：凌晨 1-5 点，群聊被 @ 本喵时 80% 装睡不回、20% 迷糊回一句。
        # 不走完整 Pipeline / 不调工具（ReAct），保证夜间不打扰且不会误触发复杂动作。
        # 未 @ 本喵的消息直接落到下方闸门（常态 skip），无需特殊处理。
        try:
            acfg = self.core.config.autonomy
            if getattr(acfg, "sleep_enabled", False) and detail_type == "group" and group_id:
                from anima.cognitive.sleep_state import is_sleep_time, classify_sleep_reply
                if is_sleep_time(
                    start_hour=getattr(acfg, "sleep_start_hour", 1),
                    end_hour=getattr(acfg, "sleep_end_hour", 5),
                ):
                    _at_me = bool(event.get("mentions_self") or event.get("reply_to_self"))
                    _decision = classify_sleep_reply(
                        _at_me, getattr(acfg, "sleep_silent_prob", 0.8),
                    )
                    if _decision == "silent":
                        logger.info(
                            "[睡眠][trace=%s] 群%s @本喵，睡眠中装睡不回", trace, group_id,
                        )
                        return
                    if _decision == "sleepy":
                        await self._sleepy_reply(
                            group_id, int(user_id), text, acfg,
                        )
                        return
        except Exception:
            logger.exception("[睡眠] 模式判定异常（已隔离，退回正常流程）")

        # 群聊回复闸门：仅当被 @、被回复或文字喊「哈基米」时才回复；私聊始终回复。
        # owner 命令已在上方 _handle_command 处理，不受此闸门影响。
        group_policy = {}
        if detail_type == "group" and group_id is not None:
            policies = getattr(getattr(self.core.config, "permissions", None), "group_policies", {})
            candidate = policies.get(str(group_id), {}) if isinstance(policies, dict) else {}
            group_policy = candidate if isinstance(candidate, dict) else {}
        if detail_type == "group" and group_policy.get("reply_enabled") is False:
            logger.debug("[群策略][trace=%s] 群 %s 已关闭普通回复", trace, group_id)
            return
        if detail_type == "group":
            # owner 忽略名单：命中直接跳过（“别再理那两人”落地成真的拦截）。
            from anima.llm.ignore_list import should_skip_ignored
            if should_skip_ignored(self._ignore_store(), detail_type, int(group_id), int(user_id)):
                mode = resolve_group_reply_mode(group_policy, self.core.config.autonomy)
                logger.info("[回复闸门][trace=%s] skip reason=ignored_user gid=%s uid=%s mode=%s",
                            trace, group_id, user_id, mode)
                quality = getattr(self.core, "reply_quality", None)
                if quality is not None:
                    try:
                        quality.record_gate_skip("ignored_user")
                    except Exception:
                        logger.debug("ignore gate skip telemetry failed", exc_info=True)
                return
            # 唤醒会话窗：直接呼叫即开启/续期；窗内相关消息可自然接续。
            try:
                from anima.llm.group_gate import is_direct_group_call
                if is_direct_group_call(event, text):
                    self._mark_wake(int(group_id))
                session_active = self._wake_session_active(int(group_id))
            except Exception:
                session_active = False
            data_dir = getattr(getattr(self.core.config, "persistence", None), "data_dir", "data")
            skip_reason = classify_group_reply_skip(
                event, text,
                user_id=int(user_id),
                group_policy=group_policy,
                autonomy=self.core.config.autonomy,
                data_dir=str(data_dir or "data"),
                social=getattr(self.core.config, "social", None),
                session_active=session_active,
            )
            if skip_reason:
                mode = resolve_group_reply_mode(group_policy, self.core.config.autonomy)
                logger.info(
                    "[回复闸门][trace=%s] skip reason=%s mode=%s text_len=%d",
                    trace, skip_reason, mode, len(text or ""),
                )
                quality = getattr(self.core, "reply_quality", None)
                if quality is not None:
                    try:
                        quality.record_gate_skip(skip_reason)
                    except Exception:
                        logger.debug("gate skip telemetry failed", exc_info=True)
                return

        # 小文本附件理解：只经 OneBot get_file 读取受信任的本地/base64 返回，
        # 不跟随 URL，避免附件段成为 SSRF 或大文件读取入口。
        if event.get("files"):
            try:
                attachment_text = await self._extract_text_attachments(event)
                if attachment_text:
                    text = (text + "\n" + attachment_text).strip()
                    event["text"] = text
            except Exception:
                logger.exception("文本附件提取异常（已隔离，不阻塞回复）")

        if event.get("records"):
            try:
                transcript = await self._transcribe_records(event, int(user_id))
                if transcript:
                    text = (text + "\n[语音转写]\n" + transcript).strip()
                    event["text"] = text
            except Exception:
                logger.exception("语音转写异常（已隔离，不阻塞回复）")

        # 拟真输入状态：私聊时发送“正在输入...”让对方看到 bot 在打字
        try:
            _channel = getattr(getattr(self.core, "gateway", None), "channel", None)
            if _channel and hasattr(_channel, "set_typing_status"):
                qq_cfg = getattr(getattr(self.core, "config", None), "qq", None)
                _typing_enabled = getattr(qq_cfg, "typing_indicator", True) if qq_cfg else True
                if _typing_enabled and detail_type == "private":
                    await _channel.set_typing_status(int(user_id), True)
        except Exception:
            pass

        # M0.1 Commit 3c-1a (D1)：ctx.core 无条件注入。
        # flag ANIMA_CORE_FACADE_ENABLED 语义翻转（见 access.facade_enabled）：
        #   现控制「legacy meta["core"] 表面」是否保留，不再控制 ctx.core 是否存在。
        #   - true（默认）：保留 legacy 表面 + Facade（ctx.core 永远注入）。
        #   - false：仅移除 legacy 表面，Facade 仍在 → 迁移探针（Phase C：旧路径归零即达标）。
        # ctx.core 永远存在，Stage 侧单路径、无需 fallback；flag 关闭后若仍有 Stage 走
        # meta["core"] 即报错，暴露未迁移点。
        from anima.contracts.access import DeprecatedCoreAccess, facade_enabled

        _legacy_surface = facade_enabled()  # 旧 flag 名保留；现语义 = legacy 表面开关
        _meta = {"group_presence": event.get("group_presence", "")}
        if _legacy_surface:
            _meta["core"] = self.core

        # 构建 pipeline 上下文
        ctx = PipelineContext(
            event=event,
            text=text,
            user_id=user_id,
            group_id=group_id,
            detail_type=detail_type,
            sender_name=sender_name,
            mentioned_uids=event.get("at_uids", []),
            inbound_images=event.get("images") or [],
            meta=_meta,
        )
        # ctx.core 永远存在（Facade 表面）；Stage 迁移到 ctx.core.<facade>() 在 3c 进行。
        ctx.core = DeprecatedCoreAccess(self.core)

        # 霸凌检测（自我保护协议）：收到消息时先过 detector，命中则标记 context。
        # 后续 generation 阶段据此决定「冷回 / 沉默」，发送阶段据此抑制分段。
        try:
            from anima.cognitive.bullying_detector import detect_bullying
            from anima.memory.chat_memory import get_chat_memory
            _cm = get_chat_memory()
            _recent_texts: list[str] = []
            if detail_type == "group" and group_id:
                _recent_texts = [m["content"] for m in
                                 _cm.get_context("group", int(group_id), recent_rounds=8, user_id=int(user_id))
                                 if m.get("role") == "user"]
            else:
                _recent_texts = [m["content"] for m in
                                 _cm.get_context("private", int(user_id), recent_rounds=8, user_id=int(user_id))
                                 if m.get("role") == "user"]
            _bully = detect_bullying(int(user_id), text, _recent_texts[-8:])
            if _bully["is_bullying"]:
                ctx.meta["bullying_detected"] = True
                ctx.meta["bullying_recent_count"] = _bully["recent_count"]
                ctx.meta["bullying_severity"] = _bully["severity"]
                logger.info("[霸凌检测] user=%s is_bullying recent=%d sev=%d",
                            user_id, _bully["recent_count"], _bully["severity"])
        except Exception:
            logger.exception("霸凌检测异常（已隔离，不阻塞回复）")

        # 执行流水线。收束窗口外连续到达的私聊仍可能有多个 handle 协程，
        # 必须在此串行化，确保后一回合读取到前一回合已写入的用户/助手记忆。
        async with self._turn_lock(detail_type, group_id, user_id):
            ctx = await self.pipeline.run(ctx)

        # 发送回复
        if not ctx.should_reply or not ctx.reply:
            if ctx.should_reply:
                llm = getattr(self.core, "llm", None)
                health = llm.health() if llm and hasattr(llm, "health") else {}
                if audit:
                    audit.emit("reply_unavailable", trace=trace,
                               reason=health.get("last_error", "empty_reply"),
                               last_status=health.get("last_status"))
                quality = getattr(self.core, "reply_quality", None)
                if quality:
                    try:
                        quality.record_unavailable()
                        # L0：应回复却无可用正文（静默/空串），与 unavailable 同步计数
                        quality.record_empty_should_reply()
                    except Exception:
                        logger.exception("空回复遥测记录失败")
            return

        target_id = group_id if detail_type == "group" else user_id

        # 出站消息段：@ 触发者 + 文本 + （可选）表情图 + （可选）生图，
        # 抽成纯函数以便单测（逻辑见 _build_reply_segments）。
        segments = _build_reply_segments(
            ctx, detail_type, user_id, group_id, self.core,
            sticker_auto_last=self._sticker_auto_last,
        )
        # §17：安全版反馈自然回话——识别到反馈且开启时，仅私聊+已有主回复文本才在
        # 末尾追加一句轻量确认（不额外发消息、不改主回复内容）。默认关，开关读自
        # config.conversation.natural_feedback_ack（实例属性 feedback_ack_enabled 供测试覆盖）。
        _ack_cfg = getattr(getattr(self.core, "config", None), "conversation", None)
        _ack_on = bool(getattr(_ack_cfg, "natural_feedback_ack", False))
        _ack_on = _ack_on or bool(getattr(self, "feedback_ack_enabled", False))
        segments = _maybe_append_feedback_ack(
            segments, detail_type=detail_type, fb=_fb_detected, enabled=_ack_on,
        )

        quality = getattr(self.core, "reply_quality", None)
        if quality:
            try:
                quality.record_reply(
                    ctx.reply, detail_type=detail_type, target_id=target_id,
                    tool_trace=ctx.meta.get("tool_trace"),
                )
            except Exception:
                logger.exception("回复质量统计失败")

        # 拟人分段发送（A1/A2）：文本按标点智能分段 + 情绪/关系调制延迟
        messages = _split_reply_messages_with_split(
            segments, self.core, user_id, detail_type,
        )
        if not messages:
            return

        sent_ids = []
        last_send_ts = time.time()
        context_aware = getattr(
            getattr(self.core.config, "reply_split", None), "context_aware_interrupt", True
        )
        _bullying_send = bool(ctx.meta.get("bullying_detected", False))
        for idx, (msg_segs, delay) in enumerate(messages):
            # 霸凌保护（任务3）：冲突中只发第一段、截断到 30 字、不发送后续分段
            if _bullying_send:
                if idx > 0:
                    logger.info("[霸凌] 抑制后续分段 trace=%s", trace)
                    break
                msg_segs = _truncate_first_text_segment(msg_segs, 30)
            # 分段语境感知（P1）：发出后续段前，若用户已插话调侃则中断并自嘲收尾
            if idx > 0 and context_aware and _should_interrupt_for_teasing(
                getattr(self.core.gateway, "channel", None), group_id, last_send_ts
            ):
                logger.info(
                    "[分段中断] 用户在段%d后插话调侃，停止后续发送 trace=%s", idx, trace
                )
                try:
                    await self.core.gateway.send(
                        OutboundMsg(detail_type, target_id, "（被你抓包了…本喵闭嘴喵）")
                    )
                except Exception:
                    logger.exception("分段中断自嘲回应发送失败")
                break
            if delay > 0:
                await asyncio.sleep(delay)
            mid = await self.core.gateway.send(
                OutboundMsg(detail_type, target_id, msg_segs)
            )
            sent_ids.append(mid)
            last_send_ts = time.time()
            if mid is None:
                logger.warning("[出站失败][trace=%s] part=%d text_len=%d | segs=%s",
                               trace, idx, len(ctx.reply or ""), [s.get("type") for s in msg_segs])
            else:
                logger.info("[出站成功][trace=%s] part=%d msg_id=%s segs=%s",
                            trace, idx, mid, [s.get("type") for s in msg_segs])
        if all(m is None for m in sent_ids):
            logger.warning("[出站全失败][trace=%s]", trace)
        else:
            episodes = getattr(self.core, "episodes", None)
            if episodes is not None:
                try:
                    agenda_factory = None
                    if (
                        hasattr(self.core, "propose_agenda")
                        and detail_type == "group"
                        and group_id
                    ):
                        agenda_factory = self.core.propose_agenda
                    episodes.observe(
                        detail_type, group_id, user_id, "assistant", ctx.reply,
                        agenda_factory=agenda_factory,
                    )
                except Exception:
                    logger.exception("回复情节记忆写入失败")

        sylann = getattr(self.core, "sylann", None)
        if sylann is not None and hasattr(sylann, "record_response"):
            try:
                sylann.record_response(
                    success=any(mid is not None for mid in sent_ids),
                    response_chars=len(ctx.reply or ""),
                )
            except Exception:
                logger.exception("Sylann 出站反馈记录失败")

        # 视觉理解：pipeline 已把用户条目写入记忆，后台生成图片描述并补写，
        # 让后续轮次能忆起「用户发过一张什么图」。绝不阻塞回复。
        if (event.get("images")
                and self.core.config.vision.enabled
                and self.core.config.vision.store_desc_to_memory):
            asyncio.create_task(
                self._vision_describe(event["images"], user_id, group_id, detail_type)
            )

    # ------------------------------------------------------------------
    # 表情包自收集（后台，不阻塞回复）
    # ------------------------------------------------------------------

    async def _observe_stickers(self, event: dict, text: str, user_id: Any,
                                group_id: Any, detail_type: str) -> None:
        """从群消息 image 段收集表情包。私聊不收集（隐私）；频率节流。"""
        cfg = self.core.config.sticker
        if not (cfg.enabled and cfg.auto_collect):
            return
        if detail_type != "group":
            return
        images = event.get("images")
        if not images:
            return
        group_rule = f"group:{group_id}"
        user_rule = f"user:{user_id}"
        collect_allow = set(getattr(cfg, "collect_allowlist", []) or [])
        collect_block = set(getattr(cfg, "collect_blocklist", []) or [])
        if group_rule in collect_block or user_rule in collect_block:
            return
        if collect_allow and not ({group_rule, user_rule} & collect_allow):
            return
        logger.info("[表情收集] 收到 %d 张图（群=%s, 用户=%s）", len(images), group_id, user_id)
        now = time.time()
        if user_id in self._sticker_last and now - self._sticker_last[user_id] < cfg.collect_min_interval:
            return
        group_key = f"group:{group_id}"
        if getattr(cfg, "collect_mode", "cooldown") == "probability":
            if random.random() > max(0.0, min(1.0, float(getattr(cfg, "collect_probability", 0.3)))):
                return
        elif now - self._sticker_last.get(group_key, 0) < max(0.0, float(getattr(cfg, "collect_cooldown_sec", 30.0))):
            return
        self._sticker_last[user_id] = now
        self._sticker_last[group_key] = now

        # 构造打标上下文：当前文本 + 该群最近消息（增强标签质量）
        ctx_text = text or ""
        try:
            # P1-2 修复：配置无 memory.memory_dir 字段；应复用全局 chat_memory 单例，
            # 而非新建一个指向不存在路径的 ChatMemory。且 get_group_context 返回
            # {role, content}（非 text 字段），须按 content 拼接。
            from anima.memory.chat_memory import get_chat_memory
            recent = get_chat_memory().get_group_context(group_id, recent_rounds=3)
            parts = [r.get("content", "") for r in recent if r.get("content")]
            if parts:
                ctx_text = "\n".join(parts[-3:]) + ("\n" + text if text else "")
        except Exception:
            logger.exception("表情收集上下文获取失败（忽略，不阻塞收集）")

        imgs = [{
            "url": (i.get("url") or ""),
            "file": (i.get("file") or ""),
            "sub_type": (i.get("sub_type") or ""),
            "file_id": (i.get("file_id") or ""),
        } for i in images]
        # 后台收集，绝不拖慢回复
        asyncio.create_task(self._collect_stickers(imgs, user_id, group_id, ctx_text))

    async def _collect_stickers(self, imgs: list, user_id: Any,
                                group_id: Any, ctx_text: str) -> None:
        try:
            from anima.cognitive.stickers import get_sticker_manager
            mgr = get_sticker_manager(self.core.config.persistence.data_dir)
            resolver = self._image_resolver()
            await mgr.collect(
                imgs, user_id, group_id, ctx_text, resolver=resolver,
                approval_required=self.core.config.sticker.require_owner_review,
                llm=getattr(self.core, "llm", None),
                pending_capacity=self.core.config.sticker.pending_capacity,
                default_scope=self.core.config.sticker.default_scope,
            )
        except Exception:
            logger.exception("表情收集任务失败")

    async def _reclassify_stickers(self, mgr, detail_type: str, target_id: Any) -> None:
        """后台重新分类现有表情库，完成后向触发者汇报结果。"""
        try:
            res = await mgr.reclassify_all(
                getattr(self.core, "llm", None),
                model=self.core.config.sticker.select_model
                or self.core.config.sticker.label_model)
            msg = (f"表情库重新分类完成：保留 {res['kept']} 张，"
                   f"剔除非表情包 {res['removed']} 张。")
        except Exception:
            logger.exception("重新分类失败")
            msg = "表情库重新分类失败（详见日志）"
        try:
            await self.core.gateway.send(OutboundMsg(detail_type, target_id, msg))
        except Exception:
            logger.exception("重新分类结果回送失败")

    def _image_resolver(self):
        """返回一个 async callable(img)->bytes，走 NapCat get_image 兜底取图。"""
        ch = getattr(self.core.gateway, "channel", None)
        if ch is not None and hasattr(ch, "resolve_image"):
            return ch.resolve_image
        return None

    async def _extract_text_attachments(self, event: dict) -> str:
        """提取小型纯文本附件，失败或不支持时只给模型明确的能力边界。"""
        channel = getattr(self.core.gateway, "channel", None)
        if channel is None or not hasattr(channel, "get_file"):
            return "[附件：当前渠道无法读取附件内容]"
        rows: list[str] = []
        for item in (event.get("files") or [])[:2]:
            file_id = str(item.get("file_id") or "")
            name = str(item.get("name") or "附件")
            suffix = Path(name).suffix.lower()
            if suffix not in _TEXT_ATTACHMENT_SUFFIXES | _DOCUMENT_ATTACHMENT_SUFFIXES:
                rows.append(f"[附件 {name}：暂仅支持纯文本、PDF、DOCX、XLSX，未读取该格式]")
                continue
            response = await channel.get_file(file_id)
            if not response:
                rows.append(f"[附件 {name}：获取失败，未读取内容]")
                continue
            content: bytes | None = None
            encoded = response.get("base64")
            if isinstance(encoded, str) and encoded:
                try:
                    content = base64.b64decode(encoded, validate=True)
                except (ValueError, TypeError):
                    content = None
            if content is None:
                local = str(response.get("file") or response.get("path") or "")
                candidate = Path(local)
                if candidate.is_file():
                    try:
                        if candidate.stat().st_size <= _MAX_ATTACHMENT_BYTES:
                            content = candidate.read_bytes()
                    except OSError:
                        content = None
            if content is None:
                rows.append(f"[附件 {name}：内容不可用或超过 {_MAX_ATTACHMENT_BYTES // 1024}KB，未读取]")
                continue
            if len(content) > _MAX_ATTACHMENT_BYTES:
                rows.append(f"[附件 {name}：超过 {_MAX_ATTACHMENT_BYTES // 1024}KB，未读取]")
                continue
            if suffix in _DOCUMENT_ATTACHMENT_SUFFIXES:
                try:
                    from anima.document_text import extract_document_text
                    decoded = extract_document_text(name, content)
                except Exception as exc:
                    logger.info("文档附件提取失败 name=%s error=%s", name[:80], type(exc).__name__)
                    rows.append(f"[附件 {name}：文档内容提取失败，未读取]")
                    continue
            else:
                decoded = content.decode("utf-8-sig", errors="replace")
            decoded = decoded.strip()
            if not decoded:
                rows.append(f"[附件 {name}：文件为空或不是可读文本]")
                continue
            rows.append(f"[附件内容 {name}]\n{decoded[:12000]}")
        return "\n".join(rows)

    async def _transcribe_records(self, event: dict, user_id: int) -> str:
        self._record_audio_metric("requests")
        cfg = getattr(self.core.config, "audio", None)
        if not (cfg and cfg.transcription_enabled and self.core.llm):
            self._record_audio_metric("unavailable")
            return "[语音：当前未启用转写]"
        now = time.time()
        if now - self._audio_last.get(user_id, 0.0) < cfg.throttle_per_user:
            self._record_audio_metric("throttled")
            return "[语音：转写请求过于频繁，已跳过]"
        channel = getattr(self.core.gateway, "channel", None)
        if channel is None or not hasattr(channel, "get_record"):
            self._record_audio_metric("failures")
            return "[语音：当前渠道无法读取语音内容]"
        record = (event.get("records") or [])[0]
        try:
            response = await channel.get_record(str(record.get("file") or ""))
        except Exception:
            self._record_audio_metric("failures")
            logger.exception("NapCat 语音取回失败")
            return "[语音：无法取回音频]"
        # NapCat/OneBot implementations differ here: the action may return a
        # local path, a downloadable URL, or (for some adapters) base64 data.
        # Resolve all three forms without logging or persisting audio bytes.
        response = response if isinstance(response, dict) else {}
        data: bytes | None = None
        fmt = str(response.get("format") or response.get("out_format") or "wav").lower().lstrip(".")
        local_name = str(response.get("file") or response.get("path") or "")
        if local_name:
            candidate = Path(local_name)
            if candidate.is_file():
                try:
                    if candidate.stat().st_size <= cfg.max_bytes:
                        data = candidate.read_bytes()
                        fmt = candidate.suffix.lower().lstrip(".") or fmt
                except OSError:
                    logger.debug("NapCat audio local file is not readable")

        url = str(response.get("url") or response.get("download_url") or "").strip()
        if data is None and url:
            try:
                downloader = getattr(channel, "_download", None)
                if callable(downloader):
                    data = await downloader(url, cfg.max_bytes)
                else:
                    import aiohttp
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                            if resp.status == 200:
                                candidate_data = await resp.read()
                                if len(candidate_data) <= cfg.max_bytes:
                                    data = candidate_data
            except Exception:
                logger.debug("NapCat audio URL fetch failed")
            suffix = Path(url.split("?", 1)[0]).suffix.lower().lstrip(".")
            if suffix:
                fmt = suffix

        if data is None:
            encoded = response.get("base64") or response.get("b64") or response.get("content")
            if isinstance(encoded, str) and encoded:
                try:
                    if encoded.startswith("data:") and "," in encoded:
                        header, encoded = encoded.split(",", 1)
                        if ";base64" not in header.lower():
                            encoded = ""
                    if encoded and len(encoded) <= (cfg.max_bytes * 4 // 3 + 16):
                        decoded = base64.b64decode(encoded, validate=True)
                        if len(decoded) <= cfg.max_bytes:
                            data = decoded
                except (ValueError, TypeError):
                    logger.debug("NapCat audio base64 is invalid")

        if not data:
            self._record_audio_metric("failures")
            return "[语音：内容不可用或超过大小限制]"
        self._audio_last[user_id] = now
        try:
            result = await self.core.llm.transcribe_audio(data, fmt, model=cfg.model or None)
        except Exception:
            self._record_audio_metric("failures")
            logger.exception("LLM 语音转写失败")
            return "[语音：转写服务暂时不可用]"
        if result and result.strip():
            self._record_audio_metric("successes")
            return result.strip()
        self._record_audio_metric("failures")
        return "[语音：转写服务未返回内容]"

    def _record_group_mention(self, group_id: Any) -> int:
        """记录某群本次被 @/被引用，返回该群近 5 分钟内的有效计数（已过期清理）。

        F6：计数按 ``group_id`` 隔离（私聊归到 key 0），避免 A 群被刷屏的疲劳
        状态拖累 B 群正常交互。asyncio 单线程模型下无传统竞态，无需加锁。
        """
        if not hasattr(self, "_mention_times_by_group"):
            self._mention_times_by_group: dict = {}
        _grp = int(group_id) if group_id else 0
        now = time.time()
        times = self._mention_times_by_group.setdefault(_grp, [])
        times.append(now)
        times = [t for t in times if now - t < 300]  # 5 分钟窗口
        self._mention_times_by_group[_grp] = times
        return len(times)

    async def _fetch_reply_message(self, message_id: Any,
                                  current_group_id: Any = None) -> Optional[dict]:
        """拉取一条被引用消息；失败或无有效 id 返回 None，不影响主回复。"""
        ch = getattr(self.core.gateway, "channel", None)
        if ch is None or not hasattr(ch, "get_msg"):
            return None
        # ANM-005：reply_message_id 强校验为 int。NapCat 偶以字符串传递，
        # 个别异常事件可能携带非数值；非法 id 直接跳过，避免把脏值透传给通道
        # （既能防崩溃，也避免被构造的畸形 id 触发非预期行为）。
        if isinstance(message_id, bool):
            return None
        if not isinstance(message_id, int):
            try:
                message_id = int(message_id)
            except (TypeError, ValueError):
                logger.debug("reply_message_id 非整数，跳过引用拉取: %r", message_id)
                return None
        try:
            msg = await ch.get_msg(message_id)
            if not isinstance(msg, dict):
                return None
            # F3-ext：归属校验——被引用消息须属于当前群，防跨群伪造引用
            # （如群 A 引用一条本属于群 B 的消息 id，bot 误读群 B 内容）。
            if current_group_id is not None:
                msg_group = msg.get("group_id")
                if msg_group is None and isinstance(msg.get("data"), dict):
                    msg_group = msg["data"].get("group_id")
                if msg_group is not None:
                    try:
                        if int(msg_group) != int(current_group_id):
                            logger.warning(
                                "reply_message_id %s 归属群 %s 与当前群 %s 不符，拒绝",
                                message_id, msg_group, current_group_id)
                            return None
                    except (TypeError, ValueError):
                        logger.debug("reply 消息 group_id 非整数，跳过归属校验: %r", msg_group)
            return msg
        except Exception:
            logger.exception("拉取引用回复消息失败")
            return None

    def _extract_reply_text(self, replied: Optional[dict]) -> str:
        """从 get_msg 返回的被引用消息里提取纯文本正文（忽略图片/at/face 等结构段）。

        绝不抛出。NapCat 返回形状一般为
        {"status":"ok","data":{"message":[...segments...],"sender":{...}}}，
        也有实现把 ``data.message`` 直接当作字符串。返回拼接后的纯文本，空则 ""。
        """
        if not isinstance(replied, dict):
            return ""
        data = replied.get("data", replied)
        if not isinstance(data, dict):
            return ""
        segs = data.get("message")
        if isinstance(segs, str):
            return segs.strip()
        if not isinstance(segs, list):
            return ""
        parts = []
        for seg in segs:
            if not isinstance(seg, dict):
                continue
            if seg.get("type") == "text":
                parts.append(str((seg.get("data") or {}).get("text", "")).strip())
        return " ".join(p for p in parts if p).strip()

    async def _fetch_reply_images(self, message_id: Any,
                                  message: Optional[dict] = None,
                                  current_group_id: Any = None) -> list[dict]:
        """拉取被引用消息中的 image 段（供 vision 理解引用图片）。绝不抛出。"""
        try:
            msg = message if isinstance(message, dict) else await self._fetch_reply_message(
                message_id, current_group_id=current_group_id)
            if not isinstance(msg, dict):
                return []
            raw_msg = msg.get("message", [])
            if not isinstance(raw_msg, list):
                return []
            imgs: list[dict] = []
            for seg in raw_msg:
                if not isinstance(seg, dict):
                    continue
                if seg.get("type") in ("image", "bface", "sface"):
                    d = seg.get("data", {}) or {}
                    imgs.append({
                        "url": d.get("url", "") or d.get("src", ""),
                        "file": d.get("file", ""),
                        "sub_type": d.get("subType", "") or seg.get("type"),
                        "file_id": d.get("file_id", "") or d.get("id", ""),
                    })
            if imgs:
                logger.info("[引用回复取图] 拉到 %d 张图 (msg_id=%s)", len(imgs), message_id)
            return imgs
        except Exception:
            logger.exception("拉取引用回复图片失败")
            return []

    # ------------------------------------------------------------------
    # 视觉理解（后台，不阻塞回复）
    # ------------------------------------------------------------------

    async def _vision_describe(self, images: list, user_id: Any,
                               group_id: Any, detail_type: str) -> None:
        """后台视觉理解：对入站图生成中文描述，补写进对话记忆。绝不抛出。"""
        if self.core.llm is None:
            return
        try:
            from anima.cognitive.vision import get_vision_manager
            from anima.media import fetch_image_bytes
            from anima.memory.chat_memory import get_chat_memory

            cfg = self.core.config.vision
            vmgr = get_vision_manager(cfg.describe_throttle_per_user)
            resolver = self._image_resolver()
            descs: list[str] = []
            for img in images[:4]:
                # VisionManager 会负责缩放/抽帧；这里保留可受控的原图，避免
                # 大图已经在下载层被跳过，导致主回复看见但长期记忆看不见。
                data = await fetch_image_bytes(
                    img,
                    getattr(cfg, "max_source_bytes", 32 * 1024 * 1024),
                    resolver=resolver,
                )
                if not data:
                    continue
                d = await vmgr.describe(self.core.llm, data, img, cfg.model)
                if d:
                    descs.append(d)
            if not descs:
                return
            desc = "；".join(descs)
            chat_id = group_id if detail_type == "group" else user_id
            get_chat_memory().set_vision_desc(detail_type, chat_id, user_id, desc)
            logger.info("视觉描述已存记忆（images=%d desc_len=%d）", len(descs), len(desc))
        except Exception:
            logger.exception("视觉描述后台任务异常")


# §17 安全版反馈自然回话：仅在私聊 + 已有主回复文本时，于主回复段末尾追加
# 一句轻量确认。绝不额外发第二条消息；默认关（由 handler 显式开启）。
_FEEDBACK_ACK_SUFFIX = "（本喵已经把这个问题记下喵～会去找原因哒）"


def _maybe_append_feedback_ack(segments: "list[dict]", *, detail_type: str,
                               fb: Any, enabled: bool) -> "list[dict]":
    """若有开启 + 私聊 + 识别到反馈 + 主回复已有文本，则在末尾追加一句轻量确认。

    纯函数（可单测）：不额外发消息、不改主回复内容；任一条件不满足则原样返回。
    """
    if not (enabled and fb is not None and detail_type == "private"):
        return segments
    if not any(isinstance(s, dict) and s.get("type") == "text" for s in segments):
        return segments
    return [*segments, {"type": "text", "data": {"text": _FEEDBACK_ACK_SUFFIX}}]


def _build_reply_segments(ctx, detail_type, user_id, group_id, core,
                          sticker_auto_last: dict | None = None) -> "list[dict]":
    """构造出站消息段：@ 触发者 + 文本 + （可选）表情图 + （可选）生图。

    表情附带规则（修复前 bug：群聊普通氛围下 LLM 已 pick_sticker 选了图却发不出）：
      - LLM 主动选了表情（ctx.sticker_to_send 有值）-> 尊重其意图，群聊/私聊都附带发出；
        不再受 always_atmosphere 限制，否则会出现「LLM 明明选了图却因群氛围被吞掉」。
      - 否则，仅当群聊且当前氛围命中 always_atmosphere 白名单时，被动随机带一张活跃气氛。
    NapCat 不保证以 bot 工作目录解析相对路径，统一传绝对路径。
    """
    segments: list[dict] = []
    if detail_type == "group":
        segments.append({"type": "at", "data": {"qq": str(user_id)}})
    segments.append({"type": "text", "data": {"text": ctx.reply}})
    scfg = getattr(core.config, "sticker", None)
    send_allowed = True
    if detail_type == "group" and scfg is not None:
        rules = {f"group:{group_id}", f"user:{user_id}"}
        blocked = set(getattr(scfg, "send_blocklist", []) or [])
        allowed = set(getattr(scfg, "send_allowlist", []) or [])
        send_allowed = not bool(rules & blocked) and (not allowed or bool(rules & allowed))
    sticker_path = getattr(ctx, "sticker_to_send", None)
    if sticker_path and send_allowed:
        # LLM 主动选了表情 -> 群聊/私聊都发，尊重意图（不受氛围/概率门控）。
        segments.append({"type": "image", "data": {"file": str(Path(sticker_path).resolve())}})
    elif detail_type == "group" and send_allowed:
        # 被动自动附带：概率/冷却模式 + 情绪联动（E1/E2）。
        # mood 非中性时优先用 mood label 选图（70%），文本推断作 fallback。
        try:
            from anima.cognitive.stickers import get_sticker_manager
            from anima.cognitive.mood import get_mood_system
            scfg = core.config.sticker
            sticker_auto_last = sticker_auto_last if sticker_auto_last is not None else {}
            now = time.time()
            last = sticker_auto_last.get(str(group_id), 0.0)
            if hasattr(scfg, "send_probability"):
                probability = float(scfg.send_probability)
                cooldown = float(getattr(scfg, "send_cooldown_sec", 0.0))
                by_emotion = bool(getattr(scfg, "send_by_emotion", False))
            else:
                probability = 1.0 if getattr(ctx, "atmosphere_style", "") in getattr(scfg, "always_atmosphere", []) else 0.0
                cooldown = 0.0
                by_emotion = False

            # E2: 情绪影响发图概率
            _mood_label = "neutral"
            try:
                _mood_sys = get_mood_system(core.config.persistence.data_dir)
                _mood_label = _mood_sys.get_state().label
            except Exception:
                pass
            if _mood_label in ("happy", "excited"):
                probability = min(1.0, probability * 1.4)
            elif _mood_label in ("tired", "annoyed", "bored"):
                probability = max(0.0, probability * 0.5)

            # R3: 伤痕影响表情包选择——对有活跃伤痕的用户降低发图概率
            try:
                from anima.cognitive.scar import get_scar_system
                _scar_sys = get_scar_system(core.config.persistence.data_dir)
                _user_scars = _scar_sys.get_for_user(int(ctx.user_id))
                if any(s.severity > 0.5 for s in _user_scars):
                    probability = max(0.0, probability * 0.6)
            except Exception:
                pass

            if (probability > 0
                    and now - last >= cooldown
                    and random.random() < probability):
                _mgr = get_sticker_manager(core.config.persistence.data_dir)
                _auto = None
                # E1: mood 非中性时优先按 mood label 选图（70%权重）
                _MOOD_TO_STICKER_EMOTION = {
                    "happy": "happy", "excited": "excited",
                    "annoyed": "angry", "tired": "tired", "bored": "speechless",
                    "curious": "curious",
                }
                _mood_emotion = _MOOD_TO_STICKER_EMOTION.get(_mood_label, "")
                if _mood_emotion and random.random() < 0.7:
                    _auto = _mgr.pick_by_emotion(
                        _mood_emotion, group_id=group_id,
                        allow_cross_group=bool(getattr(scfg, "share_across_groups", False)))
                # fallback: 文本情绪推断
                if _auto is None and by_emotion:
                    _em = _mgr.detect_reply_emotion(ctx.reply)
                    if _em:
                        _auto = _mgr.pick_by_emotion(
                            _em, group_id=group_id,
                            allow_cross_group=bool(getattr(scfg, "share_across_groups", False)))
                if _auto is None:
                    _auto = _mgr.random_one(
                        group_id=group_id,
                        allow_cross_group=bool(getattr(scfg, "share_across_groups", False)))
                if _auto:
                    sticker_auto_last[str(group_id)] = now
                    segments.append({"type": "image", "data": {"file": str(Path(_auto).resolve())}})
        except Exception:
            logger.exception("被动自动表情选取失败（忽略）")
    for image_path in getattr(ctx, "generated_image_paths", [])[:1]:
        segments.append({"type": "image", "data": {"file": image_path}})
    if getattr(ctx, "generated_image_paths", None):
        logger.debug("[IMGDEBUG] build_reply_segments ctx.generated_image_paths=%s",
                       getattr(ctx, "generated_image_paths", None))
    return segments


def _split_reply_messages(segments: "list[dict]") -> "list[list[dict]]":
    """把合并段拆成多条独立消息，实现「消息是消息、表情是表情」：

    - @ 与文本合并为一条正文消息；
    - 每个 image 段（表情包 / 生图）单独成为一条消息。

    无 image 时退化为单条，与旧行为一致。
    """
    body: list[dict] = []
    messages: list[list[dict]] = []
    for seg in segments:
        if seg.get("type") == "image":
            if body:
                messages.append(body)
                body = []
            messages.append([seg])
        else:
            body.append(seg)
    if body:
        messages.append(body)
    # 丢弃「纯空文本且无 @」的无效正文（正常 ctx.reply 非空，不会触发）
    cleaned = []
    for m in messages:
        if len(m) == 1 and m[0].get("type") == "text" and not (m[0].get("data") or {}).get("text"):
            continue
        cleaned.append(m)
    return cleaned


def _split_reply_messages_with_split(
    segments: "list[dict]",
    core: Any,
    user_id: Any,
    detail_type: str,
) -> "list[tuple[list[dict], float]]":
    """拟人分段发送：文本智能分段 + 情绪/关系调制延迟。

    返回 [(segments_list, delay_before_send), ...] 列表。
    图片段单独成条，文本段按 reply_splitter 拆分。
    """
    from anima.core.reply_splitter import SplitConfig, split_reply

    # 读取配置
    rs_cfg = getattr(core.config, "reply_split", None)
    if rs_cfg is None or not rs_cfg.enabled:
        # 回退到旧逻辑（无分段，固定 0.3s 间隔）
        msgs = _split_reply_messages(segments)
        return [(m, 0.3 if i > 0 else 0.0) for i, m in enumerate(msgs)]

    split_cfg = SplitConfig(
        enabled=rs_cfg.enabled,
        max_segments=rs_cfg.max_segments,
        min_segment_len=rs_cfg.min_segment_len,
        max_segment_len=rs_cfg.max_segment_len,
        trigger_threshold=rs_cfg.trigger_threshold,
        tail_merge_threshold=rs_cfg.tail_merge_threshold,
        delay_enabled=rs_cfg.delay_enabled,
        delay_base=rs_cfg.delay_base,
        delay_factor=rs_cfg.delay_factor,
        delay_jitter=rs_cfg.delay_jitter,
    )

    # 获取情绪和关系状态
    mood_label = "neutral"
    relation_stage = "ACQUAINTANCE"
    try:
        from anima.cognitive.mood import get_mood_system
        mood_label = get_mood_system(core.config.persistence.data_dir).get_state().label
    except Exception:
        pass
    try:
        from anima.cognitive.relation import get_relation
        manager = get_relation(core.config.persistence.data_dir)
        store = getattr(manager, "_relations", {}) or {}
        rel = store.get(str(int(user_id))) or store.get(int(user_id))
        if rel is not None:
            relation_stage = getattr(rel, "stage", "ACQUAINTANCE")
    except Exception:
        pass

    # 分离文本段和图片段
    at_seg: dict | None = None
    text_content = ""
    image_segs: list[dict] = []
    for seg in segments:
        if seg.get("type") == "at":
            at_seg = seg
        elif seg.get("type") == "text":
            text_content += (seg.get("data") or {}).get("text", "")
        elif seg.get("type") == "image":
            image_segs.append(seg)

    # 对文本进行拟人分段
    result = split_reply(text_content, split_cfg, mood_label, relation_stage)

    messages: list[tuple[list[dict], float]] = []
    for i, (seg_text, delay) in enumerate(zip(result.segments, result.delays)):
        msg_segs: list[dict] = []
        # @ 只加在第一段
        if i == 0 and at_seg is not None:
            msg_segs.append(at_seg)
        msg_segs.append({"type": "text", "data": {"text": seg_text}})
        # 第一段延迟为 0（已经等过了），后续段用计算延迟
        actual_delay = 0.0 if i == 0 else delay
        messages.append((msg_segs, actual_delay))

    # 图片段单独成条，固定 0.5s 延迟（模拟“发图”动作）
    for img_seg in image_segs:
        messages.append(([img_seg], 0.5))

    # 清理空消息
    cleaned: list[tuple[list[dict], float]] = []
    for msg_segs, delay in messages:
        if (len(msg_segs) == 1 and msg_segs[0].get("type") == "text"
                and not (msg_segs[0].get("data") or {}).get("text")):
            continue
        cleaned.append((msg_segs, delay))
    return cleaned


# ---------------------------------------------------------------------------
# 分段语境感知（P1）：用户插话调侃时中断后续分段
# ---------------------------------------------------------------------------

_TEASING_RE = re.compile(
    r"(心里话别说出来|别念叨了|别碎碎念|别自言自语|别自己跟自己说话|"
    r"别说了|打住|够了|闭嘴|说你呢|你怎么又|别自嗨|别沉浸|别自顾自|"
    r"你刚在跟谁说话|你在跟谁说话|别自己玩|念叨啥呢|你跟谁聊呢)",
    re.I,
)


def is_teasing_interrupt(text: str) -> bool:
    """判定用户插话是否为调侃/打断（此时应停止自顾自的多段输出）。"""
    return bool(_TEASING_RE.search(text or ""))


def _should_interrupt_for_teasing(channel: "Any", group_id: "int | None", since_ts: float) -> bool:
    """分段发送中，若用户在上一分段后插话调侃，返回 True（供发送循环中断用）。"""
    if channel is None or not hasattr(channel, "latest_inbound_since"):
        return False
    intr = channel.latest_inbound_since(group_id, since_ts)
    return intr is not None and is_teasing_interrupt(intr[1])


def _truncate_first_text_segment(msg_segs: "list[dict]", max_len: int = 30) -> "list[dict]":
    """霸凌保护：把第一条文本段截断到 max_len 字符（保留 @ 等前置段）。"""
    out: list[dict] = []
    done = False
    for seg in msg_segs:
        if (
            not done
            and isinstance(seg, dict)
            and seg.get("type") == "text"
        ):
            data = seg.get("data") or {}
            t = data.get("text", "")
            if isinstance(t, str) and len(t) > max_len:
                seg = {**seg, "data": {**data, "text": t[:max_len]}}
            done = True
        out.append(seg)
    return out
