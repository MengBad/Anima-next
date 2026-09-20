"""AnimaCore —— 编排核：持有所有模块，统一生命周期、事件分发。

服务注册表：
- ``register(module)``: 注册 Module 生命周期（init/restore/persist/shutdown）
- ``register_service(name, obj)``: 注册命名服务（认知模块、工具注册表等）
- ``service(name)``: 按名查找服务
- ``cognitive_modules``: 所有已注册的 CognitiveModule 实例
"""

from __future__ import annotations
import asyncio
import logging
import signal
from typing import Any, Optional

from anima.core.config import AnimaConfig
from anima.core.event_bus import EventBus
from anima.core.gateway import Gateway
from anima.core.persistence import PersistenceProvider
from anima.core.module import Module, CognitiveModule

logger = logging.getLogger("anima.core")

STICKER_POLICY_FIELDS = {
    "enabled", "auto_collect", "collect_mode", "collect_probability",
    "collect_cooldown_sec", "collect_min_interval", "max_stickers",
    "require_owner_review", "pending_capacity", "default_scope",
    "share_across_groups", "classify_at_collect", "llm_select",
    "send_probability", "send_by_emotion", "send_cooldown_sec",
    "collect_allowlist", "collect_blocklist", "send_allowlist", "send_blocklist",
    "self_review", "natural_forget",
    "forget_age_days", "forget_used_max", "forget_last_used_days",
    "phash_dedup", "phash_threshold", "smart_select",
}


class AnimaCore:
    def __init__(self, config: AnimaConfig, identity_context: Any = None):
        self.config = config
        # 由 cli 注入真实 config.yaml 路径；测试/嵌入式调用可保持 None，
        # ConfigService 此时只做内存变更，不会误写当前工作目录。
        self.config_path = None
        # Optional Phase 5.3 observation context. It is created only after an
        # explicit attestation passes; Core exposes it read-only and never
        # consults it for permissions, lifecycle, memory, or recovery.
        self._runtime_identity_context = identity_context
        self.bus = EventBus()
        self.persistence = PersistenceProvider(config.persistence.data_dir)
        # 服务注册表：统一管理认知模块、工具注册表等命名服务
        self._services: dict[str, Any] = {}
        from anima.core.health_history import HealthHistory
        self.health_history = HealthHistory(self, config.persistence.data_dir)
        self._restore_sticker_policy()
        from anima.core.permissions import PermissionStore
        self.permission_store = PermissionStore(self.persistence, config)
        from anima.core.tasks import TaskStore
        self.tasks = TaskStore(self.persistence)
        from anima.core.tasks import OwnerTaskAgent
        self.task_agent = OwnerTaskAgent(self)
        # Phase 3/4：Owner 多步 Agent（规划器/执行器/存储/审计/通知）
        from anima.agent.owner_agent import OwnerAgent
        self.agent = OwnerAgent(self)
        self.modules: list[Module] = []
        self.gateway = Gateway(self)
        self.llm = None  # P2: LLM client
        self.usage_ledger = None
        self.llm_handler = None  # 入站消息处理器（可选持有状态）
        self.tools = None  # P4: ToolRegistry
        self.native_plugins = None
        self.heartbeat = None  # P5: Heartbeat
        self.vector_memory = None  # P2/P3: VectorMemory
        self.sylann = None  # 可选 Sylann 适配器（默认 disabled）
        self.monitors = None
        self.webui = None
        self.skill_drafts = None
        from anima.cognitive.group_presence import (
            get_group_presence,
            set_group_presence_data_dir,
        )
        set_group_presence_data_dir(config.persistence.data_dir)
        self.group_presence = get_group_presence(config.persistence.data_dir)
        from anima.cognitive.agenda import AgendaQueue
        self.agenda = AgendaQueue(self.persistence)
        from anima.core.intents import IntentLedger, SubjectOrchestrator
        self.intent_ledger = IntentLedger(self.persistence)
        self.subject = SubjectOrchestrator(self.intent_ledger)
        from anima.memory.episodes import EpisodicMemory
        self.episodes = EpisodicMemory(self.persistence)
        # ④ long_term 第三层（事件→知识→行为模式；默认仅装配，注入关由 memory.long_term_context 控制）
        from anima.memory.long_term import LongTermMemory
        self.long_term = LongTermMemory(config.persistence.data_dir)
        from anima.core.workflows import WorkflowStore
        self.workflows = WorkflowStore(self.persistence)
        # 意图过期时同步关闭等待交付的工作流，避免悬挂通知。
        self.intent_ledger.on_expired = (
            lambda intent_id: self.workflows.fail_by_intent(intent_id, "关联意图已过期")
        )
        from anima.core.backup import BackupManager
        self.backups = BackupManager(self)
        from anima.core.evaluation import RuntimeEvaluator
        self.evaluator = RuntimeEvaluator(self)
        from anima.core.replay import BehaviorReplay
        self.behavior_replay = BehaviorReplay(self)
        from anima.cognitive.calendar import CalendarStore
        self.calendar = CalendarStore(self.persistence)
        from anima.core.audit import AuditLogger
        self.audit = AuditLogger(config.persistence.data_dir)
        # R16 闭环4：敏感配置/权限漂移基线（启动即锚定当前配置为基线）。
        from anima.core.config_drift import ConfigDriftMonitor
        self.config_drift = ConfigDriftMonitor(config, actor="bootstrap")
        from anima.cognitive.reply_quality import get_reply_quality
        self.reply_quality = get_reply_quality(config.persistence.data_dir)
        from anima.core.approvals import ApprovalStore
        self.approvals = ApprovalStore(self.persistence)
        from anima.media_generation import ImageGenerationManager
        self.image_generation = ImageGenerationManager(self)
        self._auto_start_heartbeat = False
        self._running = False
        self._shutdown_event: Optional[asyncio.Event] = None

    @property
    def runtime_identity_context(self) -> Any:
        """Return the immutable attested identity context, if explicitly bound."""

        return self._runtime_identity_context

    def _restore_sticker_policy(self) -> None:
        """恢复 WebUI 保存的表情策略，不接触含密钥的主配置文件。"""
        raw = self.persistence.read_json("sticker_policy.json", {}) or {}
        if not isinstance(raw, dict):
            return
        updates = {key: value for key, value in raw.items() if key in STICKER_POLICY_FIELDS}
        if not updates:
            return
        try:
            model = type(self.config.sticker)
            self.config.sticker = model.model_validate({
                **self.config.sticker.model_dump(), **updates,
            })
        except Exception:
            logger.exception("表情策略恢复失败，继续使用主配置")

    def update_sticker_policy(self, updates: dict) -> dict:
        """校验并持久化 WebUI 表情策略；返回可安全展示的策略快照。"""
        filtered = {key: value for key, value in updates.items() if key in STICKER_POLICY_FIELDS}
        if not filtered:
            raise ValueError("没有可更新的表情策略")
        model = type(self.config.sticker)
        try:
            configured = model.model_validate({
                **self.config.sticker.model_dump(), **filtered,
            })
        except Exception as exc:
            raise ValueError(f"表情策略无效：{exc}") from exc
        self.config.sticker = configured
        snapshot = {
            key: getattr(configured, key) for key in sorted(STICKER_POLICY_FIELDS)
        }
        self.persistence.write_json("sticker_policy.json", snapshot)
        try:
            from anima.cognitive.stickers import configure_sticker_manager
            configure_sticker_manager(configured)
        except Exception:
            logger.exception("表情管理器热更新失败；策略将在下次调用时生效")
        return snapshot

    @property
    def event_bus(self) -> EventBus:
        """别名，兼容 channel adapter 引用。"""
        return self.bus

    # ---- 注册 -----------------------------------------------------------

    def register(self, module: Module) -> None:
        self.modules.append(module)

    def register_service(self, name: str, service: Any) -> None:
        """注册命名服务到注册表。认知模块、工具注册表等均可通过此统一管理。"""
        self._services[name] = service

    def service(self, name: str, default: Any = None) -> Any:
        """按名查找已注册服务。"""
        return self._services.get(name, default)

    @property
    def cognitive_modules(self) -> list[CognitiveModule]:
        """所有已注册的 CognitiveModule 实例。"""
        return [s for s in self._services.values() if isinstance(s, CognitiveModule)]

    def cognitive_status(self) -> dict[str, Any]:
        """所有认知模块的状态快照（WebUI /api/modules/status 使用）。"""
        result = {}
        for name, svc in self._services.items():
            if isinstance(svc, CognitiveModule):
                try:
                    result[name] = svc.get_status()
                except Exception:
                    result[name] = {"name": name, "ok": False, "error": "get_status 异常"}
        return result

    def propose_agenda(self, group_id: int, text_intent: str, *, priority: float = 0.5,
                       not_before_at: float = 0.0, expires_at: float = 0.0,
                       source: str = "system"):
        """认知层和 owner 命令创建主动表达意图的唯一入口。"""
        from anima.cognitive.agenda import AgendaItem, is_garbled_text
        # P0/P1: 乱码/无意义内容不创建议程，避免 LLM 编造回复
        if is_garbled_text(text_intent):
            logger.warning("议程拒绝乱码内容 source=%s text=%r", source, text_intent[:80])
            return None
        intent = self.subject.propose_expression(
            group_id, text_intent, source=source, priority=priority,
            not_before_at=not_before_at, expires_at=expires_at,
        )
        item = self.agenda.push(AgendaItem(
            group_id, text_intent, priority, not_before_at, expires_at,
            intent_id=intent.id, source=source,
        ))
        self.audit.emit("agenda_created", agenda_id=item.id, group_id=item.group_id,
                        priority=item.priority, intent_id=intent.id, source=source)
        return item

    # ---- 生命周期 -------------------------------------------------------

    async def start(self) -> None:
        logger.info("AnimaCore 启动 …")
        # 初始化认知单例并恢复状态
        self._init_cognitive_state()
        from anima.cognitive.sylann_adapter import SylannAdapter, EngineSylannAdapter
        sylann_cfg = self.config.sylann
        if sylann_cfg.enabled:
            try: self.sylann = EngineSylannAdapter(self.persistence, self, sylann_cfg.mode)
            except Exception: self.sylann = SylannAdapter(self.persistence, "disabled")
        else: self.sylann = SylannAdapter(self.persistence, "disabled")
        # 初始化向量记忆
        await self._init_vector_memory()
        try:
            from anima.monitors.scheduler import MonitorScheduler
            self.monitors = MonitorScheduler(self)
            await self.monitors.start()
        except Exception:
            logger.exception("订阅调度器初始化失败，已隔离")
        if self.tools is not None:
            try:
                from anima.plugins import NativePluginRuntime
                self.native_plugins = NativePluginRuntime(self)
                await self.native_plugins.load_all()
            except Exception:
                self.native_plugins = None
                logger.exception("原生插件运行时初始化失败，已隔离")

        for m in self.modules:
            await m.restore()
        for m in self.modules:
            await m.init(self)

        # 注册社交行为监听（notice 事件：入群/退群/戳一戳）
        try:
            from anima.cognitive.social_reactions import setup_social_listeners
            setup_social_listeners(self)
        except Exception:
            logger.exception("社交行为监听注册失败，已隔离")

        # 注入 X 浏览器会话单例（带生产配置；未启用则禁用态，不报错）
        try:
            from anima.cognitive.x_browser import set_x_browser
            set_x_browser(self)
        except Exception:
            logger.exception("X 浏览器会话初始化失败，已隔离")

        if self.config.webui.enabled:
            try:
                from anima.webui import WebUIServer
                self.webui = WebUIServer(self)
                await self.webui.start()
                logger.info("WebUI 已启动: http://%s:%s", self.config.webui.host, self.config.webui.port)
            except Exception:
                self.webui = None
                logger.exception("WebUI 启动失败，已隔离")

        # 自动启动心跳
        if self._auto_start_heartbeat and self.heartbeat:
            await self.heartbeat.start()

        # 到这里 WebUI、认知模块和心跳均已完成装配；若本次是配置变更后的
        # 候选启动，写入健康点。下一次启动若仍看见 attempting，就会恢复备份。
        try:
            from anima.core.config_recovery import mark_healthy
            mark_healthy(self.config.persistence.data_dir)
        except Exception:
            logger.exception("配置候选健康标记失败，保留 recovery marker")

        # C 地基: 开机(chat_memory 已 restore)把已持久化 chat_memory 水合进宪法记忆(M1.2, 只读/有界/fail-soft)
        try:
            from anima.constitution.memory_authority import MemoryAuthority
            self.memory_authority = MemoryAuthority(hydrate_now=True)
            _count = len(self.memory_authority.snapshot_memory().episodes)
            self.audit.emit("constitution_memory_hydrated", added=_count, source="boot")
            logger.info("宪法记忆开机水合: 共 %d 条 episode(只读, 有界)", _count)
        except Exception:  # noqa: BLE001 - 水合失败只隔离, 不阻断启动
            self.memory_authority = None
            logger.exception("宪法记忆开机水合失败, 已隔离")
        # R-fix: 运行时身份连续性探针 —— 每次开机真实调用 continuity 校验(fail-soft,
        # 无 identity/memory 工件则诚实验证空转, 让该契约不再是纯死代码)。
        try:
            from pathlib import Path as _Path
            from anima.memory.continuity import verify_continuity as _verify_continuity
            _did = str(self.config.persistence.data_dir)
            _files = sorted(_Path(_did).glob("*identity*.json")) + sorted(_Path(_did).glob("*.memory.json"))
            if _files:
                _target = _files[0]
                _vc = _verify_continuity(_target)
                self.audit.emit("runtime_identity_continuity",
                                valid=bool(getattr(_vc, "valid", False)),
                                path=str(_target),
                                record_count=int(getattr(_vc, "record_count", 0)))
                logger.info("运行时身份连续性校验: valid=%s records=%s",
                            getattr(_vc, "valid", None), getattr(_vc, "record_count", 0))
            else:
                logger.info("运行时身份: 尚无 identity/memory 工件, continuity 校验空转(fail-open)")
        except Exception:  # noqa: BLE001 - 连续性校验失败只隔离, 不阻断启动
            logger.exception("运行时身份连续性校验失败, 已隔离")

        # 周期水合: 让宪法记忆随运行持续累积(dedupe), 不依赖冷启动现场
        try:
            self._hydrate_loop_task = asyncio.create_task(self._memory_hydrate_loop())
        except Exception:  # noqa: BLE001 - 周期水合失败只降级, 不阻断启动
            self._hydrate_loop_task = None
            logger.exception("周期水合任务启动失败, 降级为仅开机一次")

        self._running = True
        self._shutdown_event = asyncio.Event()
        try:
            self.health_history.record(force=True)
        except Exception:
            logger.exception("运行健康初始采样失败")
        logger.info("AnimaCore 已启动，注册模块 %d 个", len(self.modules))

    async def _memory_hydrate_loop(self) -> None:
        """周期性把实时 chat_memory 水合进宪法记忆(M1.2, dedupe), 随运行累积。"""
        _cfg = getattr(getattr(self, "config", None), "memory", None)
        _hours = getattr(_cfg, "hydrate_period_hours", 6.0) or 6.0
        try:
            _hours = float(_hours)
        except (TypeError, ValueError):
            _hours = 0.0
        if not (_hours > 0):  # 缺失/0/负/非数字均回退默认，避免负数导致忙循环
            _hours = 6.0
        period = float(_hours) * 3600.0
        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=period)
                break  # shutdown 已置位
            except asyncio.TimeoutError:
                pass
            try:
                auth = getattr(self, "memory_authority", None)
                if auth is None:
                    from anima.constitution.memory_authority import MemoryAuthority
                    auth = MemoryAuthority(hydrate_now=True)
                    self.memory_authority = auth
                _added = auth.hydrate_from_chat(force=True, dedupe=True)
                if _added:
                    _a = int(_added)
                    self.audit.emit("constitution_memory_hydrated", added=_a, source="periodic")
                    logger.info("宪法记忆周期水合: 新增 %d 条 episode", _a)
            except Exception:  # noqa: BLE001 - 周期水合失败只隔离, 不影响生命
                logger.exception("宪法记忆周期水合失败, 已隔离")

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._shutdown_event:
            self._shutdown_event.set()
        _ht = getattr(self, "_hydrate_loop_task", None)
        if _ht:
            try:
                _ht.cancel()
            except Exception:
                pass
        if self.heartbeat:
            await self.heartbeat.stop()
            try:
                self.heartbeat.persist()
            except Exception:
                logger.exception("心跳持久化失败")
        for m in reversed(self.modules):
            try:
                await m.persist()
                await m.shutdown()
            except Exception:
                logger.exception("模块 shutdown 异常: %s", getattr(m, "name", m))
        # 保存认知状态
        self._persist_cognitive_state()
        # 保存对话记忆
        try:
            from anima.memory.chat_memory import get_chat_memory
            get_chat_memory().save()
        except Exception:
            pass
        # 关闭 AnimaCore 直接持有的资源（未注册为 Module，否则 aiohttp 会话泄漏）。
        # - self.vector_memory: Embedder / Reranker 的 HTTP 会话
        # - self.llm: LLMClient 的 aiohttp 会话（cli.py 中赋值）
        for _res in (self.vector_memory, self.llm):
            _close = getattr(_res, "close", None)
            if callable(_close):
                try:
                    await _close()
                except Exception:
                    logger.exception("资源关闭异常: %s", type(_res).__name__)
        # 关闭 media 模块级共享 ClientSession（否则退出时 Unclosed client session）
        try:
            from anima.media import close_media_session
            await close_media_session()
        except Exception:
            logger.exception("media 会话关闭异常")
        if self.sylann:
            try:
                await self.sylann.close()
            except Exception:
                logger.exception("Sylann 适配器关闭异常")
        handler = getattr(self, "llm_handler", None)
        if handler:
            try:
                await handler.close()
            except Exception:
                logger.exception("LLM 处理器关闭异常")
        if self.monitors:
            try:
                await self.monitors.close()
            except Exception:
                logger.exception("订阅调度器关闭异常")
        if self.native_plugins:
            try:
                await self.native_plugins.close()
            except Exception:
                logger.exception("原生插件运行时关闭异常")
        if self.webui:
            try:
                await self.webui.close()
            except Exception:
                logger.exception("WebUI 关闭异常")
        # G1：跨群记忆/氛围 JSON 已增量落盘，此处幂等 flush 收尾
        try:
            from anima.crossgroup import close_crossgroup_manager
            close_crossgroup_manager()
        except Exception:
            logger.exception("跨群管理器 flush 异常")
        # P0/P1：工具自学习 / 能力系统单例关闭
        try:
            from anima.cognitive.tool_learning import close_tool_learning
            close_tool_learning()
        except Exception:
            logger.exception("工具自学习单例关闭异常")
        try:
            from anima.cognitive.capabilities import close_capability_manager
            close_capability_manager()
        except Exception:
            logger.exception("能力系统单例关闭异常")
        # 表情包单例关闭
        try:
            from anima.cognitive.stickers import close_sticker_manager
            close_sticker_manager()
        except Exception:
            logger.exception("表情包单例关闭异常")
        # 视觉理解单例关闭
        try:
            from anima.cognitive.vision import close_vision_manager
            close_vision_manager()
        except Exception:
            logger.exception("视觉理解单例关闭异常")
        try:
            from anima.cognitive.reply_quality import close_reply_quality
            close_reply_quality()
        except Exception:
            logger.exception("回复质量统计关闭异常")
        try:
            from anima.cognitive.selfcode import close_selfcode_manager
            close_selfcode_manager()
        except Exception:
            logger.exception("自我编程管理器关闭异常")
        logger.info("AnimaCore 已停止")

    # ---- 辅助 -----------------------------------------------------------

    async def _init_vector_memory(self) -> None:
        """如果启用，初始化向量记忆。"""
        from anima.memory.vector_memory import create_vector_memory
        try:
            self.vector_memory = create_vector_memory(self.config)
            if self.vector_memory:
                logger.info("向量记忆已启用: %s", self.config.vector.db_path)
                # P3-1：把 embedder 回填给长期记忆，供「可选」语义召回读路径使用。
                # 这一步只搬引用、不触发任何调用——真正是否启用由
                # `memory.long_term_semantic` 在查询侧决定（默认 OFF）。
                try:
                    if getattr(self, "long_term", None) is not None:
                        self.long_term.embedder = getattr(
                            self.vector_memory, "embedding", None)
                except Exception:  # noqa: BLE001 - 回填失败不影响向量记忆本身
                    logger.debug("long_term embedder 回填失败，语义召回不可用", exc_info=True)
        except Exception:
            logger.exception("向量记忆初始化失败，已禁用")
            self.vector_memory = None

    def _init_cognitive_state(self) -> None:
        """确保认知单例使用正确的数据目录并恢复。"""
        from anima.core import paths
        paths.set_data_dir(self.config.persistence.data_dir)  # P2-B：中央路径门面唯一接线点
        data_dir = str(paths.data_dir())
        from anima.memory.chat_memory import get_chat_memory, set_chat_memory_persist_path
        set_chat_memory_persist_path(str(paths.child("chat_memory.json")))  # P2-A+P2-B：无参也落盘
        get_chat_memory()
        from anima.cognitive.time_sense import get_time_sense, set_time_sense_data_dir
        set_time_sense_data_dir(data_dir)  # M3：get_time_sense() 默认归一到配置目录，消 split-brain
        get_time_sense(data_dir)
        from anima.cognitive.desire import get_desire_system
        from anima.cognitive.relation import (
            get_relation,
            set_relation_data_dir,
            set_seed_relations,
        )
        from anima.crossgroup import configure_crossgroup_manager
        get_time_sense(data_dir)
        get_desire_system(data_dir)
        from anima.cognitive.mood import get_mood_system, set_mood_data_dir
        set_mood_data_dir(data_dir)  # M3：get_mood_system() 默认归一到配置目录，消 split-brain
        get_mood_system(data_dir)
        set_relation_data_dir(data_dir)  # M2.4：get_relation() 默认归一到配置目录，消 split-brain
        set_seed_relations(
            getattr(getattr(self.config, "persona", None), "seed_relations", None))
        get_relation(data_dir)
        configure_crossgroup_manager(data_dir)  # M2.5：跨群数据落到配置目录/crossgroup
        from anima.cognitive.stickers import get_sticker_manager
        get_sticker_manager(data_dir, config=self.config.sticker)

    def _persist_cognitive_state(self) -> None:
        """保存认知单例状态。"""
        try:
            from anima.cognitive.time_sense import get_time_sense
            get_time_sense().persist()
        except Exception:
            logger.exception("时间感知持久化失败")
        try:
            from anima.cognitive.desire import get_desire_system
            get_desire_system().persist()
        except Exception:
            logger.exception("欲望系统持久化失败")
        try:
            from anima.cognitive.relation import get_relation
            get_relation().persist()
        except Exception:
            logger.exception("关系引擎持久化失败")

    async def emit(self, topic: str, payload=None) -> None:
        """向 EventBus 发布事件。"""
        await self.bus.publish(topic, payload)

    def run(self) -> None:
        """阻塞入口：asyncio.run 启动并保持。"""
        try:
            asyncio.run(self._run_loop())
        except KeyboardInterrupt:
            logger.info("收到键盘中断")

    async def _run_loop(self) -> None:
        loop = asyncio.get_running_loop()

        def _on_signal(sig):
            logger.info("收到信号 %s，开始优雅停止", sig)
            asyncio.create_task(self.stop())

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _on_signal, sig.name)
            except (NotImplementedError, ValueError):
                pass  # Windows 不支持 SIGTERM

        await self.start()
        # 启动 systemd 看门狗：事件循环一旦死锁（CPU 自旋或同步阻塞），
        # 心跳 ping 无法调度，systemd 自动 SIGKILL 重启，无需人工干预。
        from anima.watchdog import start_watchdog
        start_watchdog(loop)
        try:
            if self._shutdown_event:
                await self._shutdown_event.wait()
            else:
                while self._running:
                    await asyncio.sleep(1)
        finally:
            await self.stop()
