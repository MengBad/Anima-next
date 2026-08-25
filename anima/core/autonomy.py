"""自主思考心跳 —— 定期内省、产生主动发言冲动。

设计（基于 §6 设计规格）：
- Heartbeat Tick ~30s → emit "heartbeat.tick" → Cognitive Core 评估
- Intent: think|speak|act
- speak: 人类频率闸门（每群独立冷却 + 比例 + 预算）
- act: Tool call
- 断路熔断: 连续 N 次 LLM 失败 → 暂时退化为纯应答模式
"""

from __future__ import annotations
import asyncio
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Optional

from anima.core.gateway import OutboundMsg
from anima.core.persistence import PersistenceProvider

_BJ = timezone(timedelta(hours=8))  # 北京时间

if TYPE_CHECKING:
    from anima.core.anima_core import AnimaCore

logger = logging.getLogger("anima.core.autonomy")


def is_user_avoided(data_dir: str, user_id: int, now: float | None = None) -> bool:
    """该用户当前是否处于伤痕回避期（供自主发言门控复用）。"""
    from anima.cognitive.scar import get_scar_system
    try:
        return get_scar_system(data_dir).is_avoiding(user_id, now)
    except Exception:
        return False


# 群上下文为方便模型分辨说话人会使用「[名字]: 内容」格式。主动发言的
# 回复本身不是转述，绝不能把这个上下文标记一起发出去。
_ACTIVE_REPLY_LABEL_RE = re.compile(
    r"^\s*(?:\[(?:你|哈基米|anima|Anima|bot)\]|(?:你|哈基米|anima))\s*[:：]\s*",
    re.IGNORECASE,
)
_ACTIVE_LOW_VALUE_RE = re.compile(
    r"(群里怎么突然这么安静|是不是都在偷偷|键盘被|梦游敲键盘|大半夜的怎么突然发|"
    r"别老是喊个名字就没下文|本喵的猫罐头还没着落|发乱码|被别的猫踩了|"
    r"睡大觉|去睡觉|快去睡|早点睡|赶紧睡|乖乖睡觉|该睡觉了|睡觉舒服|快去乖乖睡)"
)


# CQ 码清理（统一引用 anima.constants，含未闭合兜底）
from anima.constants import CQ_TRANSPORT_RE as _CQ_TRANSPORT_RE


def _clean_active_reply(reply: str) -> str:
    """移除主动发言中被模型照抄的上下文说话人标签及 CQ 码。"""
    text = _ACTIVE_REPLY_LABEL_RE.sub("", (reply or "").strip(), count=1).strip()
    text = re.sub(
        r"(?:^|\n)\s*\[(?:你|哈基米|anima|Anima)\]\s*[:：]\s*",
        "\n", text, flags=re.IGNORECASE,
    ).strip()
    # 剥离残留 CQ 码（LLM 可能照抄议程中的 CQ 片段）
    text = _CQ_TRANSPORT_RE.sub("", text).strip()
    return text


def _normalized_active_reply(reply: str) -> str:
    """用于主动发言去重的宽松规范化文本。"""
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", _clean_active_reply(reply)).lower()


def _is_low_value_active_reply(reply: str) -> bool:
    """拦截主动发言里常见的空转水话（刷存在感、骂乱码、问安静）。"""
    text = _clean_active_reply(reply)
    if not text:
        return True
    if _ACTIVE_LOW_VALUE_RE.search(text):
        return True
    # 过短且不含任何实质内容
    if len(text) < 6:
        return True
    return False


_OPEN_QUESTION_RE = re.compile(
    r"(?:[?？]|为什么|为啥|怎么(?:办|弄|说)?|咋(?:办|整)?|谁知道|有没有|能不能|可不可以|吗[啊呀呢]?$)"
)
_CASUAL_HOOK_RE = re.compile(r"(?:哈哈|笑死|绷不住|离谱|绝了|逆天|救命|好家伙|乐了)")
_NOISE_ONLY_RE = re.compile(r"^[\s?？!！.。,，~～…·\d]+$")


def score_active_context(history: list[dict]) -> float:
    """评估群聊是否存在值得主动接住的近期语境，返回 0~1。

    只使用短时群缓冲，不读取跨群资料。开放问题权重最高；单纯热闹但没有
    接话点只给低分；最后一句是 bot 自己时强降权，避免自问自答。
    """
    usable = [m for m in history if str(m.get("content") or "").strip()]
    if not usable:
        return 0.0
    recent = usable[-12:]
    human = [m for m in recent if m.get("role") == "user"]
    if not human:
        return 0.0

    score = min(0.18, len(human) * 0.03)
    last = recent[-1]
    last_text = str(last.get("content") or "").strip()
    if last.get("role") != "user":
        score -= 0.40
    else:
        if _NOISE_ONLY_RE.match(last_text) or len(last_text) < 2:
            return 0.0
        if _OPEN_QUESTION_RE.search(last_text):
            score += 0.58
        if _CASUAL_HOOK_RE.search(last_text):
            score += 0.22
        if 6 <= len(last_text) <= 80:
            score += 0.08
        # 纯表情/极短灌水不值得插话
        if len(last_text) <= 3 and not _OPEN_QUESTION_RE.search(last_text):
            score -= 0.25

    # 多人连续聊天说明这是活的语境，但不足以单独触发主动发言。
    distinct_speakers = set()
    for message in human[-8:]:
        speaker = str(message.get("name") or message.get("sender_name")
                      or message.get("user_id") or "")
        content = str(message.get("content") or "")
        if not speaker and content.startswith("[") and "]:" in content:
            speaker = content[1:content.index("]: ") if "]: " in content else content.index("]:")]
        if speaker:
            distinct_speakers.add(speaker)
    if len(distinct_speakers) >= 2:
        score += 0.06
    return max(0.0, min(1.0, score))

# 昼夜节律活跃度（0-23 时）
HOURLY_ACTIVITY = {
    0: 0.15, 1: 0.10, 2: 0.05, 3: 0.05, 4: 0.10, 5: 0.15,
    6: 0.30, 7: 0.45, 8: 0.60, 9: 0.75, 10: 0.85, 11: 0.90,
    12: 0.80, 13: 0.70, 14: 0.85, 15: 0.90, 16: 0.85, 17: 0.75,
    18: 0.70, 19: 0.65, 20: 0.55, 21: 0.45, 22: 0.30, 23: 0.20,
}


# ---------------------------------------------------------------------------
# 自身状态刷新（P3）：每小时核对群头衔/名片，变更写 self_notes
# ---------------------------------------------------------------------------

def compute_self_status_notes(
    prev_title: str, new_title: str, prev_card: str, new_card: str, group_id: int
) -> "list[str]":
    """比较自身在群内的头衔/名片前后状态，返回需写入 self_notes 的文本列表。

    仅在发生实际变化时返回条目；首跑（prev 为空）由调用方决定是否跳过，避免刷屏。
    """
    notes: list[str] = []
    if prev_title != new_title:
        if new_title:
            notes.append(f"群 {group_id} 当前头衔：【{new_title}】")
        else:
            notes.append(f"群 {group_id} 头衔被清空了")
    if prev_card != new_card:
        if new_card:
            notes.append(f"群 {group_id} 当前群名片：【{new_card}】")
        else:
            notes.append(f"群 {group_id} 群名片被清空了")
    return notes


def _decide_morning_dream_share(
    hour: int, last_morning_date: str, today: str, rng: random.Random | None = None,
) -> "tuple[bool, bool]":
    """A3 纯函数：仅在 08:00、当天未尝试时以 10% 概率决定分享梦境。

    返回 (should_share, attempted)：
    - should_share=True 时调用方应在分享成功后标记 _last_morning_dream_date=today；
    - attempted=True 表示「08:00 且今天已抽过」——调用方应标记尝试，避免每 tick 重复抽。
    """
    if hour != 8:
        return (False, False)
    if last_morning_date == today:
        return (False, False)
    if (rng or random).random() >= 0.1:
        return (False, True)   # 抽不中，今天已尝试
    return (True, False)       # 抽中分享（成功后再标记）


def _constitution_drive(mood_arousal, mood_valence, relation_depth=0.2, threat=0.0):
    """T1.1 轻量宪法桥：用可见 runtime 信号沉淀最小 Memory → Constitution M1 链 → Drive dict。

    Constitution 只产出趋近/回避取向(Drive)，不改任何出站语言，只可能压制主动表达。
    任何一步失败返回 None(fail-closed)。
    """
    try:
        import anima.constitution.state as _C
        from anima.constitution.identity import derive_identity
        from anima.constitution.need import derive_need
        from anima.constitution.affect import derive_affect
        from anima.constitution.drive import derive_drive
    except Exception:
        logger.debug("constitution 引擎不可用，跳过 Drive 桥", exc_info=True)
        return None
    try:
        _mem = _C.Memory(
            semantic=[_C.MemoryRecord(
                kind="semantic",
                content={"valence": round(float(mood_valence), 3),
                         "arousal": round(float(mood_arousal), 3)},
                salience=min(1.0, abs(float(mood_valence))),
            )],
            relations=[_C.MemoryRecord(
                kind="relation",
                content={"depth": round(float(relation_depth), 3)},
                salience=min(1.0, float(relation_depth)),
            )],
        )
        _percept = _C.Perception(signals={
            "novelty": max(0.0, float(mood_arousal)),
            "threat": max(0.0, float(threat)),
        })
        _drive = derive_drive(
            derive_affect(derive_need(derive_identity(_mem)), _percept)
        )
        _ori = _drive.orientation
        return {
            "activated": bool(_drive.activated),
            "approach": float(_ori.get(_C.OrientationAxis.APPROACH, 0.0)),
            "avoid": float(_ori.get(_C.OrientationAxis.AVOID, 0.0)),
            "intensity": float(_ori.get(_C.OrientationAxis.INTENSITY, 0.0)),
        }
    except Exception:
        logger.debug("Drive 推导失败，fail-closed 视为无驱动的主动", exc_info=True)
        return None


def _drive_gate_ok(drive) -> bool:
    """T1.1 主动表达 Drive 门：须呈现且已激活、且趋近>回避，否则 fail-closed 压制。"""
    if not drive or not isinstance(drive, dict):
        return False
    if not drive.get("activated"):
        return False
    return float(drive.get("approach", 0.0)) > float(drive.get("avoid", 0.0))

class Heartbeat:
    """自主心跳引擎。"""

    def __init__(self, core: "AnimaCore",
                 tick_sec: float = 30.0,
                 cooldown_sec: int = 90,
                 daily_max: int = 20,
                 ratio_max: float = 0.30,
                 token_budget: int = 0,
                 active_reply_prob: float = 0.04,
                 active_silence_min_sec: int = 600,
                 active_global_cooldown_sec: int = 1800,
                 active_min_group_messages: int = 12,
                 active_daily_max_per_group: int = 3,
                 active_context_score_threshold: float = 0.55,
                 active_feedback_window_sec: int = 900,
                 active_feedback_min_samples: int = 4,
                 active_feedback_min_acceptance: float = 0.20,
                 data_dir: str = "data"):
        self.core = core
        self.tick_sec = tick_sec
        # R16: periodic anomaly self-check throttle + dedupe
        self._last_anomaly_check = 0.0
        self._last_anomaly_signature = None
        self._anomaly_check_interval = 300.0
        self.cooldown_sec = cooldown_sec
        self.daily_max = daily_max
        self.ratio_max = ratio_max
        self.token_budget = token_budget
        self._tokens_today = 0

        # 主动发言策略（配合沉默门控 + 全局冷却，避免刷屏）
        self.active_reply_prob = active_reply_prob
        self.active_silence_min_sec = active_silence_min_sec
        self.active_global_cooldown_sec = active_global_cooldown_sec
        self.active_min_group_messages = active_min_group_messages
        self.active_daily_max_per_group = active_daily_max_per_group
        self.active_context_score_threshold = active_context_score_threshold
        self.active_feedback_window_sec = max(60, int(active_feedback_window_sec))
        self.active_feedback_min_samples = max(1, int(active_feedback_min_samples))
        self.active_feedback_min_acceptance = max(0.0, min(1.0, float(active_feedback_min_acceptance)))

        self._task: Optional[asyncio.Task] = None
        self._running = False

        # 每群独立追踪
        self._group_last_speak: dict[int, float] = {}
        self._group_speak_count: dict[int, int] = {}
        self._group_total_msgs: dict[int, int] = {}
        self._group_last_msg: dict[int, float] = {}   # 最近一次人类发言时间
        self._last_self_status_refresh: float = 0.0
        self._self_status_cache: dict[int, dict] = {}   # group_id -> {title, card}
        self._last_active_speak: float = 0.0           # 最近一次主动发言时间
        # 只在当前进程保留最近主动发言。用于避免模型围绕同一个梗短时间复读，
        # 不写入用户记忆，也不影响正常的被动回复。
        self._recent_active_replies: dict[int, list[tuple[float, str]]] = {}
        self._active_feedback_pending: dict[int, dict[str, object]] = {}
        self._active_feedback_stats: dict[int, dict[str, int]] = {}
        self._day_reset = time.time()

        # 熔断
        self._consecutive_failures = 0
        self._max_failures = 5
        self._degraded = False

        # 持久化
        self._persist = PersistenceProvider(data_dir)
        self._path = "heartbeat.json"
        self.restore()

        # 反刍引擎
        from anima.cognitive.rumination import RuminationEngine
        self._rumination = RuminationEngine(self.core, data_dir=data_dir)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def restore(self) -> None:
        """从磁盘恢复每日计数与冷却状态。"""
        data = self._persist.read_json(self._path, {})
        self._group_last_speak = {
            int(k): v for k, v in data.get("group_last_speak", {}).items()
        }
        self._group_speak_count = {
            int(k): v for k, v in data.get("group_speak_count", {}).items()
        }
        self._group_total_msgs = {
            int(k): v for k, v in data.get("group_total_msgs", {}).items()
        }
        self._group_last_msg = {
            int(k): v for k, v in data.get("group_last_msg", {}).items()
        }
        self._last_active_speak = data.get("last_active_speak", 0.0)
        self._active_feedback_pending = {
            int(k): dict(v) for k, v in (data.get("active_feedback_pending", {}) or {}).items()
            if isinstance(v, dict)
        }
        self._active_feedback_stats = {
            int(k): {
                "engaged": int((v or {}).get("engaged", 0) or 0),
                "ignored": int((v or {}).get("ignored", 0) or 0),
            }
            for k, v in (data.get("active_feedback_stats", {}) or {}).items()
            if isinstance(v, dict)
        }
        self._day_reset = data.get("day_reset", time.time())
        self._tokens_today = data.get("tokens_today", 0)
        # 恢复近期自主发言记录（跨重启去重）
        raw_replies = data.get("recent_active_replies", {})
        now = time.time()
        self._recent_active_replies = {
            int(k): [tuple(item) for item in v if now - item[0] < 14400]
            for k, v in raw_replies.items()
            if isinstance(v, list)
        }

    def persist(self) -> None:
        """保存每日计数与冷却状态。"""
        data = {
            "group_last_speak": {str(k): v for k, v in self._group_last_speak.items()},
            "group_speak_count": {str(k): v for k, v in self._group_speak_count.items()},
            "group_total_msgs": {str(k): v for k, v in self._group_total_msgs.items()},
            "group_last_msg": {str(k): v for k, v in self._group_last_msg.items()},
            "last_active_speak": self._last_active_speak,
            "active_feedback_pending": {str(k): v for k, v in self._active_feedback_pending.items()},
            "active_feedback_stats": {str(k): v for k, v in self._active_feedback_stats.items()},
            "day_reset": self._day_reset,
            "tokens_today": self._tokens_today,
            "recent_active_replies": {
                str(k): v[-8:] for k, v in getattr(self, "_recent_active_replies", {}).items()
            },
        }
        self._persist.write_json(self._path, data)

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop())
        try:
            self.core.health_history.record(force=True)
        except Exception:
            logger.exception("心跳健康初始采样失败")
        logger.info("自主心跳已启动: tick=%.1fs cooldown=%ds daily=%d ratio=%.0f%%",
                     self.tick_sec, self.cooldown_sec, self.daily_max, self.ratio_max * 100)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try: await self._task
            except asyncio.CancelledError: pass

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.tick_sec)
            try:
                await self._tick()
            except Exception:
                logger.exception("心跳 tick 异常")

    async def _refresh_self_status(self) -> None:
        """每小时刷新 bot 在各群的专属头衔/群名片，变更则写入 self_notes（P3）。"""
        channel = getattr(getattr(self.core, "gateway", None), "channel", None)
        if channel is None or not hasattr(channel, "get_group_member_info"):
            return
        self_id = getattr(channel, "self_id", None)
        if not self_id:
            return
        groups = [int(g) for g in getattr(self, "_group_last_msg", {}).keys()]
        for gid in groups:
            try:
                info = await channel.get_group_member_info(gid, int(self_id))
            except Exception:
                logger.debug("刷新自身状态失败 group=%s", gid, exc_info=True)
                continue
            if not isinstance(info, dict):
                continue
            new_title = (info.get("title") or "").strip()
            new_card = (info.get("card") or "").strip()
            prev = self._self_status_cache.get(gid)
            if prev is None:
                # 首跑只种基线，不刷屏
                self._self_status_cache[gid] = {"title": new_title, "card": new_card}
                continue
            notes = compute_self_status_notes(
                prev.get("title", ""), new_title, prev.get("card", ""), new_card, gid
            )
            if notes:
                try:
                    from anima.cognitive.learning import get_learning
                    data_dir = str(self.core.config.persistence.data_dir)
                    for n in notes:
                        get_learning(data_dir).add_note(n, delta=0.05, category="self_correction")
                    logger.info("[自身状态] group=%s 更新 %d 条", gid, len(notes))
                except Exception:
                    logger.exception("自身状态写入 self_notes 失败")
            self._self_status_cache[gid] = {"title": new_title, "card": new_card}

    def _anomaly_selfcheck(self) -> None:
        # R16 Production Reliability: derive 24h anomaly report and record
        # severe findings into audit. Read-only, notification-only.
        now = time.time()
        if now - self._last_anomaly_check < self._anomaly_check_interval:
            return
        self._last_anomaly_check = now
        hh = getattr(self.core, "health_history", None)
        if hh is None or not hasattr(hh, "anomaly_report"):
            return
        try:
            report = hh.anomaly_report(hours=24.0)
        except Exception:
            logger.exception("anomaly_report failed, isolated")
            return
        severe = [a for a in report.get("anomalies", [])
                  if a.get("type") in ("process_restart", "channel_disconnect",
                                       "watchdog_lag", "llm_circuit_open")]
        if not severe:
            return
        items = sorted((a["type"], a.get("count", 0)) for a in severe)
        sig = repr(items)
        if sig == self._last_anomaly_signature:
            return
        self._last_anomaly_signature = sig
        audit = getattr(self.core, "audit", None)
        if audit is not None:
            audit.emit("runtime_anomaly_detected",
                       anomalies=items,
                       summary=report.get("summary"),
                       samples=report.get("samples"),
                       latest_running=bool(report.get("latest", {}).get("running")),
                       latest_connected=bool(report.get("latest", {}).get("channel_connected")))

    def _config_drift_check(self) -> None:
        # R16 闭环4：周期比对敏感配置/权限基线。仅只读告警进审计，绝不自动回滚。
        monitor = getattr(self.core, "config_drift", None)
        if monitor is None:
            return
        audit = getattr(self.core, "audit", None)
        try:
            monitor.maybe_emit(self.core.config, audit)
        except Exception:
            logger.exception("config drift check failed, isolated")

    async def _tick(self) -> None:

        self._expire_active_feedback(time.time())
        try:
            self.core.health_history.record()
        except Exception:
            logger.exception("心跳健康采样失败")
        # R16: periodic anomaly self-check (read-only + dedupe + audit)
        try:
            self._anomaly_selfcheck()
        except Exception:
            logger.exception("心跳异常自检失败，已隔离")
        # R16 闭环4: periodic config/permission drift check (read-only + audit)
        try:
            self._config_drift_check()
        except Exception:
            logger.exception("心跳配置漂移自检失败，已隔离")
        try:
            backups = getattr(self.core, "backups", None)
            if backups is not None:
                result = backups.maintain()
                if result.get("created"):
                    audit = getattr(self.core, "audit", None)
                    if audit:
                        audit.emit("scheduled_state_backup_created", backup_id=result["created"]["id"])
        except Exception:
            logger.exception("运行状态备份维护失败，已隔离")
        # 模拟生活节律（最先执行）
        await self._life_checkpoints()

        # Sylann 的无输入 tick 与 Anima 心跳共用同一调度器。引擎自身会按会话
        # 最小间隔合并，适配层只保存摘要，绝不绕过 Agenda/权限直接发 QQ 消息。
        sylann = getattr(self.core, "sylann", None)
        if sylann is not None:
            try:
                observations = await sylann.tick()
                if observations:
                    audit = getattr(self.core, "audit", None)
                    if audit:
                        audit.emit("sylann_tick", count=len(observations))
            except Exception:
                logger.exception("Sylann 心跳推进失败，已隔离")

        # 熔断检查
        if self._degraded:
            await self._check_recovery()
            self.persist()
            return

        # 日预算重置
        now = time.time()
        # 每日历事件只入队，不绕过 Agenda 的冷却、预算和发送确认。
        calendar = getattr(self.core, "calendar", None)
        if calendar is not None:
            for event in [*calendar.due(now), *calendar.weekly_due(now)]:
                workflows = getattr(self.core, "workflows", None)
                run = workflows.trigger("calendar", event.id, event.group_id, event.text,
                                        event_key=str(getattr(event, "last_triggered_day", ""))) if workflows else None
                agenda_item = self.core.propose_agenda(event.group_id, event.text, priority=0.9,
                                                       expires_at=now + 6 * 3600, source="calendar")
                if run:
                    workflows.bind_intent(run.id, getattr(agenda_item, "intent_id", ""))
                audit = getattr(self.core, "audit", None)
                if audit:
                    audit.emit("calendar_event_enqueued", calendar_id=event.id,
                               group_id=event.group_id)
        if datetime.fromtimestamp(now, _BJ).date() != datetime.fromtimestamp(self._day_reset, _BJ).date():
            self._group_speak_count.clear()
            self._group_total_msgs.clear()
            self._group_last_speak.clear()
            self._tokens_today = 0
            self._day_reset = now
            # 每日低峰：网页知识库置信衰减与过期清理（P1，失败隔离）
            try:
                from anima.memory.web_knowledge import WebKnowledgeStore
                data_dir = str(self.core.config.persistence.data_dir)
                removed = WebKnowledgeStore(data_dir).decay()
                if removed:
                    logger.info("WebKnowledgeStore 每日衰减清理 %d 条过期知识", removed)
            except Exception as e:
                logger.warning("WebKnowledgeStore 每日衰减失败（已忽略）: %s", e)
            # R16 闭环5：每日低峰清理过期审计文件（整文件粒度），保持可回溯窗口
            # 连续不断档，同时防止小盘 VPS 上审计日志无限膨胀。
            try:
                audit = getattr(self.core, "audit", None)
                if audit is not None:
                    removed_audit = audit.prune(retention_days=90)
                    if removed_audit:
                        logger.info("audit 保留期清理 %d 个过期审计文件", removed_audit)
            except Exception as e:
                logger.warning("audit 清理失败（已忽略）: %s", e)

        # Token 预算检查
        if self.token_budget and self._tokens_today >= self.token_budget:
            logger.debug("Token 日预算已耗尽")
            self.persist()
            return

        # 低频运维清理：僵尸 selfcode / 粘滞笔记（约每 2 小时一次，失败隔离）
        try:
            last_cleanup = float(getattr(self, "_last_ops_cleanup", 0.0) or 0.0)
            if now - last_cleanup >= 7200:
                self._last_ops_cleanup = now
                from anima.cognitive.ops_cleanup import run_ops_cleanup
                data_dir = str(self.core.config.persistence.data_dir)
                # 运行中只清僵尸任务与玩具欲望，不反复砍笔记（笔记由启动/CLI 一次清）
                rep = run_ops_cleanup(
                    data_dir=data_dir,
                    cancel_all_running_selfcode=True,
                    purge_notes=False,
                    purge_world=False,
                    resolve_toy_desires=True,
                )
                if rep.cancelled_selfcode or rep.resolved_desires:
                    logger.info(
                        "ops_cleanup: cancelled_selfcode=%d resolved_desires=%d",
                        rep.cancelled_selfcode, rep.resolved_desires,
                    )
                # SelfCode 效果评估：只标记建议卸载，永不自动删除
                try:
                    if bool(getattr(self.core.config.self_code, "enabled", False)):
                        from anima.cognitive.selfcode import get_selfcode_manager
                        sug = get_selfcode_manager(self.core).evaluate_tools()
                        if sug:
                            logger.info(
                                "selfcode evaluate: %d tool(s) suggest uninstall",
                                len(sug),
                            )
                except Exception:
                    logger.debug("selfcode evaluate skipped", exc_info=True)
        except Exception:
            logger.exception("ops_cleanup 心跳清理失败（已隔离）")

        # 每小时刷新自身群状态（头衔/名片），变更写 self_notes（P3）
        try:
            if now - float(getattr(self, "_last_self_status_refresh", 0.0) or 0.0) >= 3600:
                self._last_self_status_refresh = now
                await self._refresh_self_status()
        except Exception:
            logger.exception("自身状态刷新失败（已隔离）")

        # 自主发现路径（不依赖是否找到可主动发言的群）：
        # 故障工单扫描 + 自升级熔断/提案。原先挂在 _pick_group 成功之后，
        # 群沉默时整段永不运行，导致 SelfCode 洪水无法被心跳侧熔断。
        dcfg = getattr(self.core.config, "danger", None)
        if dcfg and getattr(dcfg, "self_improvement", False) \
                and getattr(dcfg, "self_improvement_confirm", False):
            try:
                from anima.cognitive.danger import get_danger_behaviors
                danger = get_danger_behaviors(core=self.core, cfg=dcfg)
                if danger is not None:
                    asyncio.create_task(danger.maybe_self_improvement())
            except Exception:
                logger.exception("自升级调度失败")
        try:
            from anima.cognitive.controlled_iteration import get_controlled_iteration
            data_dir = str(self.core.config.persistence.data_dir)
            iterate = get_controlled_iteration(data_dir)
            # 约每 10 个 tick（~5min）扫一次；内部还有节流
            if int(time.time() // max(1, float(self.tick_sec))) % 10 == 0:
                opened = iterate.scan_quality_and_open_tickets(self.core)
                if opened:
                    logger.info("受控迭代：新开 %d 个故障工单", len(opened))
                if iterate.is_auto_repair_enabled(self.core):
                    asyncio.create_task(iterate.maybe_auto_repair(self.core))
        except Exception:
            logger.exception("受控迭代调度失败（已隔离）")

        # 自主外部探索独立于群聊活跃度：先只读学习公开资料，只有明确来源群、
        # 达到价值阈值且配置允许时才会创建 Agenda，仍受后续全部出站门控。
        try:
            exploration_cfg = getattr(self.core.config, "exploration", None)
            if exploration_cfg and getattr(exploration_cfg, "enabled", False):
                from anima.cognitive.exploration import get_exploration_manager
                asyncio.create_task(get_exploration_manager(self.core).maybe_run())
        except Exception:
            logger.exception("自主外部探索调度失败（已隔离）")

        # B2 每日秘密自动生成（每天至多一次，去重落盘）
        try:
            acfg = self.core.config.autonomy
            if getattr(acfg, "secret_auto_enabled", True):
                if getattr(self, "_last_secret_gen_date", "") != self._bj_date_str(now):
                    from anima.cognitive.secret_auto import maybe_generate_daily_secrets
                    dd = str(self.core.config.persistence.data_dir)
                    added = maybe_generate_daily_secrets(dd, now=now)
                    if added:
                        logger.info("[秘密] 每日自动生成 %d 条", len(added))
                    self._last_secret_gen_date = self._bj_date_str(now)
        except Exception:
            logger.debug("每日秘密生成失败（已隔离）", exc_info=True)

        # 议程消费（约定跟进 / calendar 等）
        # 注意：议程消费在 _circadian_gate 之前，必须单独做夜间静默检查，
        # 否则凌晨仍会主动发言（如“秽土转生”事件）。
        _quiet_hour = datetime.now(_BJ).hour in (0, 1, 2, 3, 4, 5)
        agenda = getattr(self.core, "agenda", None)
        agenda_item = agenda.peek_ready(now=now) if agenda else None
        if agenda_item is not None and not _quiet_hour:
            if now - self._last_active_speak >= self.active_global_cooldown_sec:
                subject = getattr(self.core, "subject", None)
                intent = subject.claim_agenda(agenda_item, now) if subject else None
                if subject and intent:
                    subject.ledger.mark_dispatched(intent.id, "议程已进入出站")
                spoke = await self._speak(agenda_item.group_id, now, agenda_item.text_intent) if (subject is None or intent) else False
                if spoke:
                    agenda.consume(agenda_item.id)
                    if subject and intent:
                        subject.ledger.complete(intent.id, "QQ 出站确认成功")
                        workflows = getattr(self.core, "workflows", None)
                        if workflows:
                            workflows.complete_by_intent(intent.id)
                    audit = getattr(self.core, "audit", None)
                    if audit:
                        audit.emit("agenda_consumed", agenda_id=agenda_item.id,
                                   group_id=agenda_item.group_id, intent_id=getattr(intent, "id", ""))
                elif subject and intent:
                    max_attempts = int(getattr(self, "active_intent_max_attempts", 5) or 5)
                    subject.ledger.release(
                        intent.id, "本轮未满足出站条件", max_attempts=max_attempts,
                    )
            self.persist()
            return

        if not self._circadian_gate():
            # 睡眠时段（01-05 点）：不主动发言，但生成梦境（灵魂补丁 A2）。
            # 每夜至多一次，异步调度不阻塞心跳；DreamEngine 缺失时静默跳过。
            try:
                from anima.cognitive.dream_engine import DreamEngine
                data_dir = str(self.core.config.persistence.data_dir)
                eng = DreamEngine(data_dir, self.core)
                asyncio.create_task(eng.maybe_generate_during_sleep(now))
            except Exception:
                logger.debug("梦境生成调度失败（已隔离）", exc_info=True)
            self.persist()
            return

        # A3 早晨分享梦境：08:00「醒来」，读昨晚最后一条梦境，10% 概率主动在群里分享
        # （并让 mood_tag 覆盖次日 soul 初始情绪）。每「天」至多尝试一次。
        # config.morning_dream_share_enabled=False 时彻底关闭此类模板式主动分享。
        try:
            if (getattr(getattr(self.core.config, "autonomy", None), "morning_dream_share_enabled", True)
                    and self._maybe_morning_dream_share(now)):
                gid = self._pick_group(now)
                if gid is not None:
                    from anima.cognitive.dream_engine import DreamEngine
                    eng = DreamEngine(str(self.core.config.persistence.data_dir), self.core)
                    dream = eng.get_last_dream()
                    if dream and dream.get("dream"):
                        _share = (
                            f"昨晚本喵做了一个好奇怪的梦…{dream['dream']}"
                            f"（抖耳朵）"
                        )
                        await self._speak(gid, now, _share)
                        eng.apply_dream_mood_to_soul(dream.get("mood_tag", "calm"))
                        logger.info("[梦境] 早晨主动分享梦境 mood_tag=%s", dream.get("mood_tag"))
                        self._last_morning_dream_date = _bj_date_str(now)
                        self._group_last_speak[gid] = now
                        self._last_active_speak = now
                        self.persist()
                        return
        except Exception:
            logger.debug("早晨梦境分享失败（已隔离）", exc_info=True)

        # 选择发言目标群（已含沉默门控 + 冷却 + 预算 + 占比）
        group_id = self._pick_group(now)
        if group_id is None:
            self.persist()
            return

        # 立场传播（danger.stance_propagation）：复用已通过门控的群，由高强度
        # 外向欲望触发主动发言。成功则更新冷却/预算并优先于随机接话返回。
        dcfg = getattr(self.core.config, "danger", None)
        if dcfg and getattr(dcfg, "stance_propagation", False) \
                and getattr(dcfg, "stance_propagation_confirm", False):
            try:
                from anima.cognitive.danger import get_danger_behaviors
                danger = get_danger_behaviors(core=self.core, cfg=dcfg)
                if danger is not None:
                    spoke = await danger.maybe_stance_propagation(group_id)
                    if spoke:
                        self._group_last_speak[group_id] = now
                        self._last_active_speak = now
                        self._group_speak_count[group_id] = self._group_speak_count.get(group_id, 0) + 1
                        self.persist()
                        return
            except Exception:
                logger.exception("立场传播调度失败")

        # 全局冷却：限制主动发言整体频率，防止多群同时刷屏
        if now - self._last_active_speak < self.active_global_cooldown_sec:
            self.persist()
            return

        # 主动发言概率门控（用户要求 0.1）：
        # 通过沉默门控的群，每个 eligible tick 仅以 active_reply_prob 概率真正
        # 开口；其余 tick 做轻量内省（不产生回复），保持「活的」但不打扰。
        if random.random() >= self.active_reply_prob:
            try:
                from anima.cognitive.simulated_life import get_life
                await get_life(self.core).flow_thought()
            except Exception:
                logger.exception("心跳 think 异常")
            self.persist()
            return

        # 主动“可接可不接”：无真实接话点时不开口（省一次 LLM、不刷“尝试即失败”账本）。
        has_hook = self._active_hook_present(group_id, now)
        subject = getattr(self.core, "subject", None)
        proactive_intent = None
        # T1.1: Constitution Drive 门（默认关，fail-closed）——主动表达须源于趋近 Drive 才开口，可 trace。
        drive_origin = None
        if getattr(self.core.config.autonomy, "drive_gate", False):
            drive_origin = self._constitution_drive_snapshot(group_id)
            if drive_origin and not _drive_gate_ok(drive_origin):
                logger.info(
                    "[宪法Drive] 群%s 无趋近驱动(activated=%s approach=%s avoid=%s)，压制主动说话",
                    group_id, drive_origin["activated"], drive_origin["approach"], drive_origin["avoid"],
                )
                has_hook = False
        if subject is not None and has_hook:
            try:
                proposed = subject.propose_expression(
                    group_id, "主动接话", source="heartbeat", priority=0.35,
                    expires_at=now + 3600,
                )
                proactive_intent = subject.ledger.claim(proposed.id, now)
                if proactive_intent:
                    subject.ledger.mark_dispatched(proactive_intent.id, "主动接话出站中")
                if drive_origin:
                    audit = getattr(self.core, "audit", None)
                    if audit:
                        try:
                            audit.emit(
                                "proactive_expression_drive", group_id=group_id, source="heartbeat",
                                activated=bool(drive_origin["activated"]),
                                approach=round(drive_origin["approach"], 3),
                                avoid=round(drive_origin["avoid"], 3),
                                intensity=round(drive_origin.get("intensity", 0.0), 3),
                            )
                        except Exception:
                            logger.exception("Drive trace 审计失败（已隔离）")
            except Exception:
                logger.exception("主动发言意图登记失败")
                proactive_intent = None

        # 多 Bot 社交：每日最多一次「主动找其他 Bot 说话」意图（rivalry>0.3 才触发，config 开启）
        # 仅入议程，最终是否出站仍受 Agenda 冷却/预算/发送确认约束，不会绕过闸门。
        try:
            scfg = getattr(self.core.config, "social", None)
            if scfg and getattr(scfg, "bot_talk_enabled", False):
                from anima.cognitive import bot_social
                _bsd = str(self.core.config.persistence.data_dir)
                _target = bot_social.pick_bot_talk_target(_bsd, now)
                if _target and subject is not None:
                    _intent_text = (
                        f"在群里和 {_target['name']} 聊两句（它是另一个 Bot，你们竞争度较高，"
                        f"可以带刺或调侃，但别真的人身攻击，也别刷屏）。"
                    )
                    subject.propose_expression(
                        group_id, _intent_text, source="bot_social", priority=0.3,
                        expires_at=now + 3600,
                    )
                    logger.info("[多Bot] 今日主动找 Bot %s 说话已入议程", _target["name"])
        except Exception:
            logger.exception("多 Bot 主动意图失败（已隔离）")

        spoke = await self._speak(group_id, now) if has_hook else False
        if subject and proactive_intent:
            if spoke:
                subject.ledger.complete(proactive_intent.id, "主动接话出站成功")
            else:
                max_attempts = int(getattr(self, "active_intent_max_attempts", 5) or 5)
                subject.ledger.release(
                    proactive_intent.id, "主动接话未满足出站条件", max_attempts=max_attempts,
                )
        self.persist()

    # ------------------------------------------------------------------
    # 频率闸门
    # ------------------------------------------------------------------

    def _bj_date_str(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, _BJ).strftime("%Y-%m-%d")

    def _maybe_morning_dream_share(self, now: float) -> bool:
        """A3：仅 08:00、每天至多一次、10% 概率主动分享梦境。返回是否本次应当分享。"""
        today = self._bj_date_str(now)
        last = getattr(self, "_last_morning_dream_date", "")
        should_share, attempted = _decide_morning_dream_share(datetime.now(_BJ).hour, last, today)
        if attempted and not should_share:
            # 抽不中：标记今天已尝试，避免每 tick 重复抽
            self._last_morning_dream_date = today
        return should_share

    def _circadian_gate(self) -> bool:
        from datetime import datetime, timezone, timedelta
        tz = timezone(timedelta(hours=8))
        hour = datetime.now(tz).hour
        # 硬静默：凌晨 1-5 点绝不主动发言（避免深夜骚扰）
        if hour in (1, 2, 3, 4, 5):
            return False
        base = HOURLY_ACTIVITY.get(hour, 0.5)
        jitter = random.uniform(0.5, 1.5)
        prob = base * jitter
        # H3: 情绪驱动主动社交权重
        try:
            from anima.cognitive.mood import get_mood_system
            data_dir = str(self.core.config.persistence.data_dir)
            mood_label = get_mood_system(data_dir).get_state().label
            if mood_label in ("excited", "happy"):
                prob *= 1.3
            elif mood_label in ("tired", "bored"):
                prob *= 0.5
        except Exception:
            pass
        return random.random() < prob

    def _pick_group(self, now: float) -> Optional[int]:
        """选择一个符合冷却和预算条件的群。"""
        from anima.memory.chat_memory import get_chat_memory
        memory = get_chat_memory()

        # 优先选有活跃对话的群
        candidates = []
        gids: set[int] = set(memory.group_recent_ids())
        for key in list(memory._store.keys()):
            if not key.startswith("group_"):
                continue
            # 修正（P0-1）：key 形如 group_{gid}_{uid}，若用
            # int(key.split("_", 1)[1]) 会得到 "gid_uid"，而 Python 允许整数字面量
            # 带下划线（int("727_100") == 727100），会把群号错误地放大成巨大数，
            # 导致主动发言发到错误/不存在的群。改为全拆分取第二段（parts[1]）。
            parts = key.split("_")
            try:
                gid = int(parts[1])
            except (ValueError, IndexError):
                continue

            gids.add(gid)

        for gid in gids:
            core = getattr(self, "core", None)
            permissions = getattr(getattr(getattr(core, "config", None), "permissions", None), "group_policies", {})
            policy = permissions.get(str(gid), {}) if isinstance(permissions, dict) else {}
            if isinstance(policy, dict) and policy.get("autonomy") is False:
                continue
            feedback_gate = getattr(self, "_feedback_allows_group", None)
            if callable(feedback_gate) and not feedback_gate(gid):
                continue
            # 冷却检查
            last = self._group_last_speak.get(gid, 0)
            if now - last < self.cooldown_sec:
                continue

            # 沉默门控：仅当该群曾有过人类发言，且最近一次发言距今
            # ≥ active_silence_min_sec，才考虑主动插话。这样 bot 会在
            # 「对话冷场后」自然地接一句，而不是在聊得正嗨时硬插。
            last_msg = self._group_last_msg.get(gid, 0)
            if last_msg <= 0:
                continue
            if now - last_msg < self.active_silence_min_sec:
                continue

            # 日预算
            count = self._group_speak_count.get(gid, 0)
            active_daily_max = getattr(self, "active_daily_max_per_group", self.daily_max)
            if count >= self.daily_max or count >= active_daily_max:
                continue

            # 占比检查 + 群成熟度：日内计数经日切会清空，回退群在场累计，
            # 避免重启/跨日后「明明是老群却永远凑不够 min_group_messages」。
            total = self._group_total_msgs.get(gid, 0)
            min_group_messages = getattr(self, "active_min_group_messages", 0)
            established = total
            if established < min_group_messages and core is not None:
                try:
                    presence = getattr(getattr(core, "group_presence", None), "items", {}) or {}
                    item = presence.get(int(gid))
                    established = max(established, int(getattr(item, "message_count", 0) or 0))
                except Exception:
                    pass
            if established < min_group_messages:
                continue
            if total > 0 and count / max(total, 1) > self.ratio_max:
                continue

            candidates.append(gid)

        if not candidates:
            return None

        return random.choice(candidates)

    def _group_only_avoided_speakers(self, group_id: int, now: float) -> bool:
        """群内近期人类发言者若全部是被回避用户，则跳过主动发言。

        避免 bot 在 24h 回避期内还去「关心 / care」曾经攻击自己的人。
        """
        try:
            data_dir = str(self.core.config.persistence.data_dir)
            from anima.memory.chat_memory import get_chat_memory
            from anima.cognitive.scar import get_scar_system
            cm = get_chat_memory()
            scar = get_scar_system(data_dir)
            spoken_uids: set[int] = set()
            for key in list(cm._store.keys()):
                if not key.startswith(f"group_{group_id}_"):
                    continue
                parts = key.split("_")
                if len(parts) < 3:
                    continue
                try:
                    uid = int(parts[2])
                except (ValueError, IndexError):
                    continue
                hist = cm.get_context("group", group_id, recent_rounds=3, user_id=uid)
                if any(m.get("role") == "user" for m in hist):
                    spoken_uids.add(uid)
            if not spoken_uids:
                return False
            return all(scar.is_avoiding(uid, now) for uid in spoken_uids)
        except Exception:
            logger.debug("回避发言检查失败", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # 发言
    # ------------------------------------------------------------------

    def _constitution_drive_snapshot(self, group_id: int):
        """汇集群级 today 信号 → 轻量宪法桥 → 带 gate_ok 的 Drive，供主动表达 trace。失败返回 None。"""
        data_dir = str(getattr(self.core.config.persistence, "data_dir", "data"))
        arousal, valence = 0.2, 0.1
        try:
            from anima.cognitive.mood import get_mood_system
            _m = get_mood_system(data_dir).get_state()
            arousal = float(getattr(_m, "arousal", arousal))
            valence = float(getattr(_m, "valence", valence))
        except Exception:
            logger.debug("Drive 桥 mood 读取失败，用默认", exc_info=True)
        threat = max(0.0, -valence)
        d = _constitution_drive(arousal, valence, 0.2, threat)
        if d:
            d["gate_ok"] = _drive_gate_ok(d)
        return d
    def _active_hook_present(self, group_id: int, now: float) -> bool:
        """是否存在值得主动接住的近期群语接话点（短时上下文评分 >= 0.24）。"""
        try:
            from anima.memory.chat_memory import get_chat_memory
            memory = get_chat_memory()
            recent_context = getattr(memory, "get_recent_group_context", None)
            history = recent_context(group_id, limit=12) if callable(recent_context) else []
            if not history:
                return False
            return score_active_context(history) >= 0.24
        except Exception:
            # 评分失败保守放行：回退给原模型 [skip] 判断，不削弱主动发言。
            return True

    async def _speak(self, group_id: int, now: float, intent: str = "") -> bool:
        """生成并发送主动发言。"""
        if self.core.llm is None:
            return False

        # 自我保护：被伤过两次的用户，24h 内不主动 care / 接话
        try:
            if not intent and self._group_only_avoided_speakers(group_id, now):
                logger.info("[回避] 群 %d 近期发言者均被回避，跳过主动发言", group_id)
                return False
        except Exception:
            logger.debug("回避门控异常", exc_info=True)

        from anima.cognitive.time_sense import time_description
        from anima.cognitive.desire import get_desire_system
        from anima.memory.chat_memory import get_chat_memory
        from anima.core.reply_splitter import filter_inner_monologue

        desire = get_desire_system()
        memory = get_chat_memory()
        # 主动接话只看 15 分钟 TTL 的群短时缓冲，避免拿持久化旧对话突然续聊。
        # 明确议程/提醒可以在没有近期上下文时照常发送。
        recent_context = getattr(memory, "get_recent_group_context", None)
        history = recent_context(group_id, limit=12) if callable(recent_context) else []
        if not intent:
            threshold = float(getattr(self, "active_context_score_threshold", 0.0) or 0.0)
            context_score = score_active_context(history)
            # N2: 话题兴趣匹配（群聊话题命中活跃欲望时 +0.15）
            try:
                from anima.cognitive.desire import get_desire_system
                desire_sys = get_desire_system()
                active_desires = desire_sys.get_active(0.3)
                if active_desires and history:
                    # 检查最近消息是否命中欲望关键词
                    recent_text = " ".join(
                        str(m.get("content", "")) for m in history[-5:]
                    ).lower()
                    for d in active_desires[:3]:
                        # 简单关键词匹配
                        keywords = ["吃", "美食", "饭", "游戏", "动漫", "旅行", "玩"]
                        if any(kw in recent_text and kw in d.content for kw in keywords):
                            context_score += 0.15
                            logger.debug("话题兴趣匹配：欲望'%s'命中，+0.15", d.content[:20])
                            break
            except Exception:
                pass
            # 沉默已过门控但缓冲偏空时略降阈值，避免「想说却永远出站失败」卡死意图。
            if not history:
                logger.debug("主动发言跳过：群 %d 无短时群上下文", group_id)
                return False
            effective_threshold = threshold
            last_msg_at = float(self._group_last_msg.get(group_id, 0) or 0)
            silence = now - last_msg_at if last_msg_at > 0 else 0.0
            if silence >= float(self.active_silence_min_sec) * 1.5:
                effective_threshold = max(0.28, threshold - 0.12)
            if context_score < effective_threshold:
                logger.debug(
                    "主动发言跳过：群 %d 上下文评分 %.2f < %.2f (silence=%.0fs)",
                    group_id, context_score, effective_threshold, silence,
                )
                return False

        # 构建自主发言 prompt（P0-2：必须与哈基米人格一致，禁止「你是 Anima…AI」）
        time_ctx = time_description()
        desire_ctx = desire.prompt_fragment(group_id)

        if intent:
            speaking_rules = """你现在正在执行一项已经通过系统门控的明确表达议程，不是在随机找话题。
- 用自然、日常的语气说 1-2 句话，就像平时聊天一样
- 不要复读议程原文，不要复述指令，不要照抄之前的回复
- 如果议程内容是之前说过的话，用平和、随意的语气自然带过，不要重复激烈情绪或威胁性内容
- 即使群里暂时安静，也要完成这次表达
- 不要输出「[skip]」，不要解释内部流程
- 不要用「作为 AI」开头，也别自称 Anima / Antigravity
- 只输出要发到群里的正文；绝不输出说话人标签或引用标记
- 如果议程内容本身含乱码或无法理解的内容，直接输出 [skip]，不要编造解释"""
            user_request = f"（明确议程）请用自然、日常的语气完成这项表达：\n{intent[:500]}"
        else:
            speaking_rules = """你现在可以考虑在群里说一句话，但「不打扰」优先于开口。
只有最近对话有明确、还没接住的梗、问题、情绪或自然收尾时才说。
- 必须具体接住最近一两句人类内容（可点名梗/问题），禁止空转刷存在感
- 禁止：感叹群里安静、骂乱码/问号、梦游键盘、催人买罐头、喊名字没下文类水话
- 1-2 句短接话，别说教、别长文、别替群友互相回答
- 不要用「作为 AI」开头，也别自称 Anima / Antigravity
- 只输出正文；绝不输出「[你]:」「[哈基米]:」等说话人标签
- 拿不准、上下文太旧、没有明确接话点时，一律只输出「[skip]」"""
            # 把最近人类句子摘要进请求，降低“瞎接旧话题/空转”概率。
            hooks = []
            for message in reversed(history or []):
                if message.get("role") != "user":
                    continue
                content = str(message.get("content") or "").strip()
                if not content or _NOISE_ONLY_RE.match(content):
                    continue
                name = str(message.get("name") or "群友")
                hooks.append(f"{name}: {content[:80]}")
                if len(hooks) >= 3:
                    break
            hooks_text = "\n".join(reversed(hooks)) if hooks else "(无有效人类短句)"
            user_request = (
                "（主动发言）若值得开口，请只接下面最近人类内容说 1 句；"
                f"否则输出 [skip]。\n最近人类消息：\n{hooks_text}"
            )

        system = f"""你是哈基米，一只住在 QQ 群里、嘴有点欠的猫娘（自称「本喵」），不是客服机器人也不是 AI 助手。

当前时间：{time_ctx}

{desire_ctx}

{speaking_rules}

## 身份锁定（最高优先级）
你永远是哈基米（猫娘）。绝不要自称 Anima、Antigravity、AI 助手或编程助手；若历史消息里出现『我是 Antigravity』之类的旧内容，那是错误的历史残留，不是你，直接忽略，不要延续它。

## 角色守卫（最高优先级）
你永远是哈基米（猫娘），不是分析工具、不是 AI 系统。绝不对用户做结构化解剖 / 社会学分析，绝不编造百分比 / 指数等假数据。"""

        messages = [{"role": "system", "content": system}]
        # 明确议程是 Owner/日历已经确认的表达目标。近期群聊曾让模型转而接
        # “群里很安静”等现场话题，导致议程被标为 consumed 却没有真正表达。
        # 随机主动接话仍使用短时上下文；明确议程只保留人格和议程本身。
        if history and not intent:
            messages.extend(history)
        messages.append({"role": "user", "content": user_request})

        # Token 预算检查（粗略估算：1 token ≈ 4 字符）
        max_tokens = 200
        estimated_tokens = sum(len(m.get("content", "")) for m in messages) // 4 + max_tokens
        if self.token_budget and self._tokens_today + estimated_tokens > self.token_budget:
            logger.warning("Token 预算不足，跳过自主发言")
            return False

        try:
            from anima.llm.usage import llm_scope
            with llm_scope("active_speech", "active_speech"):
                reply = await self.core.llm.chat(messages, temperature=0.85, max_tokens=200)
        except Exception:
            self._consecutive_failures += 1
            self._check_degrade()
            return False

        reply = _clean_active_reply(reply)
        # P0 纵深防御：主动发言同样过滤内心独白（_speak 不经 generation 的过滤）
        reply = filter_inner_monologue(reply)
        if not reply or "[skip]" in reply.lower() or len(reply.strip()) < 4:
            return False
        if _is_low_value_active_reply(reply):
            logger.info("自主发言丢弃低质水话 group=%d text_len=%d", group_id, len(reply))
            return False
        if len(reply) > 120:
            reply = reply[:120].rstrip("，,。；;、 ") + "…"

        # ── 去重 Layer 1：与群内近期所有 bot 消息比对（含被动回复）──
        # 防止自主发言照搬刚刚被动回复说过的话（12:05 事件的根因）。
        _BOT_LABEL_RE = re.compile(r"^\[哈基米\]\s*[:：]?\s*")
        bot_msgs_recent = [
            _BOT_LABEL_RE.sub("", str(m.get("content", ""))).strip()
            for m in (history or [])
            if m.get("role") == "assistant"
        ]
        current = _normalized_active_reply(reply)
        if len(current) >= 10 and any(
            SequenceMatcher(None, current, _normalized_active_reply(bm)).ratio() >= 0.70
            for bm in bot_msgs_recent
            if len(_normalized_active_reply(bm)) >= 10
        ):
            logger.info("自主发言丢弃：与群内近期 bot 回复过于相似 group=%d", group_id)
            return False

        # ── 去重 Layer 2：与本群近期自主发言比对（4h 窗口，跨重启持久化）──
        # 0.76 能挡住同主题的换词复读，又不会干扰普通短句或正常的后续对话。
        recent_by_group = getattr(self, "_recent_active_replies", {})
        recent = [item for item in recent_by_group.get(group_id, []) if now - item[0] < 14400]
        recent_by_group[group_id] = recent
        if len(current) >= 12 and any(
            SequenceMatcher(None, current, _normalized_active_reply(previous)).ratio() >= 0.76
            for _, previous in recent
        ):
            logger.info("自主发言去重：群 %d 的候选回复与近期自主内容过于相似", group_id)
            return False

        try:
            message_id = await self.core.gateway.send(OutboundMsg("group", group_id, reply))
        except Exception:
            self._consecutive_failures += 1
            self._check_degrade()
            logger.exception("自主发言出站异常 group=%d", group_id)
            return False
        if message_id is None:
            self._consecutive_failures += 1
            self._check_degrade()
            logger.warning("自主发言出站失败 group=%d", group_id)
            return False

        # 只有 OneBot 返回成功后才消耗预算、更新冷却和写入群记忆。
        self._consecutive_failures = 0
        self._tokens_today += estimated_tokens
        self._group_last_speak[group_id] = now
        self._last_active_speak = now
        self._group_speak_count[group_id] = self._group_speak_count.get(group_id, 0) + 1
        recent.append((now, reply))
        recent_by_group[group_id] = recent[-4:]
        self._recent_active_replies = recent_by_group
        feedback_pending = getattr(self, "_active_feedback_pending", None)
        if feedback_pending is None:
            feedback_pending = self._active_feedback_pending = {}
        feedback_pending[group_id] = {
            "sent_at": now,
            "message_id": str(message_id),
            "source": "agenda" if intent else "heartbeat",
        }
        logger.info("自主发言成功 group=%d text_len=%d", group_id, len(reply))

        # 记录到记忆
        memory.add_group_bot_reply(group_id, reply)
        return True

    # ------------------------------------------------------------------
    # 消息计数（由外部调用，每收到一条群消息 +1）
    # ------------------------------------------------------------------

    def count_message(self, group_id: int) -> None:
        """每收到一条群消息调用：累加计数并记录最近发言时间（供沉默门控）。"""
        self._record_active_engagement(int(group_id), time.time())
        self._group_total_msgs[group_id] = self._group_total_msgs.get(group_id, 0) + 1
        self._group_last_msg[group_id] = time.time()

    def _feedback_stats(self, group_id: int) -> dict[str, int]:
        return self._active_feedback_stats.setdefault(int(group_id), {"engaged": 0, "ignored": 0})

    def _record_active_engagement(self, group_id: int, now: float) -> None:
        pending = self._active_feedback_pending.get(int(group_id))
        if not pending:
            return
        sent_at = float(pending.get("sent_at", 0) or 0)
        if sent_at <= 0 or now - sent_at > self.active_feedback_window_sec:
            return
        stats = self._feedback_stats(group_id)
        stats["engaged"] += 1
        self._active_feedback_pending.pop(int(group_id), None)
        audit = getattr(self.core, "audit", None)
        if audit:
            audit.emit("active_speech_feedback", group_id=int(group_id), outcome="engaged",
                       source=str(pending.get("source") or ""))

    def _expire_active_feedback(self, now: float) -> None:
        expired = []
        for group_id, pending in self._active_feedback_pending.items():
            sent_at = float(pending.get("sent_at", 0) or 0)
            if sent_at > 0 and now - sent_at >= self.active_feedback_window_sec:
                expired.append((group_id, pending))
        for group_id, pending in expired:
            self._feedback_stats(group_id)["ignored"] += 1
            self._active_feedback_pending.pop(group_id, None)
            audit = getattr(self.core, "audit", None)
            if audit:
                audit.emit("active_speech_feedback", group_id=int(group_id), outcome="ignored",
                           source=str(pending.get("source") or ""))

    def active_acceptance(self, group_id: int) -> dict[str, float | int | bool]:
        stats = self._feedback_stats(group_id)
        engaged = int(stats.get("engaged", 0) or 0)
        ignored = int(stats.get("ignored", 0) or 0)
        samples = engaged + ignored
        score = (engaged + 1) / (samples + 2)  # Beta(1,1) 平滑，避免零样本极端值。
        limited = samples >= self.active_feedback_min_samples and score < self.active_feedback_min_acceptance
        return {"engaged": engaged, "ignored": ignored, "samples": samples,
                "score": round(score, 3), "limited": limited}

    def _feedback_allows_group(self, group_id: int) -> bool:
        return not bool(self.active_acceptance(group_id)["limited"])

    # ------------------------------------------------------------------
    # 模拟生活节律
    # ------------------------------------------------------------------

    async def _life_checkpoints(self) -> None:
        """在每次心跳 tick 中检查模拟生活 + 反刍 + 遗忘 + 情绪。"""
        try:
            from anima.cognitive.simulated_life import get_life
            from anima.cognitive.forgetting import get_forgetting
            from anima.memory.chat_memory import get_chat_memory

            life = get_life(self.core)
            forgetting = get_forgetting(self.core.config.persistence.data_dir)
            memory = get_chat_memory()

            # 情绪系统更新（时间段影响 + 衰减）
            try:
                from anima.cognitive.mood import get_mood_system
                mood = get_mood_system(self.core.config.persistence.data_dir)
                # 每次心跳触发一次时间影响（内部有衰减逻辑）
                mood.trigger("mentioned", 0.05)  # 轻微的时间影响
            except Exception:
                logger.debug("情绪系统更新失败，已隔离")

            # 晨间觉醒
            await life.morning_routine()

            # 碎片心流
            await life.flow_thought()

            # 晚间反思
            await life.evening_reflection()

            # M2: 晚安行为
            await life.goodnight_behavior()

            # N1: 旁观者内心独白（群很热闹但 bot 没被 @ 时，10% 概率写内心笔记）
            try:
                from anima.cognitive.group_presence import get_group_presence
                from anima.cognitive.learning import get_learning
                gp = get_group_presence(self.core.config.persistence.data_dir)
                learning = get_learning(self.core.config.persistence.data_dir)
                # 找最近活跃但 bot 没被 summon 的群
                for gid, presence in gp.items.items():
                    if presence.state == "GETTING_FAMILIAR" and presence.message_count >= 5:
                        # 群很热闹但 bot 没被 @
                        if random.random() < 0.1:
                            learning.add_note(
                                f"[旁观] 群 {gid} 在聊什么，本喵在旁边看着挺有意思的"
                            )
                            logger.debug("旁观内心独白已写入 group=%s", gid)
                        break  # 每次心跳只处理一个群
            except Exception:
                logger.debug("旁观内心独白失败")

            # S3: 群活跃度影响欲望生成
            try:
                from anima.cognitive.group_presence import get_group_presence
                from anima.cognitive.desire import get_desire_system
                gp = get_group_presence(self.core.config.persistence.data_dir)
                desire_sys = get_desire_system(self.core.config.persistence.data_dir)
                # 统计总群活跃度
                total_msgs = sum(
                    p.message_count for p in gp.items.values()
                    if hasattr(p, "message_count")
                )
                if total_msgs >= 15:
                    # 群很活跃 → 社交欲上升
                    desire_sys.add("想跟大家聊天", intensity=0.35, kind="chat")
                elif total_msgs <= 2:
                    # 群冷清 → 独处欲/好奇心上升
                    desire_sys.add("想自己探索点新东西", intensity=0.3, kind="chat")
            except Exception:
                logger.debug("S3 群活跃度欲望生成失败")

            # 反刍（将近期经历沉淀为自我笔记）
            if self._rumination:
                await self._rumination.maybe_ruminate()

            # 遗忘调度
            await forgetting.run(chat_memory=memory, vector_memory=self.core.vector_memory)

        except Exception:
            logger.exception("模拟生活 checkpoints 异常")

    # ------------------------------------------------------------------
    # 熔断
    # ------------------------------------------------------------------

    def _check_degrade(self) -> None:
        if self._consecutive_failures >= self._max_failures:
            self._degraded = True
            logger.warning("熔断: 连续 %d 次 LLM 失败，切换为纯应答模式", self._consecutive_failures)

    async def _check_recovery(self) -> None:
        """每 5 分钟尝试恢复。"""
        if self._consecutive_failures == 0:
            self._degraded = False
            logger.info("熔断恢复: 自主模式已重新启用")
            return

        # 尝试探测 LLM
        if self.core.llm:
            try:
                from anima.llm.usage import llm_scope
                with llm_scope("active_speech", "health_probe"):
                    probe = await self.core.llm.chat(
                        [{"role": "user", "content": "ping"}],
                        temperature=0, max_tokens=5
                    )
                if probe:
                    self._consecutive_failures = 0
                    self._degraded = False
                    logger.info("熔断恢复: LLM 探测成功")
            except Exception:
                self._consecutive_failures += 1
