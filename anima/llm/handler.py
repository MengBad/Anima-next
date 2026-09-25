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

from anima.constants import event_text
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
    is_cancel_signal,
    is_correction_signal,
    is_conversation_continuation,
    classify_group_reply_skip,
    should_reply_in_group,
)
from anima.util.name_clean import clean_display_name, sender_display_name

if TYPE_CHECKING:
    from anima.core.anima_core import AnimaCore

logger = logging.getLogger("anima.llm.handler")

# 单条回复附带「工具生图 / X 配图」的上限：避免一次拿几十张图刷屏 / QQ 段爆炸。
# 超出部分丢弃（用户可再要「下一批」）。每轮工具下载的图已由 generation 阶段去重保序传入。
MAX_GENERATED_IMAGES_PER_REPLY = 4

# ── 社交疲劳 gate 的罐头回复（第 14 个兜底出口，2026-09-21 补守护）────────────
# 触发：同群 5 分钟内被 @ 超过 `autonomy.fatigue_mention_hard`（默认 8）且发言者非 owner。
# 池化要求与其他兜底池一致：≥2 变体 / 场景无关 / 人格口吻。
_TIRED_REPLIES = (
    "本喵累了喵…先歇会儿…",
    "呜…本喵的脑袋转不过来了喵…",
    "你们能不能让本喵喘口气喵…",
    "本喵要午睡一会儿…别戳了喵…",
)

# 同群冷却：同一句罐头不得在短窗口内重复出站。
# 实测（09-21）：21:37:29 与 21:39:23 各发一次「本喵要午睡一会儿…别戳了喵…」。
_TIRED_COOLDOWN_SEC = 600.0

# 社交疲劳**情绪扣分**的同群去重窗（与上者独立：那个管"别再发罐头"，这个管"别重复扣分"）。
# 实测（09-21）：`cnt > soft` 之后每条被 @ 的消息都会再 `trigger("overwhelmed")`
# （valence −0.05 / arousal +0.1），一个 5 分钟窗口内可扣 4–5 次 ⇒ 把 mood 从 tired
# 象限推进 annoyed（arousal 越过 0.5），于是 C1 把回复砍到 120 字并补「算了不说了」。
_FATIGUE_MOOD_COOLDOWN_SEC = 300.0


def _record_tired_fallback(core: Any) -> None:
    """疲劳罐头出站计数（fail-soft）。

    口径同 `ReplyQuality.record_fallback` —— 这一条出站**不是模型正常生成的正文**。
    此前该出口既无计数也无冷却，正是"13 出口 / 12 埋点"口径漏掉的第 14 个。
    遥测绝不允许影响回复路径，故一律吞异常。
    """
    try:
        quality = getattr(core, "reply_quality", None)
        if quality is not None:
            quality.record_fallback("tired")
    except Exception:
        logger.debug("疲劳兜底遥测记录失败", exc_info=True)


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


# 队列④：实现迁至 anima/llm/commands.py；此处保留导入兼容旧测试引用
from anima.llm.commands import (  # 队列④第一刀：命令路由表 + 分支 mixin
    AnimaCommandsMixin,
    _parse_beijing_reminder_time,  # noqa: F401  兼容旧测试引用
)


class LLMHandler(AnimaCommandsMixin):
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
        # 同会话回复在途时, 后续直接@消息先吸收合并, 待上一回复发完再统一回复,
        # 避免「连续@→机器人连发」的刷屏/重复/上下文割裂。
        self._reply_inflight: set[tuple] = set()
        self._coalesced: dict[tuple, list[dict]] = {}

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
        from anima.pipeline.stages.prefilter import (
            prefilter_stage,
            attachment_text_stage,
            bullying_stage,
        )
        pipe = (Pipeline()
                .use(prefilter_stage)
                .use(attachment_text_stage)
                .use(bullying_stage)
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
                        .use(prefilter_stage)
                        .use(attachment_text_stage)
                        .use(bullying_stage)
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
        _db = getattr(self, "_group_debouncer", None)
        if _db is not None:
            try:
                await _db.close()
            except Exception:
                logger.exception("群防抖器关闭异常")

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
    WAKE_SESSION_TTL = 60.0  # 秒；可经 autonomy.wake_session_ttl_sec 覆盖

    def _wake_session_ttl(self) -> float:
        try:
            v = getattr(self.core.config.autonomy, "wake_session_ttl_sec", None)
            if v is not None:
                return float(v)
        except Exception:
            pass
        return float(self.WAKE_SESSION_TTL)

    # ── 每用户被动回复冷却：同一用户刚回完，未 @ 的被动接话先冷一冷 ──
    def _passive_cooldown_sec(self) -> float:
        try:
            v = float(getattr(self.core.config.autonomy, "passive_reply_cooldown_sec", 0))
            return max(0.0, v)
        except Exception:
            return 0.0

    def _passive_should_cooldown(self, group_id: Any, user_id: Any) -> bool:
        """麻该用户最近已回复且本次是非直接呼叫的被动接话 → True(跳过)。"""
        cd = self._passive_cooldown_sec()
        if cd <= 0:
            return False
        reg = getattr(self, "_passive_last_reply", None)
        if not reg:
            return False
        try:
            last = reg.get((str(int(group_id)), str(int(user_id))))
            if last is None:
                return False
            return (time.time() - last) < cd
        except Exception:
            return False

    def _mark_passive_replied(self, group_id: Any, user_id: Any) -> None:
        cd = self._passive_cooldown_sec()
        if cd <= 0:
            return
        try:
            reg = getattr(self, "_passive_last_reply", None)
            if reg is None:
                reg = self._passive_last_reply = {}
            reg[(str(int(group_id)), str(int(user_id)))] = time.time()
        except Exception:
            pass

    # 纠正豁免的频率上限：60s 内同一 (群,用户) 最多 3 次。防止在唤醒窗内
    # 「每条都带纠正词」无限豁免被动冷却、把豁免变成刷屏绕过（2026-09-06 复审）。
    _CONTINUATION_EXEMPT_MAX = 3
    _CONTINUATION_EXEMPT_WINDOW = 60.0

    def _continuation_exempt_ok(self, group_id: Any, user_id: Any) -> bool:
        try:
            key = (str(int(group_id)), str(int(user_id)))
        except (TypeError, ValueError):
            return False
        reg = getattr(self, "_continuation_exempt_log", None)
        if reg is None:
            reg = self._continuation_exempt_log = {}
        now = time.time()
        window = reg.setdefault(key, [])
        while window and now - window[0] > self._CONTINUATION_EXEMPT_WINDOW:
            window.pop(0)
        if len(window) >= self._CONTINUATION_EXEMPT_MAX:
            return False
        window.append(now)
        return True

    def _mark_wake(self, gid: int, uid: int | None = None) -> None:
        """唤醒窗按 (群,用户) 粒度：A 的唤醒不再让 B 的闲聊被当续话
        （2026-09-12 收敛计划 A3）。uid 缺省时退化为群级（兼容旧调用）。"""
        try:
            s = getattr(self, "_wake_sessions", None)
            if s is None:
                s = self._wake_sessions = {}
            key = f"{int(gid)}:{int(uid)}" if uid is not None else str(int(gid))
            s[key] = time.time() + self._wake_session_ttl()
        except Exception:
            logger.debug("mark wake failed", exc_info=True)

    def _wake_session_active(self, gid: Any, uid: Any = None) -> bool:
        try:
            s = getattr(self, "_wake_sessions", None)
            if not s:
                return False
            key = f"{int(gid)}:{int(uid)}" if uid is not None else str(int(gid))
            return bool(s.get(key)) and time.time() < s[key]
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
                    # 私聊提醒/记事按设计仅 owner 可执行（提醒落到群日历）。
                    # 非 owner 的提醒请求给确定性诚实指引，而不是回落 LLM
                    # 幻觉「本喵没有闹钟功能」（2026-09-06 第三轮测试 B2）。
                    from anima.llm.request_executor import classify_request_intent
                    if classify_request_intent(text) == "remind":
                        return ("提醒要落到群日历喵：在群里 @我 说一句"
                                "「明天早上7点提醒我喂猫」就行啦～")
                    return None
            else:
                # 同 `_handle_inner`：不得局部 import（会编译期遮蔽模块级 L22）。
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
        from anima.agent.owner_proposal import handle_owner_problem
        _t, reply = await handle_owner_problem(self.core, text, int(user_id), prefix="")
        return reply

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
        """处理 owner 命令。返回 True 表示已消费该消息。

        队列④第一刀：路由实现整体迁至 anima/llm/commands.py
        （AnimaCommandsMixin 分支 + COMMAND_ROUTES 路由表，顺序即语义）。
        """
        from anima.llm.commands import dispatch_anima_command
        return await dispatch_anima_command(
            self, event, text, user_id, group_id, detail_type)

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
                    # P1-2：抽取层早已产出 confidence（默认 0.5），此前下游丢弃，
                    # 导致低置信猜测与确凿事实同权。此处真正落库。
                    confidence=float(f.get("confidence") or 0.5),
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
        """入站消息入口：同会话回复在途时合并后续直接@消息, 单控回复节奏。"""
        if not hasattr(self, "_reply_inflight"):
            self._reply_inflight = set()
            self._coalesced = {}
        user_id = event.get("user_id")
        raw = event.get("raw", {}) or {}
        detail_type = raw.get("message_type", "private")
        group_id = event.get("group_id")
        direct = bool(event.get("mentions_self") or event.get("reply_to_self") or detail_type == "private")
        key = None
        if user_id is not None:
            target = int(group_id) if detail_type == "group" and group_id else int(user_id)
            key = (detail_type, target, int(user_id))
        if key is not None and direct:
            if key in self._reply_inflight:
                self._coalesced.setdefault(key, []).append(dict(event))
                _n = len(self._coalesced[key])
                _audit = getattr(self.core, "audit", None)
                if _audit:
                    try:
                        _audit.emit("message_coalesced", user_id=int(user_id),
                                    group_id=int(group_id) if group_id else None,
                                    detail_type=detail_type, message_id=event.get("message_id"), n=_n)
                    except Exception:
                        pass
                logger.info("[合并] 会话回复在途, 吸收后续@ (key=%s, n=%d)", key, _n)
                return
            self._reply_inflight.add(key)
        try:
            await self._handle_inner(event)
        finally:
            if key is not None:
                self._reply_inflight.discard(key)
                await self._drain_coalesced(key)

    async def _drain_coalesced(self, key: tuple) -> None:
        """上一回复完成后, 把吸收的后续@合并成一条统一再回复。"""
        pending = self._coalesced.pop(key, None)
        if not pending:
            return
        texts = [str(e.get("text") or "").strip() for e in pending]
        texts = [t for t in texts if t]
        if not texts:
            return
        head = texts[-1]
        extras = [t for t in texts[:-1] if t]
        merged = head
        if extras:
            joined = "；".join(reversed(extras))
            merged = f"{head}\n（另补充: {joined[:400]}）"
        synth = dict(pending[-1])
        synth["text"] = merged[:800]
        _audit = getattr(self.core, "audit", None)
        if _audit:
            try:
                _audit.emit("message_coalesce_drained", key=str(key), n=len(pending))
            except Exception:
                pass
        logger.info("[合并] 上一回复完成, 统一再回复 %d 条在途消息 (key=%s)", len(pending), key)
        await self.handle(synth)

    async def _handle_inner(self, event: dict) -> None:
        # 提取事件信息
        text = event.get("text", "").strip()
        user_id = event.get("user_id")
        group_id = event.get("group_id")
        raw = event.get("raw", {})
        detail_type = raw.get("message_type", "private")
        # 🔴 群名片优先于 QQ 昵称（2026-09-20 深审）：群里用的一直是群名片「Aki」，
        # 只读 nickname 会拿到 QQ 昵称「Rezon」⇒ 主动发言张口叫错人。
        sender_name = sender_display_name(raw.get("sender"), user_id)

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
                    _fat_soft = int(getattr(self.core.config.autonomy, "fatigue_mention_soft", 6) or 6)
                    _fat_hard = int(getattr(self.core.config.autonomy, "fatigue_mention_hard", 8) or 8)
                    # `_fatigue_mood_allow` 去重：一个 5 分钟窗口只扣一次情绪分
                    # （否则每条被 @ 的消息都再扣，把 mood 推进 annoyed 象限）。
                    if cnt > _fat_soft and self._fatigue_mood_allow(group_id):
                        data_dir = str(self.core.config.persistence.data_dir)
                        get_mood_system(data_dir).trigger("overwhelmed", 0.5)
                        logger.info("社交疲劳触发：群%s 5分钟内被@%d次", group_id, cnt)
                        # 疲劳 gate：发简短回复并跳过完整 pipeline，避免被刷屏时每条都走 LLM
                        # C2：owner/管理员不受罐头硬闸（阈值可配）；软性 overwhelmed 情绪仍生效
                        if cnt > _fat_hard and not self._is_owner(int(user_id)):
                            # 冷却：同群 10 分钟只发一次（否则连刷时同句重复出站）
                            if self._tired_gate_allow(group_id):
                                _tired = random.choice(_TIRED_REPLIES)
                                try:
                                    await self.core.gateway.send(
                                        OutboundMsg("group", int(group_id), _tired))
                                except Exception:
                                    pass
                                _record_tired_fallback(self.core)
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
            # 🔴 取正文只走 `constants.event_text`（2026-09-25）：此前这里是
            # `raw_message or message`，而 `raw_message` **恒缺**（onebot11 只设
            # `message`）、`message` 是 CQ 原文 ⇒ 卡片/引用类私聊被
            # `CQ_TRANSPORT_RE` 整段剥空 ⇒ `memory_stage` 判空 `SKIP_REPLY`：
            # **无日志、无计数、无出站**（静默丢弃，生产实证 09-24 14:18:17）。
            text = event_text(event)

        # B1: 群聊防抖——同一用户 2s 内连续消息合并为一个语义回合
        _group_debouncer = getattr(self, "_group_debouncer", None)
        if detail_type == "group" and _group_debouncer is not None:
            assembled = await _group_debouncer.ingest(event)
            if assembled is None:
                return  # 已被合并，不单独处理
            event = assembled
            # 🔴 同上，且这条**覆盖**了 `:697` 群路径取的 `event["text"]` ⇒ 群里被
            # @ 的卡片/引用消息（json/forward/video/markdown/face/file）同样静默丢弃。
            text = event_text(event)

        # G1：跨群氛围观察 + 事实抽取（前置到回复闸门之前，对所有入站消息生效，
        # 这样"未 @bot 的群消息"也能学到氛围/事实）。氛围用本地启发式（零成本），
        # 事实抽取用 asyncio.create_task 后台跑，绝不拖慢回复。
        await self._observe_crossgroup(text, user_id, group_id, detail_type, sender_name)

        # 未 @ bot 的群消息同样进入仅内存、短 TTL 的群上下文；不写个人档案和向量库。
        # `addressed_to_bot` 用回复闸门的同一信号（`group_gate.is_direct_group_call`，
        # 即 @bot / 引用 bot / 正文喊「哈基米」），记录"这句是否直接对 bot 说"。
        # 用途：群缓冲里会同时存在「别人问 bot、且尚未被回复」的消息，与本轮要回的
        # 那条**同形**（都 role=user、都像在问 bot、中间没有 assistant 隔断），模型
        # 无法分辨 ⇒ 2026-09-21「引用锚正确、正文却在答上一条」的事故。注入侧据此标注。
        if detail_type == "group" and group_id is not None:
            try:
                from anima.memory.chat_memory import get_chat_memory
                get_chat_memory().observe_group_message(
                    int(group_id), int(user_id), sender_name, text, event.get("message_id"),
                    addressed_to_bot=is_direct_group_call(event, text),
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

        # 睡眠/免打扰/群策略/忽略名单/回复闸门/被动冷却/附件与语音合并
        # → pipeline 前置 stage（队列④第二刀，见 anima/pipeline/stages/prefilter.py）

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
        _meta = {
            "group_presence": event.get("group_presence", ""),
            "trace": trace,
            "_handler": self,  # 前置 stage 需要 handler 态（唤醒窗/冷却/忽略名单）
        }
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

        # 霸凌检测 → bullying_stage（队列④第二刀，见 anima/pipeline/stages/prefilter.py）

        # 执行流水线。收束窗口外连续到达的私聊仍可能有多个 handle 协程，
        # 必须在此串行化，确保后一回合读取到前一回合已写入的用户/助手记忆。
        # 记录生成起点时刻：防抖/闸门耗时不算「生成窗口」，用户在窗口内补的
        # 消息已被防抖合并；之后到达的才是需要新鲜度重估的。
        _turn_start_ts = time.time()
        async with self._turn_lock(detail_type, group_id, user_id):
            ctx = await self.pipeline.run(ctx)

        # 发送回复
        if not ctx.should_reply or not ctx.reply:
            if ctx.should_reply:
                # I4（2026-09-19）：应回复却无正文 —— 此前只写 audit/quality（均不进
                # journal），线上出现「ReAct 跑完却无出站」时事后不可归因。补一条
                # warning，让每个静默出口都能按 trace 定位（纯可观测，零行为变更）。
                logger.warning(
                    "[无出站][trace=%s] 应回复但正文为空：stop_reason=%s text_len=%d reply_len=%d",
                    trace, ctx.stop_reason or "-", len(ctx.text or ""), len(ctx.reply or ""),
                )
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

        # 记录该用户已回复（供被动回复冷却判定）。
        if detail_type == "group":
            try:
                self._mark_passive_replied(group_id, user_id)
            except Exception:
                logger.debug("mark passive replied failed", exc_info=True)

        # 出站消息段：@ 触发者 + 文本 + （可选）表情图 + （可选）生图，
        # 抽成纯函数以便单测（逻辑见 _build_reply_segments）。
        # 仅当对方真正「点到」了 bot（@ / 回复 / 点名）时才回 @；
        # 被动熟人接话 / 主动插话不 @，避免热群连续 @ 同一个人刷屏。
        at_trigger = True
        if detail_type == "group":
            try:
                # 🔴 不得在此局部 import `is_direct_group_call`：函数体内任一绑定都会让
                # Python 在编译期把它判为**整函数的局部名**，遮蔽模块级 L22 的导入，
                # 从而使更早的 `_handle_inner` 群上下文写入点（L873）抛 UnboundLocalError
                # （2026-09-22 P0 回归）。模块级导入即正解，守卫见
                # tests/test_no_local_import_shadowing.py。
                at_trigger = bool(is_direct_group_call(event, ctx.text))
            except Exception:
                at_trigger = False
        segments = _build_reply_segments(
            ctx, detail_type, user_id, group_id, self.core,
            sticker_auto_last=self._sticker_auto_last,
            at_trigger=at_trigger,
            message_id=event.get("message_id"),
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

        # 新鲜度重估：LLM 生成可能耗时数秒到数十秒，期间用户可能已改主意。
        # 首段发出前扫一遍生成窗口内同一用户的入站消息，命中撤销信号则整条
        # 让路——不占用群友注意力，也不回答已被取消的问题。仅群聊（渠道的
        # 入站缓冲按群组织）；渠道缺失（如内存桩测试）时静默跳过。
        # 必须在 quality.record_reply 之前：被弃答的回复不能计入回复统计。
        _channel = getattr(self.core.gateway, "channel", None)
        if (detail_type == "group" and _channel is not None
                and hasattr(_channel, "inbounds_since")):
            try:
                _stale = _inbound_matches(
                    _channel.inbounds_since(group_id, _turn_start_ts),
                    user_id, is_cancel_signal,
                )
            except Exception:
                logger.exception("新鲜度检查失败（忽略，按正常发送）")
                _stale = False
            if _stale:
                logger.info("[新鲜度][trace=%s] 生成期间用户撤销，丢弃在途回复", trace)
                quality = getattr(self.core, "reply_quality", None)
                if quality:
                    try:
                        quality.record_gate_skip("stale_dropped")
                    except Exception:
                        logger.debug("stale_dropped 遥测记录失败", exc_info=True)
                _audit = getattr(self.core, "audit", None)
                if _audit:
                    try:
                        _audit.emit("reply_stale_dropped", trace=trace,
                                    group_id=int(group_id) if group_id else None,
                                    user_id=int(user_id) if user_id else None)
                    except Exception:
                        pass
                return

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
            # 分段语境感知（P1 + 会话连续性）：发出后续段前看一眼上一段后的插话——
            # 撤销 → 静默停止；纠正 → 停止并让位给纠正后的新回复；调侃 → 自嘲收尾。
            if idx > 0 and context_aware:
                _intr = _classify_send_interrupt(
                    getattr(self.core.gateway, "channel", None), group_id, last_send_ts,
                    user_id=user_id,
                )
                if _intr == "cancel":
                    logger.info(
                        "[分段中断] 用户在段%d后撤销，停止后续发送 trace=%s", idx, trace
                    )
                    break
                if _intr == "correction":
                    logger.info(
                        "[分段中断] 用户在段%d后纠正，停止后续发送等新回复 trace=%s", idx, trace
                    )
                    break
                if _intr == "teasing":
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
                    if ctx.meta.get("promise_agenda_created"):
                        logger.debug("承诺跟进议程已由生成阶段创建，跳过 episodes 二次建档")
                    else:
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
        _auto = cfg.auto_collect
        try:
            from anima.cognitive.stickers import get_sticker_manager
            _rt = get_sticker_manager(self.core.config.persistence.data_dir).runtime_auto_collect()
            if _rt is not None:
                _auto = _rt
        except Exception:
            _auto = cfg.auto_collect
        if not (cfg.enabled and _auto):
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

    def _tired_gate_allow(self, group_id: Any) -> bool:
        """社交疲劳罐头的**同群冷却**判定；True = 允许本次发送。

        2026-09-21 深审：该出口此前无冷却，同一句话在 21:37:29 与 21:39:23 各发一次
        （中间群友仍在 @）。冷却期内**不发送**但仍 `return` —— 被刷屏时 owner 的设计
        就是"不回"，不是退回完整 pipeline 再把每条都喂给 LLM。

        fail-open：任何异常都放行（宁可多发一句，不可把回复路径卡住）。
        """
        try:
            now = time.time()
            if not hasattr(self, "_last_tired_at"):
                self._last_tired_at: dict = {}
            _grp = int(group_id) if group_id else 0
            last = float(self._last_tired_at.get(_grp, 0.0) or 0.0)
            if now - last < _TIRED_COOLDOWN_SEC:
                return False
            self._last_tired_at[_grp] = now
            return True
        except Exception:
            return True

    def _fatigue_mood_allow(self, group_id: Any) -> bool:
        """社交疲劳**情绪扣分**的同群去重；True = 允许本次扣分。

        2026-09-21：`cnt > soft` 之后每条被 @ 的消息都会再 `trigger("overwhelmed")`，
        一个 5 分钟窗口内可扣 4–5 次（valence −0.05 / arousal +0.1）⇒ 把 mood 从
        tired 象限（arousal<0.5）推进 **annoyed**（arousal>0.5），于是 C1 把回复
        砍到 120 字再补「算了不说了」。去重后每个窗口只扣一次。

        与 `_tired_gate_allow` 的 600s 冷却**独立**：那个管"别再发同一句罐头"，
        这个管"别重复扣情绪分"。

        fail-open：任何异常都放行（宁可多扣一次分，不可卡住消息处理路径）。
        """
        try:
            now = time.time()
            if not hasattr(self, "_fatigue_mood_at"):
                self._fatigue_mood_at: dict = {}
            _grp = int(group_id) if group_id else 0
            last = float(self._fatigue_mood_at.get(_grp, 0.0) or 0.0)
            if now - last < _FATIGUE_MOOD_COOLDOWN_SEC:
                return False
            self._fatigue_mood_at[_grp] = now
            return True
        except Exception:
            return True

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




def _extract_emotion_tag(text):
    """从回复文本剥除 &&情绪&& 标签，返回 (情绪, 去标签文本)；无标签返回 (None, 原文)。"""
    s = str(text or "")
    if not s or "&&" not in s:
        return None, text
    start = s.find("&&")
    end = s.find("&&", start + 2)
    if end < 0:
        return None, text
    emo = s[start + 2:end].strip()
    if not emo or len(emo) > 12:
        return None, text
    return emo, s[:start] + s[end + 2:]

def _build_reply_segments(ctx, detail_type, user_id, group_id, core,
                          sticker_auto_last: dict | None = None,
                          at_trigger: bool = True,
                          message_id=None) -> "list[dict]":
    """构造出站消息段：@ 触发者 + 文本 + （可选）表情图 + （可选）生图。

    at_trigger：是否前插 @ 段。仅当对方真正「点到」了 bot（真 @ / 回复引用 /
    点名「哈基米」）时才应回 @；被动熟人接话 / 主动插话不应 @，否则热群会
    连续 @ 同一个人刷屏。
    

    表情附带规则（修复前 bug：群聊普通氛围下 LLM 已 pick_sticker 选了图却发不出）：
      - LLM 主动选了表情（ctx.sticker_to_send 有值）-> 尊重其意图，群聊/私聊都附带发出；
        不再受 always_atmosphere 限制，否则会出现「LLM 明明选了图却因群氛围被吞掉」。
      - 否则，仅当群聊且当前氛围命中 always_atmosphere 白名单时，被动随机带一张活跃气氛。
    NapCat 不保证以 bot 工作目录解析相对路径，统一传绝对路径。
    """
    segments: list[dict] = []
    logger.debug("[IMGDBG] _build_reply_segments 进入 detail_type=%s gen_paths=%d",
                detail_type, len(getattr(ctx, "generated_image_paths", None) or []))
    # &&情绪&& 标签：读出并自回复文本剥除（stealer 被动标签，不污染出站文本）
    _tag_emotion = None
    if ctx.reply and "&&" in ctx.reply:
        _tag_emotion, cleaned = _extract_emotion_tag(ctx.reply)
        if (cleaned or "").strip():
            ctx.reply = cleaned
        else:
            # M2.2：整条回复只是 &&标签&&（stealer 被动标签），剥净后为空。
            # 别静默不发送（否则 should_reply 又不落任何 trace）——发一句轻量兜底
            # 并在日志留痕，热群不显凭空不响应。
            logger.warning("回复剥标签后为空(M2.2): emotion=%r，发轻量兜底", _tag_emotion)
            ctx.reply = "喵"
    # 引用(quote)优先：凡在回复某条消息(有 message_id)即前插 reply 段——含被动接话，
    # 让回复锚定上下文、热群不显凭空插话；引用本身会通知被引用者。
    if detail_type == "group" and message_id is not None:
        segments.append({"type": "reply", "data": {"id": str(message_id)}})
    # @ 兜底：仅真点到且拿不到可引用消息时用 @；已有引用时不再重复 @，避免同消息双提醒。
    if detail_type == "group" and at_trigger and message_id is None:
        segments.append({"type": "at", "data": {"qq": str(user_id)}})
    segments.append({"type": "text", "data": {"text": ctx.reply}})
    scfg = getattr(core.config, "sticker", None)
    send_allowed = True
    if detail_type == "group" and scfg is not None:
        rules = {f"group:{group_id}", f"user:{user_id}"}
        try:
            from anima.cognitive.stickers import get_sticker_manager
            _rtbl = list(get_sticker_manager(core.config.persistence.data_dir).runtime_blocklist() or [])
        except Exception:
            _rtbl = []
        blocked = set((getattr(scfg, "send_blocklist", []) or []) + _rtbl)
        allowed = set(getattr(scfg, "send_allowlist", []) or [])
        send_allowed = not bool(rules & blocked) and (not allowed or bool(rules & allowed))
    sticker_path = getattr(ctx, "sticker_to_send", None)
    if sticker_path and send_allowed:
        # LLM 主动选了表情 -> 群聊/私聊都发，尊重意图（不受氛围/概率门控）。
        # 表情包走「表情渠道」：image subType=1 让 NapCat 按 QQ 表情包小图渲染，
        # 而非普通大图。small_emoji_channel=False 时退回普通图片。
        _img = {"file": str(Path(sticker_path).resolve())}
        _emic = scfg is not None and bool(getattr(scfg, "small_emoji_channel", True))
        if _emic:
            _img["subType"] = "1"
        segments.append({"type": "image", "data": _img})
    elif detail_type == "group" and send_allowed:
        # 被动自动附带：概率/冷却模式 + 情绪联动（E1/E2）。
        # mood 非中性时优先用 mood label 选图（70%），文本推断作 fallback。
        try:
            from anima.cognitive.stickers import get_sticker_manager
            from anima.cognitive.mood import get_mood_system
            scfg = core.config.sticker
            # 意图门控（对标 stealer）：严肃/服务氛围不加被动表情。
            # 注意：仅跳过「被动表情」，绝不影响用户明确要的生图段（generated_image_paths）。
            # 曾因「formal 氛围 return 整个函数」导致 3 张抓图被吞、只发文本。
            _ig_gate = bool(getattr(scfg, "intent_gate", False)) and \
                str(getattr(ctx, "atmosphere_style", "")).strip() in set(
                    getattr(scfg, "intent_gate_serious_atmospheres", []) or [])
            if _ig_gate:
                logger.debug("[IMGDBG] 表情 intent_gate 命中（氛围=%s），仅跳过被动表情，生图段保留",
                            getattr(ctx, "atmosphere_style", ""))
                # 确保后续 E2/R3 引用的变量已定义且不会触发选图（probability=0）
                sticker_auto_last = sticker_auto_last if sticker_auto_last is not None else {}
                now = time.time()
                last = sticker_auto_last.get(str(group_id), 0.0)
                probability = 0.0
                cooldown = 0.0
                by_emotion = False
            if not _ig_gate:
                sticker_auto_last = sticker_auto_last if sticker_auto_last is not None else {}
                now = time.time()
                last = sticker_auto_last.get(str(group_id), 0.0)
                if hasattr(scfg, "send_probability"):
                    probability = float(scfg.send_probability)
                    try:
                        from anima.cognitive.stickers import get_sticker_manager
                        _rprob = get_sticker_manager(core.config.persistence.data_dir).runtime_send_probability()
                    except Exception:
                        _rprob = None
                    if _rprob is not None:
                        probability = float(_rprob)
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
                # stealer &&情绪&& 被动标签：回复显式点名要的情绪，直接按它选图（已从回复文本剥离）
                if _auto is None and _tag_emotion:
                    try:
                        _auto = _mgr.pick_by_emotion(
                            _tag_emotion, group_id=group_id,
                            allow_cross_group=bool(getattr(scfg, "share_across_groups", False)))
                    except Exception:
                        _auto = None
                # fallback: 文本情绪推断（先走自然语言分析器，再退关键词计票）
                if _auto is None and by_emotion:
                    _em = ctx.reply
                    try:
                        from anima.cognitive.sticker_emotion import detect_emotion_name
                        _em = detect_emotion_name(_em) if _em else None
                    except Exception:
                        _em = None
                    if not _em:
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
                    # 被动带图同样走表情渠道 subType=1（与 LLM 主动选表情一致）
                    _auto_img = {"file": str(Path(_auto).resolve())}
                    _auto_emic = scfg is not None and bool(getattr(scfg, "small_emoji_channel", True))
                    if _auto_emic:
                        _auto_img["subType"] = "1"
                    segments.append({"type": "image", "data": _auto_img})
        except Exception:
            logger.exception("被动自动表情选取失败（忽略）")
    # 生图出站：本回合工具（x_search / run_python / owner 工具）下载的全部配图，
    # 去重保序后逐张发出（每张独立成一条消息，见 _split_reply_messages）。
    # 旧逻辑 [:1] 只发第 1 张，导致「拿其他图却一直发同一张」。
    # 单条回复附带图片上限 MAX_GENERATED_IMAGES_PER_REPLY，避免刷屏 / QQ 段爆炸。
    _gen_seen = set()
    _gen_paths: list[str] = []
    _ctx_gen = getattr(ctx, "generated_image_paths", None) or []
    logger.debug("[IMGDBG] build_reply_segments 读取 ctx.generated_image_paths=%d 条", len(_ctx_gen))
    for image_path in _ctx_gen:
        _s = str(image_path)
        if _s and _s not in _gen_seen:
            _gen_seen.add(_s)
            _gen_paths.append(_s)
        if len(_gen_paths) >= MAX_GENERATED_IMAGES_PER_REPLY:
            break
    for image_path in _gen_paths:
        # 鲁棒性：文件可能已不存在（磁盘清理/进程切换），失效路径跳过，不硬发。
        try:
            if not Path(image_path).is_file():
                logger.warning("[IMGDBG] 生图路径失效已跳过（文件不存在）: %s", image_path)
                continue
        except Exception:
            # 路径异常（含非法字符等）同样跳过，避免发送层因坏路径报错
            logger.warning("[IMGDBG] 生图路径异常已跳过: %s", image_path)
            continue
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
    reply_seg: dict | None = None
    text_content = ""
    image_segs: list[dict] = []
    for seg in segments:
        if seg.get("type") == "reply":
            if reply_seg is None:
                reply_seg = seg
        elif seg.get("type") == "at":
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
        # 引用段只加在第一段(引用需在正文前, Q<qq>收起通知定位到被引用消息)
        if i == 0 and reply_seg is not None:
            msg_segs.append(reply_seg)
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


def _classify_send_interrupt(channel: "Any", group_id: "int | None", since_ts: float,
                             user_id: "int | None" = None) -> "str | None":
    """上一分段后的最新插话分类：cancel / correction / teasing / None。

    撤销/纠正只认**同一用户**——跨用户无法取消别人正在接收的回复
    （2026-09-06 复审 R8：原实现任意群成员可在分段间隙打断他人回复）。
    调侃保留任意群成员语义：对 bot 的社会性打断（"闭嘴/别说了"）属合法
    反馈，且自嘲回应有界。私聊（group_id=None）或渠道无入站缓冲时 None。
    """
    if channel is None or not hasattr(channel, "latest_inbound_since"):
        return None
    intr = channel.latest_inbound_since(group_id, since_ts)
    if not intr:
        return None
    uid, text = intr[0], intr[1] or ""
    if is_cancel_signal(text) or is_correction_signal(text):
        if user_id is None:
            return None
        try:
            if int(uid) != int(user_id):
                return None
        except (TypeError, ValueError):
            return None
        return "cancel" if is_cancel_signal(text) else "correction"
    if is_teasing_interrupt(text):
        return "teasing"
    return None


def _should_interrupt_for_teasing(channel: "Any", group_id: "int | None", since_ts: float) -> bool:
    """分段发送中，若用户在上一分段后插话调侃，返回 True（保留旧接口）。"""
    return _classify_send_interrupt(channel, group_id, since_ts) == "teasing"


def _inbound_matches(inbounds: "Any", user_id: "Any", predicate) -> bool:
    """inbounds 为 [(user_id, text)]：任一同一用户的消息命中谓词即 True。"""
    try:
        _uid = int(user_id)
    except (TypeError, ValueError):
        return False
    for uid, txt in inbounds or []:
        try:
            if int(uid) == _uid and predicate(txt or ""):
                return True
        except (TypeError, ValueError):
            continue
    return False


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
