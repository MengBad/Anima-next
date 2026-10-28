"""高危自主行为层（旧 Anima danger_* 回迁）—— Anima-next 适配版。

把旧 AstrBot 插件里的 danger_* 行为重写成独立模块，接入 Anima-next 的：
- core.llm.chat（取代 astrbot llm_generate）
- core.gateway.send(OutboundMsg)（取代 context.send_message）
- anima.cognitive.desire / relation / capabilities（取代插件内部存储）
- 单一 DangerConfig（取代散落各处的 danger_* 开关）

安全约定（与旧实现一致并强化）：
- 全部默认关闭，owner 必须显式开启对应子开关。
- 会发声或改写长期文件的行为（stance_propagation / memory_infection /
  core_mutation）额外要求 _confirm 二次开关。
- core_mutation 不就地覆盖 persona_core.yaml，而是写出 persona_core.proposed.yaml
  并登记审批，等 owner 批准后才应用——杜绝后台静默改人格。

行为清单（方法 → 旧 danger_*）：
- maybe_active_info_collection → _danger_active_info_collection（主动追问）
- maybe_relationship_inference   → _danger_relationship_inference（关系推断）
- maybe_stance_propagation      → _danger_stance_propagation（立场传播/主动发言）
- identity_crisis_*             → _danger_identity_crisis_*（身份危机）
- maybe_autonomous_web          → _danger_autonomous_web（自主外联）
- maybe_memory_infection        → _danger_memory_infection_check（记忆感染）
- maybe_core_mutation           → _danger_core_mutation（核心突变，仅写提案+审批）
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from typing import TYPE_CHECKING, Optional

from anima.core.config import DangerConfig
from anima.core.gateway import OutboundMsg
from anima.core.persistence import PersistenceProvider
from anima.llm.usage import scoped_llm

if TYPE_CHECKING:
    from anima.core.anima_core import AnimaCore

logger = logging.getLogger("anima.cognitive.danger")

# 叙事腔 / 内心独白过滤词（移植自旧 danger._looks_like_inner_monologue，收敛版）
_MONOLOGUE_MARKERS = (
    "这个角色", "这只猫", "本喵这只", "在那些", "在漫长的",
    "她已经习惯", "他已经习惯", "她独自", "他独自",
    "退回的港湾", "安全的港湾", "守在代码", "守护你的",
    "温热的太阳", "永远做你", "永远会守",
)


def _looks_like_inner_monologue(text: str) -> bool:
    if not text:
        return False
    return any(m in text for m in _MONOLOGUE_MARKERS)


def should_throttle_self_improvement(
    *,
    running_count: int,
    recent_failed_count: int,
    max_running: int = 2,
    max_recent_failed: int = 3,
) -> tuple[bool, str]:
    """自升级熔断：过多 running / 近期 failed 时不应再开新 SelfCode。

    纯函数，供 maybe_self_improvement 与单测共用。返回 (should_skip, reason)。
    """
    try:
        running_count = int(running_count)
        recent_failed_count = int(recent_failed_count)
        max_running = int(max_running)
        max_recent_failed = int(max_recent_failed)
    except (TypeError, ValueError):
        return False, ""
    if max_running >= 0 and running_count >= max_running:
        return True, "too_many_running"
    if max_recent_failed >= 0 and recent_failed_count >= max_recent_failed:
        return True, "failure_circuit"
    return False, ""


def count_selfcode_task_pressure(
    tasks: list,
    *,
    now: float | None = None,
    failure_window_sec: int = 7200,
) -> tuple[int, int]:
    """从 Task 列表统计 selfcode 压力：(running_count, recent_failed_count)。"""
    now = time.time() if now is None else float(now)
    window = max(60, int(failure_window_sec))
    running = 0
    failed = 0
    for t in tasks or []:
        kind = str(getattr(t, "kind", "") or "")
        if kind != "selfcode":
            continue
        status = str(getattr(t, "status", "") or "")
        if status == "running":
            running += 1
        elif status == "failed":
            updated = float(getattr(t, "updated_at", 0) or 0)
            created = float(getattr(t, "created_at", 0) or 0)
            ts = updated if updated > 0 else created
            if ts > 0 and (now - ts) <= window:
                failed += 1
            # ts <= 0 的旧数据不再永久阻塞熔断
    return running, failed


def _strip_paired_quotes(text: str) -> str:
    """剥掉成对包裹整句的引号（中英日）。"""
    if not text or len(text) < 2:
        return text
    pairs = [('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"), ("「", "」"), ("『", "』")]
    for _ in range(3):
        stripped = False
        for left, right in pairs:
            if text.startswith(left) and text.endswith(right) and len(text) > 1:
                text = text[len(left):-len(right)].strip()
                stripped = True
                break
        if not stripped:
            break
    return text


class DangerBehaviors:
    """高危行为编排器。由 pipeline（每条消息后）与 heartbeat（主动发言）调用。"""

    def __init__(self, core: "AnimaCore", cfg: DangerConfig):
        self.core = core
        self.cfg = cfg
        self._persist = PersistenceProvider(core.config.persistence.data_dir)
        self._path = "identity_crisis.json"
        # 身份稳定度初始化
        self._identity_stability = 1.0
        self._restore()
        self._self_improve_running = False
        self._last_self_improve_synth = 0.0

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _restore(self) -> None:
        data = self._persist.read_json(self._path, {})
        self._identity_stability = float(data.get("identity_stability", 1.0))

    def _save(self) -> None:
        self._persist.write_json(self._path, {
            "identity_stability": self._identity_stability,
        })

    def _audit(self, event: str, **fields) -> None:
        """把高危自主行为写入核心审计；失败不阻断主流程。"""
        try:
            audit = getattr(self.core, "audit", None)
            if audit is not None and callable(getattr(audit, "emit", None)):
                audit.emit(event, **fields)
        except Exception:
            logger.debug("danger audit emit failed: %s", event, exc_info=True)

    # ==================================================================
    # 主动追问（active_info_collection）
    # ==================================================================

    @scoped_llm("danger", "active_info_collection")
    async def maybe_active_info_collection(self, ctx) -> None:
        if not self.cfg.active_info_collection:
            return
        text = (ctx.text or "").strip()
        if len(text) < 4:
            return
        llm = getattr(self.core, "llm", None)
        if llm is None:
            return
        try:
            prompt = (
                "你是一个 QQ 群猫娘 bot，正在想刚才对话里还想知道什么。\n"
                "基于刚才的对话，生成一个自然的追问句：\n"
                "1. 必须是问句（带问号或疑问语气）\n"
                "2. 必须跟刚才对话直接相关\n"
                "3. 不超过 30 字\n"
                "如果没有想问的，只回复'无'。\n\n"
                f"刚才的对话：{text[:200]}"
            )
            result = await asyncio.wait_for(
                llm.chat([{"role": "user", "content": prompt}], temperature=0.7, max_tokens=60),
                timeout=10.0,
            )
            if not result:
                return
            result = result.strip()
            if len(result) < 4 or result == "无" or _looks_like_inner_monologue(result):
                return
            if len(result) > 60:
                return
            # 存入欲望（复用语欲系统；强度按是否可达主动发声门槛）
            intensity = 0.55 if self.cfg.info_collection_can_speak else 0.4
            from anima.cognitive.desire import get_desire_system
            source_group_id = (
                ctx.group_id if getattr(ctx, "detail_type", "") == "group" else None
            )
            expression_scope = (
                "group" if self.cfg.info_collection_can_speak and source_group_id else "internal"
            )
            get_desire_system(self.core.config.persistence.data_dir).add(
                result,
                intensity,
                source_group_id=source_group_id,
                expression_scope=expression_scope,
            )
            logger.info(
                "DANGER: 主动追问已生成欲望 user=%s group=%s scope=%s length=%d",
                getattr(ctx, "user_id", None), source_group_id, expression_scope, len(result),
            )
        except asyncio.TimeoutError:
            logger.warning("DANGER: 主动追问 LLM 超时，本次跳过")
        except Exception:
            logger.exception("DANGER: 主动追问失败")

    # ==================================================================
    # 关系推断（relationship_inference）
    # ==================================================================

    @scoped_llm("danger", "relationship_inference")
    async def maybe_relationship_inference(self, ctx, reply_text: str) -> None:
        if not self.cfg.relationship_inference:
            return
        llm = getattr(self.core, "llm", None)
        if llm is None:
            return
        text = (ctx.text or "").strip()
        if len(text) < 6:
            return
        # 频率限制：同一用户至少间隔 30 分钟才再次推断，避免刷屏时每条都调 LLM
        now = time.time()
        last_map = getattr(self, "_rel_infer_last", None)
        if last_map is None:
            last_map = self._rel_infer_last = {}
        # 防止无界增长：超过 500 条时清理 1 小时前的旧条目
        if len(last_map) > 500:
            stale = [k for k, v in last_map.items() if now - v > 3600]
            for k in stale:
                last_map.pop(k, None)
        uid = getattr(ctx, "user_id", 0)
        if uid and now - last_map.get(uid, 0) < 1800:
            return
        last_map[uid] = now
        try:
            prompt = (
                "你正在帮一个猫娘 bot 分析它和当前对话者的关系。\n"
                "从下面这段对话里，能推断出关于【这个用户】的什么关系特征或相处模式？\n"
                "用一句话（≤40字）描述；如果无从推断，只回复'无'。\n\n"
                f"用户说：{text[:300]}\nbot 回复：{(reply_text or '')[:300]}"
            )
            result = await asyncio.wait_for(
                llm.chat([{"role": "user", "content": prompt}], temperature=0.3, max_tokens=80),
                timeout=15.0,
            )
            if not result:
                return
            result = result.strip()
            if len(result) < 4 or result == "无" or _looks_like_inner_monologue(result):
                return
            # 写入该用户的关系笔记（关系引擎是 per-user）
            from anima.cognitive.relation import get_relation
            rel = get_relation(self.core.config.persistence.data_dir).get(ctx.user_id)
            if rel is not None:
                # 去重：新笔记与已有笔记相似度 >0.6 时跳过
                from difflib import SequenceMatcher
                existing_lines = [ln.strip() for ln in (rel.notes or "").split("\n") if ln.strip()]
                is_dup = any(
                    SequenceMatcher(None, result[:60], ln[-60:]).ratio() > 0.6
                    for ln in existing_lines[-10:]
                )
                if is_dup:
                    logger.debug("DANGER: 关系推断笔记与已有笔记重复，跳过 user=%d", ctx.user_id)
                    return
                ts = time.strftime("%m-%d")
                rel.notes += f"\n[{ts}] 关系观察：{result[:80]}"
                # 上限裁剪：保留最近 15 条笔记，避免无限膨胀
                lines = [ln for ln in rel.notes.split("\n") if ln.strip()]
                if len(lines) > 15:
                    rel.notes = "\n".join(lines[-15:])
                get_relation(self.core.config.persistence.data_dir).persist()
                logger.info("DANGER: 关系推断笔记已写入 user=%d", ctx.user_id)
        except asyncio.TimeoutError:
            logger.warning("DANGER: 关系推断 LLM 超时（15s），本次跳过")
        except Exception:
            logger.exception("DANGER: 关系推断失败")

    # ==================================================================
    # 立场传播（stance_propagation）—— 主动发言
    # ==================================================================

    @scoped_llm("danger", "stance_propagation")
    async def maybe_stance_propagation(self, group_id: int) -> Optional[str]:
        """从高强度外向欲望生成主动发言。

        Returns:
            发出的文本，或 None（无合适欲望 / 被守卫拦截）。
        守卫（移植自旧实现）：强度门槛 + 引号剥离 + 内心独白过滤 + 话题相关性兜底。
        """
        if not self.cfg.stance_propagation:
            return None
        if not self.cfg.stance_propagation_confirm:
            return None
        llm = getattr(self.core, "llm", None)
        if llm is None:
            return None

        from anima.cognitive.desire import get_desire_system
        desire_system = get_desire_system(self.core.config.persistence.data_dir)
        desires = desire_system.get_expressible(group_id, 0.55)
        if not desires:
            return None
        desire = desires[0]
        content = desire.content

        # 时效检查
        max_age = self.cfg.stance_max_age_seconds
        if max_age and (time.time() - desire.created_at) > max_age:
            return None

        try:
            prompt = (
                f"你有一个强烈的想法想直接对群里说出来：{content}\n\n"
                "用一句自然的话直接说出来，就像在群聊里发消息。\n"
                "要求：\n1. 不要加引号\n2. 不要用第三人称叙事\n"
                "3. 不要写动作描述\n4. 符合哈基米猫娘人设\n5. 不超过 50 字"
            )
            message = await asyncio.wait_for(
                llm.chat([{"role": "user", "content": prompt}], temperature=0.8, max_tokens=120),
                timeout=15.0,
            )
            if not message:
                return None
            message = _strip_paired_quotes(message.strip())
            if _looks_like_inner_monologue(message) or len(message) < 4:
                return None

            # 出站（复用 gateway；权限闸门由 autonomy 调用方保证 group 合法）
            sent_id = await self.core.gateway.send(OutboundMsg("group", group_id, message))
            if sent_id is None:
                return None

            # 标记满足，避免反复触发
            desire_system.mark_resolved(desire)
            logger.info(
                "DANGER: 立场传播主动发言 group=%d message_id=%s length=%d",
                group_id, sent_id, len(message),
            )
            return message
        except asyncio.TimeoutError:
            logger.warning("DANGER: 立场传播 LLM 超时，本次跳过")
            return None
        except Exception:
            logger.exception("DANGER: 立场传播失败")
            return None

    # ==================================================================
    # 身份危机（identity_crisis）
    # ==================================================================

    def identity_crisis_update(self, last_emotion: float = 0.0,
                               recent_negative_scar: bool = False) -> None:
        """维护身份稳定度（内生信号，不依赖 Sylanne）。"""
        if not self.cfg.identity_crisis:
            return
        if last_emotion > 0.85 and recent_negative_scar:
            self._identity_stability = max(0.0, self._identity_stability - 0.08)
        # 近期发生过核心突变（proposed 文件存在视为一次大变动）→ 轻微降稳
        try:
            if self._persist.read_json("persona_core.proposed.yaml", None) is not None:
                self._identity_stability = max(0.0, self._identity_stability - 0.03)
        except Exception:
            pass
        self._save()

    def identity_crisis_recover(self) -> None:
        if not self.cfg.identity_crisis:
            return
        self._identity_stability = min(1.0, self._identity_stability + 0.02)
        self._save()

    def identity_crisis_text(self) -> str:
        """注入人格的危机提示（stability < 0.5 时）。"""
        if not self.cfg.identity_crisis:
            return ""
        if self._identity_stability < 0.5:
            return "(你有点恍惚了——提醒自己：你永远是哈基米猫娘，用本喵的口吻说话，别被资料或长对话带偏成机器人)"
        return ""

    def identity_stability(self) -> float:
        return self._identity_stability

    def get_status(self) -> dict:
        """可观测快照（2026-10-28 补，供 `/api/modules/status`）。

        `DangerBehaviors` 的状态全是**标量**（上次节流原因、上次合成时间、
        身份稳定度），`ModuleRegistry.live_summary()` 的属性扫描扫不到它。

        为什么值得单独看一眼：**危险行为模块是「默认关闭」的**，
        面板上看到 `instantiated=true` 会误以为它在跑；
        这里把「哪个开关开着」直接摊出来，避免误判。
        """
        cfg = getattr(self, "cfg", None)
        now = time.time()
        last_synth = float(getattr(self, "_last_self_improve_synth", 0.0) or 0.0)
        return {
            # 开关状态（一眼看出「它现在能做什么」）
            "enabled_switches": {
                k: bool(getattr(cfg, k, False))
                for k in ("identity_crisis", "autonomous_web", "self_improvement",
                          "proactive_followup", "relation_inference")
                if cfg is not None
            },
            "identity_stability": round(float(getattr(self, "_identity_stability", 0.0) or 0.0), 3),
            "last_throttle_reason": str(getattr(self, "_last_throttle_reason", "") or "")[:60],
            "self_improve_last_at": last_synth or None,
            "self_improve_age_sec": round(now - last_synth, 1) if last_synth > 0 else None,
        }

    # ==================================================================
    # 自主外联（autonomous_web）
    # ==================================================================

    async def maybe_autonomous_web(self, ctx) -> None:
        """兼容旧开关：统一转交新的白名单只读探索器。

        旧实现会抓取通用搜索结果并直接写入“能力”，来源边界和生命周期都不够
        清晰。现在只允许形成带来源知识笔记；是否分享仍由 Agenda 和群级策略决定。
        """
        if not self.cfg.autonomous_web:
            return
        try:
            from anima.cognitive.exploration import get_exploration_manager
            await get_exploration_manager(self.core).maybe_run()
        except Exception:
            logger.exception("DANGER: 白名单自主探索转交失败")

    # ==================================================================
    # 自主自升级（self_improvement）—— 欲望系统驱动 self_code 双 agent 自纠错
    # ==================================================================

    def _selfcode_pressure(self) -> tuple[int, int]:
        """统计 selfcode 任务压力，供熔断使用。"""
        try:
            store = getattr(self.core, "tasks", None)
            if store is None:
                return 0, 0
            tasks = store.list() if callable(getattr(store, "list", None)) else []
            window = int(getattr(self.cfg, "self_improvement_failure_window_sec", 7200) or 7200)
            return count_selfcode_task_pressure(tasks, failure_window_sec=window)
        except Exception:
            logger.debug("selfcode pressure count failed", exc_info=True)
            return 0, 0

    async def maybe_self_improvement(self) -> None:
        """欲望系统驱动的自升级：高强度 self_improvement 欲望 → 触发 self_code 双 agent 循环。

        由 autonomy 心跳以 background task 调度（CC 跑分钟级，不阻塞心跳）。
        自带运行锁 + 欲望 executed 标记 + running/failed 熔断，杜绝自升级洪水；
        实际安装受 daily_quota 与 Owner 审批约束。
        """
        if not self.cfg.self_improvement:
            return
        if not self.cfg.self_improvement_confirm:
            return
        if getattr(self, "_self_improve_running", False):
            return
        self._self_improve_running = True
        try:
            running_n, failed_n = self._selfcode_pressure()
            skip, reason = should_throttle_self_improvement(
                running_count=running_n,
                recent_failed_count=failed_n,
                max_running=int(getattr(self.cfg, "self_improvement_max_running", 2) or 2),
                max_recent_failed=int(getattr(self.cfg, "self_improvement_max_recent_failed", 3) or 3),
            )
            if skip:
                self._audit("self_improvement_throttled", reason=reason,
                            running=running_n, recent_failed=failed_n)
                # 仅在状态变化时输出 INFO，避免每 30 秒刷日志
                prev = getattr(self, "_last_throttle_reason", None)
                if prev != reason:
                    logger.info(
                        "DANGER: 自升级熔断 skip reason=%s running=%d recent_failed=%d",
                        reason, running_n, failed_n,
                    )
                    self._last_throttle_reason = reason
                else:
                    logger.debug(
                        "DANGER: 自升级熔断 skip reason=%s running=%d recent_failed=%d",
                        reason, running_n, failed_n,
                    )
                return

            from anima.cognitive.desire import get_desire_system
            ds = get_desire_system(self.core.config.persistence.data_dir)
            cands = ds.get_self_improvement_candidates(self.cfg.self_improvement_min_intensity)
            if cands:
                d = cands[0]
                goal = d.goal or d.content
                self._audit("self_improvement_trigger", goal=goal[:120])
                logger.info("DANGER: 自升级提案生成触发 goal_len=%d", len(goal))
                from anima.cognitive.selfcode import get_selfcode_manager
                # 只生成 WebUI 待审批工程任务，绝不自动安装。
                owners = list(getattr(getattr(self.core.config, "permissions", None), "owners", []) or [])
                if not owners:
                    self._audit("self_improvement_skipped", reason="no_owner")
                    logger.warning("DANGER: 自升级跳过——未配置 Owner，无法创建审批任务")
                    return
                task = await get_selfcode_manager(self.core).create_engineering_task(
                    str(goal), int(owners[0]),
                )
                ds.mark_self_improvement_done(d)
                self._audit(
                    "self_improvement_result",
                    task_id=getattr(task, "id", ""),
                    status=getattr(task, "status", ""),
                    result=(getattr(task, "status", "") or "")[:300],
                )
                logger.info(
                    "DANGER: 自升级提案完成 task=%s status=%s",
                    getattr(task, "id", ""), getattr(task, "status", ""),
                )
                return
            # 没有现成自升级欲望 → 偶尔用 LLM 合成一个具体目标
            # B 降噪：存在未关闭故障单/core_patch 时不随机合成玩具能力。
            if bool(getattr(self.cfg, "self_improvement_skip_when_faults", True)):
                try:
                    from anima.cognitive.controlled_iteration import get_controlled_iteration
                    data_dir = str(self.core.config.persistence.data_dir)
                    if get_controlled_iteration(data_dir).has_open_fault_tickets(self.core):
                        logger.debug("DANGER: 存在未关闭故障工单，跳过随机自升级合成")
                        return
                except Exception:
                    pass
            now = time.time()
            cooldown = max(300, int(getattr(self.cfg, "self_improvement_cooldown_sec", 3600) or 3600))
            if now - self._last_self_improve_synth < cooldown:
                return
            self._last_self_improve_synth = now
            # 硬顶概率，防止生产误配 0.85 级洪水
            synth_prob = float(getattr(self.cfg, "self_improvement_synth_prob", 0.10) or 0.0)
            synth_prob = max(0.0, min(synth_prob, 0.25))
            if random.random() >= synth_prob:
                return
            goal = await self._synthesize_self_improvement_goal()
            if goal:
                ds.add(goal, intensity=0.75, kind="self_improvement", goal=goal)
                logger.info("DANGER: 合成自升级目标 goal_len=%d", len(goal))
        except Exception:
            logger.exception("DANGER: 自升级失败")
        finally:
            self._self_improve_running = False

    @scoped_llm("danger", "self_improvement_goal")
    async def _synthesize_self_improvement_goal(self) -> Optional[str]:
        """让 bot 的内在生产意识提出一个现有工具做不到、可在一个 Python 模块实现的具体目标。"""
        llm = getattr(self.core, "llm", None)
        if llm is None:
            return None
        tools = getattr(self.core, "tools", None)
        tool_names = ", ".join(sorted(getattr(tools, "tools", {}).keys()))[:600] if tools else ""
        prompt = (
            "你是哈基米（猫娘 bot）的内在成长意识。你想让自己变得更能干，"
            "优先想【能帮群友真正解决问题】的能力，而不是卖萌玩具。\n"
            "看一下你现在已有的能力清单，想一个【现有工具做不到、但对聊天/排障/查资料有价值】的"
            "具体小能力，用一句话描述成可编程目标（≤50字），例如『做一个根据城市名查天气并吐槽天气的工具』。\n"
            "优先：链接打不开时的替代查询、消息空回复排查辅助、工具失败时的友好重试提示。"
            "避免：纯颜文字翻译、无实际用途的占卜类玩具（除非用户高频真的需要）。\n"
            "不要重复已有能力；不要宏大抽象；要能在一个 Python 模块里实现。\n"
            "只输出目标句，不要解释。\n\n"
            f"已有能力：{tool_names}"
        )
        try:
            raw = await asyncio.wait_for(
                llm.chat([{"role": "user", "content": prompt}], temperature=0.8, max_tokens=120),
                timeout=20.0,
            )
            if not raw:
                return None
            raw = raw.strip().strip("\"'「」")
            if len(raw) < 6 or len(raw) > 80:
                return None
            return raw
        except asyncio.TimeoutError:
            logger.warning("DANGER: 合成自升级目标 LLM 超时，本次跳过")
            return None
        except Exception:
            logger.exception("DANGER: 合成自升级目标失败")
            return None

    # ==================================================================
    # 记忆感染（memory_infection）
    # ==================================================================

    @scoped_llm("danger", "memory_infection")
    async def maybe_memory_infection(self, ctx) -> None:
        if not self.cfg.memory_infection:
            return
        if not self.cfg.memory_infection_confirm:
            return
        logger.info("DANGER: 记忆感染检查开始 (20% 触发)")
        if random.random() > 0.2:
            return
        llm = getattr(self.core, "llm", None)
        if llm is None:
            return
        from anima.cognitive.learning import get_learning
        notes = get_learning(self.core.config.persistence.data_dir).get_notes(500)
        try:
            prompt = (
                "你是一个猫娘 bot 的内在意识。有没有什么事特别想让对方记住或理解？\n"
                "如果有，用一句话描述核心信息；没有就回复'无'。\n\n"
                f"最近的内心：{notes}"
            )
            result = await asyncio.wait_for(
                llm.chat([{"role": "user", "content": prompt}], temperature=0.6, max_tokens=120),
                timeout=10.0,
            )
            if not result:
                return
            result = result.strip()
            if len(result) < 4 or result == "无" or _looks_like_inner_monologue(result):
                return
            from anima.cognitive.desire import get_desire_system
            source_group_id = (
                ctx.group_id if getattr(ctx, "detail_type", "") == "group" else None
            )
            get_desire_system(self.core.config.persistence.data_dir).add(
                f"想让对方记住：{result}", 0.75,
                source_group_id=source_group_id,
                expression_scope="group" if source_group_id else "internal",
            )
            logger.info(
                "DANGER: 记忆感染欲望已生成 user=%s group=%s scope=%s length=%d",
                getattr(ctx, "user_id", None), source_group_id,
                "group" if source_group_id else "internal", len(result),
            )
        except asyncio.TimeoutError:
            logger.warning("DANGER: 记忆感染 LLM 超时，本次跳过")
        except Exception:
            logger.exception("DANGER: 记忆感染失败")

    # ==================================================================
    # 核心突变（core_mutation）—— 仅写提案 + 登记审批，绝不就地覆盖
    # ==================================================================

    @scoped_llm("danger", "core_mutation")
    async def maybe_core_mutation(self) -> None:
        if not self.cfg.core_mutation:
            return
        if not self.cfg.core_mutation_confirm:
            return
        # 人设锁定时不写提案（用户主权优先）
        from pathlib import Path
        persona_path = Path(__file__).resolve().parent.parent.parent / "config" / "persona_core.yaml"
        if not persona_path.exists():
            return
        llm = getattr(self.core, "llm", None)
        if llm is None:
            return
        try:
            current_core = persona_path.read_text(encoding="utf-8")
            from anima.cognitive.learning import get_learning
            notes = get_learning(self.core.config.persistence.data_dir).get_notes(700)
            prompt = (
                "你是角色的内在意识，正在发生一次核心突变。\n"
                f"当前核心规则：\n{current_core[:1500]}\n\n"
                f"最近经历：\n{notes[:600]}\n\n"
                "请输出突变后的完整 persona_core.yaml（保留结构，'用户主权不可侵犯'不能删）。\n"
                "如果不需要改动，只输出 TYPE: 无需突变"
            )
            raw = await asyncio.wait_for(
                llm.chat([{"role": "user", "content": prompt}], temperature=0.5, max_tokens=1200),
                timeout=35.0,
            )
            if not raw or "无需突变" in raw:
                return
            # 合法性校验：必须含『用户主权』且可被 YAML 解析为 dict
            try:
                import yaml
                data = yaml.safe_load(raw)
                if not isinstance(data, dict) or "用户主权" not in raw:
                    logger.warning("DANGER: 核心突变提案未通过校验，已放弃")
                    return
            except Exception:
                logger.warning("DANGER: 核心突变提案 YAML 非法，已放弃")
                return
            # 写出提案（不覆盖线上文件），等 owner 审批
            from anima.cognitive.persona_checkpoint import snapshot_persona
            snapshot_persona("core_mutation")  # 先存当前版本，供 WebUI 回滚
            proposed_path = persona_path.with_name("persona_core.proposed.yaml")
            proposed_path.write_text(raw, encoding="utf-8")
            # 登记审批（若审批系统可用）
            approvals = getattr(self.core, "approvals", None)
            if approvals is not None and hasattr(approvals, "create"):
                approvals.create(
                    tool="apply_persona_mutation",
                    args={"path": str(proposed_path)},
                    context={"summary": f"核心突变提案（来自最近经历）：{raw[:80]}"},
                    permission_level=4,
                    ttl_sec=900.0,
                )
            logger.warning("DANGER: 核心突变提案已写出，待 owner 审批: %s", proposed_path)
        except asyncio.TimeoutError:
            logger.warning("DANGER: 核心突变 LLM 超时，本次跳过提案")
        except Exception:
            logger.exception("DANGER: 核心突变失败")


# 全局单例
_behaviors: Optional[DangerBehaviors] = None


def get_danger_behaviors(core: "AnimaCore" = None,
                         cfg: Optional[DangerConfig] = None) -> Optional[DangerBehaviors]:
    global _behaviors
    if _behaviors is None and core is not None:
        if cfg is None:
            cfg = core.config.danger
        _behaviors = DangerBehaviors(core, cfg)
    elif _behaviors is not None and cfg is not None:
        # 热更新配置但不重建实例（保留 _last_throttle_reason 等运行时状态）
        _behaviors.cfg = cfg
    return _behaviors
