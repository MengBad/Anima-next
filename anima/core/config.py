"""ValidatedConfig —— pydantic 校验的全局配置，所有设计决策默认值已编码。"""

from __future__ import annotations
import os
from pathlib import Path
from typing import Optional, Any

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator


class QQConfig(BaseModel):
    # Staging can run the whole cognitive/WebUI stack without opening a second
    # connection to NapCat or ever replying in the production QQ groups.
    enabled: bool = True
    napcat_ws_url: str = "ws://127.0.0.1:3001"
    napcat_http_url: str = "http://127.0.0.1:3001"
    self_id: Optional[int] = None
    access_token: Optional[SecretStr] = None
    event_concurrency: int = 12       # 同时执行业务事件上限，WS 回执仍独立即时处理
    max_pending_events: int = 128     # 高峰保护；超过后只丢新事件并计入健康指标
    typing_indicator: bool = True     # 私聊回复前发送“正在输入...”状态（NapCat 扩展）
    known_bot_ids: list[int] = Field(default_factory=list)  # 多 bot 环境：显式标注其他 bot 的 QQ，用于归属/过滤
    #: 🔴 bot 发现的开关【不在这里】，在 `SocialConfig.bot_discovery_enabled`
    #: （social 段，与 bot_social_enabled / bot_talk_enabled 同族）。2026-10-14 修：
    #: 曾误在此处新增同名字段，造成两处同名开关，已删除。


class LLMConfig(BaseModel):
    endpoint: str = "https://api.openai.com/v1"
    api_key: SecretStr = SecretStr("")
    model: str = "gpt-4o-mini"
    embedding_endpoint: Optional[str] = None   # null  => 同 endpoint
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536
    timeout: float = 60.0
    retries: int = 2
    retry_backoff_sec: float = 0.6
    fallback_models: list[str] = Field(default_factory=list)  # 同一兼容网关内的备用模型，空=不切换
    #: 🔴 **异族兜底**（2026-10-06，72h 生产账本驱动）
    #:
    #: 实测 72h：`gemini-3.8-flash-n` 1952/1952 **100%**、`gemini-3.8-flash` 377/377 100%，
    #: 而降级链两级 `gemini-3.6-flash` 96.5%、`gemini-3-flash` **0%**。
    #: ⇒ 主模型从不出问题；**一旦降级两级都在同一个 provider（google）**，
    #: 而同 provider 的「200-空」是**上游整体抖动**，换同族模型救不回来
    #: （00:25 用户面事故即此形状：`-high` 返空 → `3.6` 返空 → 用户看到兜底话术）。
    #:
    #: 所以：**异族** provider 的模型是治「同族全灭」的唯一结构性兜底。
    #: 空列表（默认）⇒ 行为与从前**逐字相同**，是零影响回滚点。
    #: 🔴 最多生效 **1 档**（链长上限 4 → 5），避免最坏延迟被拖长。
    extra_fallback_models: list[str] = Field(default_factory=list)
    circuit_failure_threshold: int = 3  # 连续失败达到此值后短暂熔断，保护 QQ 主链路
    circuit_cooldown_sec: float = 30.0
    # 可选的命名提供商配置。保留旧字段是为了兼容现有 config.yaml；
    # WebUI 的模型控制中心优先使用 active_provider，未配置时回退到旧字段。
    providers: dict[str, dict[str, Any]] = Field(default_factory=dict)
    active_provider: str = "default"
    # module -> model；空字符串表示继承默认模型。调用点显式传 model 时优先级更高。
    module_models: dict[str, str] = Field(default_factory=dict)
    # module -> reasoning_effort（2026-10-03）。**只写顶层**参数，嵌套写法静默无效。
    #   None（缺省） ⇒ 用代码默认表（`llm._DEFAULT_REASONING_EFFORT`：sticker_label/vision=minimal）
    #   {}           ⇒ 一键**全关**（不下发任何 reasoning_effort）
    #   非空 dict    ⇒ 整体替换默认表（**不是**叠加）
    # 只对 gemini 系下发（`_SUPPORTED_REASONING_EFFORT_PREFIXES`）；其余模型硬发可能 400。
    module_reasoning_effort: Optional[dict[str, str]] = None
    # model -> {input: price_per_million, output: price_per_million}
    model_prices: dict[str, dict[str, float]] = Field(default_factory=dict)

    def provider_config(self, provider_id: str | None = None) -> dict[str, Any]:
        """Return a normalized provider config without exposing secrets."""
        provider_id = provider_id or self.active_provider
        raw = self.providers.get(provider_id) if isinstance(self.providers, dict) else None
        if isinstance(raw, dict):
            return {
                "endpoint": raw.get("endpoint", self.endpoint),
                "api_key": raw.get("api_key", self.api_key),
                "model": raw.get("model", self.model),
                "timeout": raw.get("timeout", self.timeout),
                "retries": raw.get("retries", self.retries),
                "retry_backoff_sec": raw.get("retry_backoff_sec", self.retry_backoff_sec),
                "fallback_models": raw.get("fallback_models", self.fallback_models),
                "extra_fallback_models": raw.get("extra_fallback_models", self.extra_fallback_models),
                "circuit_failure_threshold": raw.get("circuit_failure_threshold", self.circuit_failure_threshold),
                "circuit_cooldown_sec": raw.get("circuit_cooldown_sec", self.circuit_cooldown_sec),
            }
        return {
            "endpoint": self.endpoint,
            "api_key": self.api_key,
            "model": self.model,
            "timeout": self.timeout,
            "retries": self.retries,
            "retry_backoff_sec": self.retry_backoff_sec,
            "fallback_models": self.fallback_models,
            "extra_fallback_models": self.extra_fallback_models,
            "circuit_failure_threshold": self.circuit_failure_threshold,
            "circuit_cooldown_sec": self.circuit_cooldown_sec,
        }


class VectorConfig(BaseModel):
    enabled: bool = True
    db_path: str = "data/anima.db"
    embedding_backend: str = "remote"          # 本期仅 remote
    cache: bool = True
    # —— Embedding 后端（OpenAI 兼容 /embeddings，默认 SiliconFlow BAAI/bge-m3） ——
    embedding_endpoint: str = "https://api.siliconflow.cn/v1"
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = 1024
    embedding_api_key: SecretStr = SecretStr("")
    # —— Reranker 后端（/v1/rerank，默认 SiliconFlow BAAI/bge-reranker-v2-m3） ——
    rerank_enabled: bool = True
    rerank_endpoint: str = "https://api.siliconflow.cn/v1/rerank"
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_api_key: SecretStr = SecretStr("")
    rerank_top_n: int = 5


class AutonomyConfig(BaseModel):
    enabled: bool = True
    heartbeat_tick_sec: float = 30.0
    cooldown_sec: int = 90
    daily_max_per_group: int = 20
    ratio_max: float = 0.30
    token_budget_per_day: int = 0        # 0 = 不限额（LLM 用量全开；仍保留超时/重试/熔断/并发锁与群打扰控制）

    # —— 回复策略 ——
    # 群聊中仅当被 @、被回复或文本提到「哈基米」时回复；私聊始终回复。
    # 可被 permissions.group_policies[gid].reply_mode 覆盖：
    #   mention | familiar | open
    reply_only_when_mentioned: bool = True
    # familiar 模式：亲密度达到该阈值时，无需 @ 也可接话（仅对该群策略生效）。
    familiar_reply_min: float = 0.35
    # 群内其它 bot 名字：句首点到他们且未叫「哈基米」时不抢话（可被 group_policies 覆盖）。
    other_bot_names: list[str] = Field(
        default_factory=lambda: ["沙琪玛", "沙其玛", "莎琪玛", "aki", "阿基"],
    )
    # 唤醒会话窗：被 @/回复/喊名后，窗内非刷屏消息可自然接续的时长（秒）。
    # 调短可显著降低「被叫一次后连续接很多句」的连环刷屏。默认 240→改为 60。
    wake_session_ttl_sec: float = 60.0
    # 每用户被动回复冷却（秒）：对同一用户，距上次 bot 已回复 < 该秒数时，
    # 若这次消息未被 @/回复（纯被动接话）则跳过，避免对熟人逐条抢答。
    # 0 = 关闭。这个不阻断被 @ 的直接呼叫。
    passive_reply_cooldown_sec: float = 45.0
    # 会话连续性：唤醒窗内，非@消息若命中纠正/补充词（"不对，我说的是X"），
    # 豁免上面的被动回复冷却——那是对话仍在进行的信号，不是刷屏。
    # 双闸门 = 命中词表 ∧ 唤醒窗活跃；关闭则恢复一律冷却。
    correction_exempt_cooldown: bool = True

    # —— R1（2026-10-12）：熟悉度质量闸门 ——
    # 修复前 `RelationEngine.touch()` 的 familiarity 增益**无条件 +0.02**，与消息
    # 内容好坏无关 ⇒ 群里「骂 bot」也算一次交互、照样涨熟度 ⇒ 生产 8 个高频用户
    # 里 6 个 fam = 1.000 封顶，而熟人放行门槛（`familiar_min` = 0.85）对他们
    # **恒成立** ⇒ 只要 >12 字、非闲聊短语、非纯图、非 @ 别人，就一律放行
    # ⇒ 观感「没叫它也回」，且**骂它反而喂它熟悉度**（正反馈环）。
    # 打开后：关系已破裂（信任见底 / 情绪显著负面）时熟悉度改为**缓慢下降**，
    # 使其重新落回门槛以下。False = 完全回到修复前行为（应急回滚）。
    familiarity_quality_gate: bool = True
    familiarity_gain: float = 0.02            # 正常交互增量（原值）
    familiarity_gain_decay: float = 0.5       # 已 >0.6 时的增益衰减（原值）
    familiarity_bad_trust: float = 0.3        # 信任 <= 此值 ⇒ 关系已破裂
    familiarity_bad_valence: float = -0.3     # 情绪价态 <= 此值 ⇒ 关系已破裂
    familiarity_bad_gain: float = -0.01       # 破裂时每次交互的增量（缓慢疏远）
    familiarity_min: float = 0.03             # 下限保护
    familiarity_max: float = 1.0             # 上限（原语义）

    # 社交疲劳阈值（第四轮体验测试 C2）：5 分钟窗口内被 @/引用次数超过 soft
    # 触发 overwhelmed 情绪、超过 hard 直接罐头短回复跳过 LLM（owner 豁免硬闸）。
    # 此前硬编码 6/8 不可调，管理员长会话也被误拦。
    fatigue_mention_soft: int = 6
    fatigue_mention_hard: int = 8

    # —— 主动发言（自主心跳 speak）策略 ——
    # 通过全部主动发言门控后的实际开口概率。默认保守，避免群里突然刷屏。
    active_reply_prob: float = 0.08
    # Candidate B：主动发言接地网关。True=出站前校验回复不可虚构前提/断言群友
    # 心理状态/离题；未接地一律丢弃。默认开启（fail-closed 行为加固）。
    proactive_grounding: bool = True
    # T1.1: Constitution(M1) Drive 门。True=主动表达须源于趋近 Drive(approach>avoid)才开口，
    #      且把 Drive 记入审计 trace。默认关(fail-closed,行为不变)。Constitution 只改意图/表达倾向，不改出站语言。
    # 2026-09-18（B1，审计整改）：Drive 桥链首改为读**真实水合记忆**
    #      (`core.memory_authority.snapshot_memory()`)，原为现场合成替身 Memory。
    # 🔴 翻转此门之前必读：水合目前只产 `episode`，而 `derive_identity` 只把 `semantic`
    #      的数值字段聚成 traits ⇒ 真实记忆下 `identity_traits == 0`、`need.urgency == 0`，
    #      Drive 激活退化为只由 Perception(novelty > 0.75) 决定 = 绝大多数时候不开口。
    #      先闭合 Reflection→Memory 闭环(工程债台账 D-13)，再开此门；否则等于自设压制。
    #      2026-09-18 后段：D-13 口径 (a)（真实 producer）**已接上**，但那只补了"闭环有人调"，
    #      **第二层（数值 trait 通道）仍未闭** ⇒ 上面 `identity_traits==0` 的结论不变。
    #      因此"接了 producer" ≠ "可以开此门"；开此门前必须先把 traits 变成非空。
    drive_gate: bool = False
    # T1.1/D-13 验收 (a)：宪法 Reflection→Memory 的**真实 producer** 开关。
    # True=每次真实主动承载结束后，把那次承载结果经 reflect() 沉淀进宪法记忆
    #      (`anima/constitution/reflection_loop.py`)，并记审计
    #      `constitution_reflection_sedimented`。默认关（行为不变）。
    # 🔴 该闭环**不产生数值 trait**：reflect() 只产文本 lesson，而 derive_identity 只聚合
    #      semantic 的数值字段 ⇒ 开它之后 `identity_traits` 仍为 0。它补的是"闭环有生产
    #      producer"这一条，**不等于** drive_gate 可以翻转（后者还需一条数值 trait 生产路径）。
    constitution_reflection_loop: bool = False
    # 群最近一次人类发言距今 ≥ 该秒数，才考虑主动插话（避免对话正嗨时打断）。
    active_silence_min_sec: int = 300
    # 全局两次主动发言的最小间隔（秒），防止多群同时刷屏。
    active_global_cooldown_sec: int = 900
    # 仅在该群已积累足够人类消息后考虑主动开口，避免新群/低活跃群被打扰。
    active_min_group_messages: int = 6
    # 每群每天最多多少次非议程的主动发言；提醒/日历消息不受此项限制。
    active_daily_max_per_group: int = 5
    # 近期语境评分达到该阈值才允许调用 LLM 生成主动发言。
    active_context_score_threshold: float = 0.40
    # 主动意图连续出站失败多少次后标记 failed，避免「出站条件」死循环卡账本。
    active_intent_max_attempts: int = 5
    # 主动发言后多长时间内出现人类新消息，视为获得自然互动反馈。
    active_feedback_window_sec: int = 900
    # 样本足够后才按接受度自动降频，避免新群因一两次沉默被永久压制。
    active_feedback_min_samples: int = 4
    active_feedback_min_acceptance: float = 0.20

    # —— 睡眠模式（灵魂补丁 A1）——
    # 凌晨 01:00-05:00 进入「睡眠」：被 @ 时大概率装睡不回，少数情况迷糊回一句；
    # 心跳在该时段不主动发言，只生成梦境（见 cognitive/dream_engine.py）。
    sleep_enabled: bool = True
    sleep_start_hour: int = 1          # 含
    sleep_end_hour: int = 5            # 不含
    sleep_silent_prob: float = 0.8     # 被 @ 时不回复的概率（迷糊回复概率 = 1 - 此值）
    sleepy_max_tokens: int = 20        # 迷糊回复长度上限
    sleepy_temperature: float = 0.4     # 迷糊回复随机度（偏低，含糊）
    sleepy_llm_enabled: bool = True    # 用 LLM 生成迷糊回复；关闭则只用模板

    # —— quiet_hours 免打扰时段（通用，独立于 sleep 睡眠模式）——
    # 命中该时段时，非点名/非 @ 的被动消息一律静默跳过（不回复）；点名/@/私聊仍正常回。
    # 与 sleep 睡眠模式区别：sleep 处理「凌晨被 @ 时装睡」；quiet_hours 是用户自定义的
    # 通用免打扰窗口，只拦非点名被动回复，不影响主动发言与点名应答。
    quiet_hours_enabled: bool = False
    quiet_hours_start_hour: int = 23    # 含，跨午夜窗口
    quiet_hours_end_hour: int = 7       # 不含
    quiet_hours_allow_direct: bool = True  # 点名/@/私聊是否豁免（默认豁免=仍回）

    secret_auto_enabled: bool = True   # 心跳每日根据群观察自动生成秘密（B2）
    # 模板式主动发言开关（默认开启，可关掉避免突兀）。二者均只影响
    # 「主动问候/分享」这类与对话无关的定时插话，不涉及被动回复。
    morning_greeting_enabled: bool = True    # 晨间反思后 20% 概率向最近活跃群发早安
    morning_dream_share_enabled: bool = True  # 08:00 每天一次 10% 概率分享昨晚梦境


class DriftConfig(BaseModel):
    """人格漂移幅度上限（与 SelfLearning 对齐）。

    daily_budget 默认 1.5（≈30 条/天 ×0.05），避免反刍 ~15min 一轮时
    旧值 0.3 过早拒记；self_correction_daily_budget 为纠错独立池。
    window_ceiling 默认 4.0：约 2.5–3 日满额 general 预算的滚动上限
    （沿用旧比 0.80/0.30≈2.67，随 daily 上调；防慢性跑偏仍严于「日×7」）。
    """
    delta_max: float = 0.10
    daily_budget: float = 1.5
    self_correction_daily_budget: float = 0.5
    window_ceiling: float = 4.0
    similarity_floor: float = 0.75
    valence_min: float = -0.30
    valence_max: float = 0.70
    checkpoints_keep: int = 7


class PermissionConfig(BaseModel):
    owners: list[int] = Field(default_factory=list)
    admins: list[int] = Field(default_factory=list)
    readonly: list[int] = Field(default_factory=list)
    banned: list[int] = Field(default_factory=list)
    readonly_groups: list[int] = Field(default_factory=list)  # 只读群
    banned_groups: list[int] = Field(default_factory=list)      # 静默群
    group_policies: dict[str, dict] = Field(default_factory=dict)  # gid -> {autonomy, tools, ...}


class PersistenceConfig(BaseModel):
    data_dir: str = "data"
    use_sqlite: bool = True


class CompatConfig(BaseModel):
    astrbot_enabled: bool = False
    astrbot_tier: int = 1  # Tier 1（本期已定）


class CrossGroupConfig(BaseModel):
    """跨群记忆 + 群氛围差异化（G1 crossgroup_bot 集成）。"""
    enabled: bool = True                       # 总开关
    observe_all_messages: bool = True          # 对所有入站消息观察氛围/抽事实（前置到回复闸门前）
    min_fact_text_len: int = 6                 # 事实抽取的最小文本长度（过滤 "666"/emoji）
    extraction_model: str = ""                 # 事实抽取模型；空=复用 llm.model
    max_facts_per_user: int = 50               # 单用户事实上限（防止膨胀，FIFO 截断）

    @field_validator("extraction_model", mode="before")
    @classmethod
    def _coerce_none_extraction_model(cls, v):
        # 容忍 yaml 里 `extraction_model:`（无值→None）写成空串，避免校验失败
        return "" if v is None else v


class ToolLearningConfig(BaseModel):
    """工具调用自学习（P0）：记录 ReAct 工具轨迹、归纳使用心得。"""
    enabled: bool = True
    summarize_interval: int = 10      # 同工具成功记录每累计 N 条做一次规律归纳
    records_max: int = 200            # records 环形上限，防无界增长
    inject_top_k: int = 4             # 注入 system 的使用心得条数


class ReplyLearningConfig(BaseModel):
    """被动回复效果自学习（B1：只采集，不改行为）。

    记录「开口之后群里有没有人接」，归纳成经验事实注入 system。

    🔴 刻意**不接闸门**（`group_gate` 仍读静态 `familiarity`）—— 先跑一周拿真数字，
    再决定 `familiar_min` 要不要动态化。没有数字就调阈值 = 拍脑袋。

    `enabled=False` 只关**注入与归纳**（两个影响 system/花钱的点）；采集与统计是
    纯本地计数，始终运行，便于事后回看。
    """
    enabled: bool = True              # 注入 + 归纳总开关（采集恒开）
    window_sec: int = 300             # 开口后多久内有人接话算 engaged
    records_max: int = 200            # 每群心得环形上限
    inject_top_k: int = 3             # 注入 system 的心得条数
    min_samples: int = 5              # 少于这么多开口样本不归纳


class CapabilityConfig(BaseModel):
    """个人能力自生长系统（P1）：bot 从经历里长出可复用方法论。"""
    enabled: bool = True              # 总开关
    initial_confidence: float = 0.3   # 新建能力基线置信度（脱钩 LLM 自评）
    auto_register_confidence: float = 0.65  # 仅作参考阈值（实际注册走 value_score 晋升）
    dynamic_daily_quota: int = 8      # 每日新注册能力工具上限（曾因自动能力占满3导致命名候选排队）
    max_total: int = 40               # 能力库硬上限
    unused_drop_days: int = 30        # 未使用超此天数淘汰
    unused_decay_days: int = 14       # 未使用超此天数降权
    dedup_text_threshold: float = 0.5 # 语义去重文本相似度兜底阈值（0.5：表面相近的中文物也走兜底合并；真·语义重复由 LLM 去重把关）
    max_zero_usage: int = 12         # 零调用能力软上限：超出则按价值分淘汰最低价值者，止住冗余堆积
    min_create_text_len: int = 6      # 能力名+描述+用法总内容最小长度（防碎碎念）


class CapabilityLayerConfig(BaseModel):
    """Capability Layer routing controls. Not related to self-growing capability library.

    RFC-0001 Capability Layer 的**治理 / 路由**开关（M0.4.7 D-A1）。与上方
    ``CapabilityConfig``（``capabilities:`` —— 个人能力自生长系统 P1）**无任何关系**，
    禁止互相复用：前者管「LLM 经 capability id 路由到 Provider」的准入面，
    后者管「bot 从经历里长出方法论」的知识库。

    Configuration changes require controlled restart until runtime synchronization
    is separately authorized.（R4：``ConfigService._sync_runtime`` 不热更 registry，
    kill switch 语义为受控重启生效。）
    """
    # 总开关：capability 路由未激活时，call_capability 一律拒绝、capability schema 不产出。
    capability_mode: bool = False
    # Empty allowlist denies all capability routing.
    # fail-closed（R2）：空 allowlist = 拒绝全部治理能力路由，**不是**放行全部。
    # 条目为调用主体标识，形如 "user:123456"；刻意不支持任何通配符（通配符即后门）。
    pilot_capability_allowlist: list[str] = Field(default_factory=list)

    # --- F9.2 Operational Activation：观测面开关（全部默认 OFF）---------------
    # 这些开关**只控制"是否记录"**，不参与任何 provider 选择 / 策略判定 /
    # lifecycle 迁移——观测永不改变执行（S5 Activation Proof）。
    #
    # capability_gateway_mode 此前只在构造参数里存在、未声明为 config 字段，
    # 导致 pydantic（extra=ignore）静默丢弃 config.yaml 的写入（F9.1 G-01）。
    # 这里补齐声明，使"配置写了就生效"成立。
    capability_gateway_mode: bool = False
    # 三条正交证据链各自独立开关（互不连带）：
    #   resolution → PersistentAuditSink（为什么选这个 provider）
    governance_audit_enabled: bool = False
    #   health     → HealthEvidenceStore（执行之后发生了什么）
    health_evidence_enabled: bool = False
    #   recovery   → TransitionStore（lifecycle 迁移记录；本阶段仅接线不驱动）
    transition_audit_enabled: bool = False
    # 治理证据落盘目录（相对 persistence.data_dir；绝对路径按原样使用）。
    governance_data_dir: str = "governance"

    # --- R5 PHASE 13：python.exec 受治理激活主开关（**代码默认** OFF / fail-closed）----
    # 仅当本开关为 True **且** capability_gateway_mode 为 True 时，python_exec
    # provider 才会被注册进 CapabilityRegistry。
    # ⚠️ 2026-09-18 实测订正：旧注释称「生产两者皆 False → python.exec 完全不可达」，
    #    **与生产实配不符**。生产 config.yaml 实际已置
    #    capability_mode/capability_gateway_mode/governance_audit_enabled/python_exec_enabled
    #    四项为 true（owner 授权，代码留痕见 anima/tools/__init__.py:193「R5 PHASE 13
    #    re-open (owner-authorized 2026-08-15)」）⇒ provider **已注册且 owner 可达**。
    #    实测使用量为 0（resolution 事件全为 search.web；审计/日志 0 命中）。
    #    注意：空 pilot_capability_allowlist **挡不住 python.exec**（见 tools/__init__.py:194）。
    #    真实门 = gateway 四道 fail-closed 门 + provider 的 owner-only 边界。
    #    详见 reports/2026-09-18_phase_a_honesty_pass.md A2 节。
    python_exec_enabled: bool = False
    # 激活参数（仅在 python_exec_enabled=True 时生效）。
    python_exec_permission_level: int = 2
    python_exec_require_audit: bool = True


class NotesCompactConfig(BaseModel):
    """自我笔记压缩（兑现 learning.py docstring 承诺，止住 self_notes.md 无限膨胀）。"""
    enabled: bool = True             # 总开关
    notes_max_keep: int = 300        # self_notes.md 保留最近 N 行，超出部分交给 LLM 摘要归档
    compact_every_ruminations: int = 6  # 每 N 次反刍触发一次压缩（避免频繁调用 LLM）
    compact_model: str = ""          # 压缩所用模型；空=复用 llm.model


class RuminationConfig(BaseModel):
    """反刍取样范围（P0-B1：跨用户隐私串扰收口）。

    - scope="group_only"（默认）：只从群聊会话桶取样——群内本就是共享语境，
      bot 作为群成员的共同经历可反思；私聊是一对一信任语境，不卷入跨上下文。
    - scope="all"：旧行为（全库所有桶），仅供 owner 显式选择。
    - max_sessions：最多取样最近活跃的 N 个会话桶，防止热门群独占 1500 字预算。
    """
    scope: str = "group_only"        # group_only | all
    max_sessions: int = 6


class StickerConfig(BaseModel):
    """表情包自收集系统：从群消息自动捡图存库，回复时由 LLM 选图附带。"""
    enabled: bool = True             # 总开关
    auto_collect: bool = True         # 从群消息 image 段自动收集
    collect_mode: str = "cooldown"    # cooldown | probability
    collect_probability: float = 0.30 # probability 模式单图批次收集概率
    collect_cooldown_sec: float = 30.0  # cooldown 模式同群最小收集间隔
    collect_min_interval: float = 3.0  # 同发送者收集最小间隔（秒），防刷屏
    max_stickers: int = 500          # 表情库硬上限（环形淘汰最旧/最少用）
    label_throttle_per_user: int = 600  # 每发送者打标最小间隔（秒）
    label_model: str = ""            # 打标模型；空=复用 llm.model
    label_by_vision: bool = True     # 表情打标改用真·看图（视觉），否则退化为文本推断
    require_owner_review: bool = True   # 新收集图片先入候选区，批准后才允许发送
    pending_capacity: int = 200       # 待审核池满时暂停自动收集，防止未审内容无限堆积
    default_scope: str = "local"      # local=仅来源群，public=可跨群（仍受全局开关约束）
    collect_allowlist: list[str] = [] # 为空=全部群；group:<id> / user:<id>
    collect_blocklist: list[str] = []
    send_allowlist: list[str] = []
    send_blocklist: list[str] = []
    share_across_groups: bool = False   # 默认仅在来源群复用，跨群需显式开启
    # 跨群白名单（fail-closed）：非空时，即便 share_across_groups=True，也只有列出的群
    # 能用别群的库存；未列出的群（含以后新加入的群）一律不放行。留空 = 沿用全局开关语义。
    cross_group_allowlist: list[str] = []

    @field_validator("cross_group_allowlist", mode="before")
    @classmethod
    def _coerce_cross_group_allowlist(cls, v):
        """YAML 里 `[727631526]` 会被解析成 int 列表，群号一律按字符串归一。

        不加这层会把一个未加引号的数字变成 ValidationError，而配置加载失败
        会让整个服务起不来（2026-09-17 实际发生过一次）。
        """
        if v is None:
            return []
        if isinstance(v, (str, int, float)):
            v = [v]
        return [str(x).strip() for x in v if str(x).strip()]
    always_atmosphere: list[str] = ["humorous", "casual"]  # 允许带图的群氛围
    recent_window: int = 4          # 同一群最近 N 张不重复（候选不足时允许复用）
    gif_min_interval_sec: int = 1800  # 同一群动图最小间隔，避免刷屏
    classify_at_collect: bool = True   # LLM 视觉闸门：导入时判定是否为表情包并分类（否=拒收）
    llm_select: bool = True            # LLM 视觉终选：发送时从候选里挑最贴的（否=退回本地标签匹配/随机）
    select_model: str = ""             # 选图/分类视觉模型；空=复用 label_model
    # --- 对标 astrbot_plugin_stealer 的能力增强 ---
    phash_dedup: bool = True           # 收集时 pHash 感知哈希近似去重（乱序截帧/改色/加字视为同一张）
    phash_threshold: int = 20          # pHash 汉明阈值（越低越严格）
    smart_select: bool = True          # 选图走 BM25+混合相似度+嵌入语义 加权随机（否=旧本地/LLM 视觉选图）
    # 智能选图各信号权重（对标 stealer 可配置）：desc/tag/scene/bm25/emb/favorite 相加驱动打分；
    # recency=近期重复惩罚、negation=否定词命中惩罚因子、top_k=高分候选加权随机的池子大小。
    select_weights: dict = {"desc": 0.35, "tag": 0.25, "scene": 0.20, "bm25": 0.2, "emb": 0.20, "favorite": 0.3, "recency": 0.1, "negation": 0.5, "top_k": 3}
    # --- 表情包自审查 / 自过审：AI 判定明确的表情自动入库可发，只留拿不准的给 Owner ---
    self_review: bool = True            # AI 自审自过审：收集/打标后判定明确即自动 approved，无需 Owner 逐张批
    natural_forget: bool = True         # 自然遗忘：旧且少用的表情自动腾位（即使未到硬上限）
    forget_age_days: float = 60.0       # 超过该天数的旧表情才纳入自然遗忘候选
    forget_used_max: int = 2            # 历史使用次数 <= 此值才算"少用/远"
    forget_last_used_days: float = 30.0 # 距上次使用超过该天才算"已凉"（last_used 为 0 视为从未用过）
    # --- 对话中被动自动附带（照 astrbot_plugin_stealer 概率/冷却模式精神）---
    send_probability: float = 0.1     # LLM 未主动选图时，被动附带表情的概率（0=关，1=必带）
    send_by_emotion: bool = True      # 被动附带优先按回复情绪匹配；否则随机
    send_cooldown_sec: float = 20.0   # 同群被动带图最小间隔（秒），防连续刷屏
    # --- 意图门控（对标 stealer）：严肃/服务类氛围下不被动附表情，避免嬉皮笑脸 ---
    small_emoji_channel: bool = True   # 表情包出站带 image subType=1(NapCat 表情包小图渠道)；False=普通图片显示大图
    intent_gate: bool = True          # 开启后，命中 intent_gate_serious_atmospheres 时跳过被动带图
    intent_gate_serious_atmospheres: list[str] = ["formal", "help", "support"]  # 严肃/服务类氛围
    # 注：被动自动附带不再受 always_atmosphere 门控（stealer 为全局概率模式），
    # 仅在群聊内触发，scope 受 share_across_groups 控制。


class BilibiliReadConfig(BaseModel):
    """B 站视频理解：发视频链接/BV号/b23.tv 短链，抓字幕让 bot 真正“看懂”视频。

    参考 astrbot_plugin_biliread：字幕接口需登录态（SESSDATA），无 cookie 时多数
    视频拿不到字幕，会如实提示。总结复用 Anima 的 LLMClient，不另起 provider。
    """
    enabled: bool = True             # 总开关
    sessdata: str = ""               # B 站 Cookie SESSDATA（字幕接口需登录态）
    bili_jct: str = ""               # B 站 Cookie bili_jct
    summary_model: str = ""          # 字幕总结模型；空=复用 llm.model
    max_subtitle_length: int = 4000  # 字幕截断上限，防上下文溢出


class SelfCodeConfig(BaseModel):
    """自我编程：用 agentic coder（Claude Code CLI）把自然语言目标变成真实可运行能力。

    参考 astrbot_plugin_claudecode：把 Claude Code CLI 当 LLM 函数工具，给其 Bash/Edit/Read
    工具与隔离 workspace，让它写真实 Python 模块；通过后只形成待审批工程提案。

    安全模型：
    - 只把 Claude 写入隔离 workspace 的文件纳入候选，绝不碰核心代码
    - 安装前必须在沙箱子进程自测通过（pytest/import + run() 冒烟）
    - 候选的作用、风险、文件与测试证据必须先展示在 WebUI
    - 只有 Owner 审批后才允许安装；安装前对运行时目录做备份并写审计
    """
    enabled: bool = False            # 总开关（高危，需 owner 显式开启）
    claude_executable: str = "claude"   # Claude Code CLI 路径/命令
    anthropic_api_key: str = ""      # 留空则读环境变量 ANTHROPIC_API_KEY
    api_base_url: str = ""           # 可选自定义端点（如逆向网关）
    workspace_dir: str = ""          # 隔离工作区；空= data_dir/selfcode_workspace
    selfcode_dir: str = ""           # 安装落盘目录；空= data_dir/selfcode（不修改源码包）
    max_turns: int = 12              # Claude 最大 agentic 轮数
    timeout_seconds: int = 180       # Claude 单次调用超时
    permission_mode: str = "acceptEdits"  # default/acceptEdits/plan/dontAsk
    auto_install: bool = False       # 兼容旧配置；运行时始终要求 Owner 审批
    daily_quota: int = 0             # 0 = 不限制候选生成次数（不影响超时/并发/审批）
    test_timeout: int = 30           # 沙箱自测子进程超时
    model: str = ""                # CC 使用的模型；空=复用 llm.model（与 bot 同款）
    max_review_rounds: int = 2     # 双 agent 自纠错轮数（Coder↔Critic）
    critic_enabled: bool = True    # 是否启用 Critic 只读审查（自纠错关键环节）
    coder_backend: str = "auto"     # auto=claude 可用则用之否则原生 | claude=强制 CLI | native=直接用 bot 的 LLMClient
    coder_retries: int = 4          # 原生 coder/critic 单次产出失败(LLM 异常或没给出代码块)时的最大尝试次数；重试退避让它"慢慢跑"
    coder_retry_delay: float = 5.0  # 原生 coder/critic 重试基础退避秒数(线性递增=attempt*delay)


class VisionConfig(BaseModel):
    """全量多模态视觉理解：让 bot 真·看见图片（描述/打标/接话）。

    设计原则（用户定调）：体验优先于 token 成本——默认全开，不为省钱降级。
    隐私：入站图仅下载到本机内存/内联，绝不以原文件落盘（sticker 收集除外，
    那是用户明确要的素材库）；描述文本进记忆，图片本身不进记忆。
    """
    enabled: bool = True             # 总开关（看图片能力）
    model: str = ""                  # 视觉理解模型；空=复用 llm.model
    max_inline_bytes: int = 2 * 1024 * 1024  # 单图转码后上限；再压一档以减少 413 丢视觉
    max_source_bytes: int = 32 * 1024 * 1024  # QQ 原图下载上限，防止异常文件耗尽内存
    max_request_image_bytes: int = 3 * 1024 * 1024  # 单次聊天全部视觉帧总预算（base64 后更安全）
    max_dimension: int = 1280        # 视觉输入最长边；再压以控制网关载荷
    max_animation_frames: int = 3    # GIF/WebP 动图保留的关键帧数
    describe_throttle_per_user: int = 600    # 后台描述任务每发送者最小间隔（秒）
    describe_on_inbound: bool = False  # 入站即描述：带图消息一进来就后台描述（不再等「本回合真的回复了」）
    comprehend_in_chat: bool = True  # 用户在聊天里发图时，bot 当场看图并接话
    store_desc_to_memory: bool = True  # 把图片一句描述存进对话记忆，供后续轮次忆起


class ImageGenerationConfig(BaseModel):
    """图像生成；默认关闭以避免意外成本。daily_limit=0 表示不限制每日张数。"""
    enabled: bool = False
    model: str = "gemini-3.1-flash-image"
    # 图像模型 fallback 容灾：主模型(model)遇上游 429/500 等可重试状态时，依次尝试
    # 这些模型。支持两种端点格式：
    #   - chat 端点（gemini-* 类，走 /chat/completions，message.images[].image_url）
    #   - images 端点（含 imagine 类，走 /images/generations，data[].b64_json）
    # 网关下可用的图像模型示例：gemini-3.1-flash-image / grok-imagine-image /
    #   grok-imagine-image-quality / grok-imagine-image-2.0
    fallback_models: list[str] = []
    daily_limit: int = 0  # 0 = 不限每日张数
    max_output_bytes: int = 16 * 1024 * 1024  # 0 = 不限制单图体积（建议保留上限防 OOM）


class AudioConfig(BaseModel):
    """语音转写：只在用户发送 record 时调用，受大小和节流约束。"""
    transcription_enabled: bool = True
    model: str = ""
    max_bytes: int = 10 * 1024 * 1024
    throttle_per_user: int = 120


class SylannConfig(BaseModel):
    """SylannEngine 适配开关与引擎计算配置。"""
    enabled: bool = False
    mode: str = "disabled"  # disabled | shadow | active
    data_dir: str = "sylann"
    weight: float = 0.15
    engine_mode: str = "lite"  # Sylanne-Core: lite | pro | max
    assessor_enabled: bool = True
    pel_core_enabled: bool = True
    tick_sessions_limit: int = 32


class SkillsConfig(BaseModel):
    """声明式技能包。初期只允许 prompt 型技能，禁止任意代码执行。"""
    enabled: bool = True
    dir: str = "skills"
    allow_prompt: bool = True
    max_loaded: int = 32


class ConversationConfig(BaseModel):
    """连续私聊输入的语义合并窗口。"""
    enabled: bool = True
    wait_sec: float = 1.5
    max_wait_sec: float = 8.0
    # §17 安全版反馈自然回话：识别到工程反馈时，仅私聊+已有主回复文本才在回复末尾
    # 追加一句轻量确认（不双发/不改主回复）。默认关；置 true 即启用。
    natural_feedback_ack: bool = False


class ReplySplitConfig(BaseModel):
    """拟人分段发送 + 拟真延迟（参考 astrbot_plugin_splitter）。"""
    enabled: bool = True
    max_segments: int = 2
    min_segment_len: int = 40
    max_segment_len: int = 90
    trigger_threshold: int = 150      # 低于此字数不分段（避免短回复被切成多条）
    tail_merge_threshold: int = 15    # 尾段低于此字数合并到前段
    delay_enabled: bool = True
    delay_base: float = 0.4           # 基础延迟（秒）
    delay_factor: float = 0.02        # 每字延迟（秒/字）
    delay_jitter: float = 0.3         # 随机抖动上限（秒）
    context_aware_interrupt: bool = True  # 分段发送中若用户插话调侃，停止后续分段并自嘲收尾


class DebounceConfig(BaseModel):
    """群聊消息防抖（参考 astrbot_plugin_continuous_message）。"""
    group_enabled: bool = True
    group_wait_sec: float = 1.0       # 2026-10-03：2.0→1.0（实测回合数仅 +4%，却省 1s/条）
    group_max_wait_sec: float = 6.0
    adaptive: bool = True
    short_msg_threshold: int = 10     # 短句阈值（字）


class SoulConfig(BaseModel):
    """灵魂能量槽系统（参考 astrbot_plugin_angel_memory）。"""
    enabled: bool = True
    ema_alpha: float = 0.3            # EMA 平滑系数
    persist_interval_sec: float = 60.0


class GateConfig(BaseModel):
    """轻重双模型前置判断（参考 astrbot_plugin_angel_heart）。"""
    enabled: bool = False             # 默认关闭，需手动启用
    model: str = ""                   # 空则继承 llm.model
    timeout_ms: int = 500
    fallback_to_full: bool = True     # 超时/失败时走完整流程


class QZoneConfig(BaseModel):
    """QQ 空间说说（参考 astrbot_plugin_qzone_tools）。"""
    enabled: bool = False             # 默认关闭
    daily_max: int = 2                # 每天最多发几条
    cooldown_sec: int = 14400         # 冷却 4 小时


class QQStatusConfig(BaseModel):
    """QQ 在线状态同步（参考 astrbot_plugin_qzone_tools）。"""
    enabled: bool = False             # 默认关闭


class VoiceConfig(BaseModel):
    """AI 语音消息 TTS（参考 astrbot_plugin_qzone_tools）。"""
    enabled: bool = False
    auto_probability: float = 0.1     # 特殊场景自动触发概率


class MemeConfig(BaseModel):
    """趣味图片互动（参考 astrbot_plugin_pic_toolbox）。"""
    enabled: bool = False
    petpet_on_poke: float = 0.3       # 被戳时摸头回去概率


class ScraplingConfig(BaseModel):
    """Scrapling 反反爬网页抓取。"""
    enabled: bool = False             # 默认关闭，需手动启用


class TavilyConfig(BaseModel):
    """Tavily 联网搜索（高相关、带时效字段）。

    作为 ``search.web`` 的首选后端，显著优于免费抓取（SearXNG/Bing）。
    api_key 为 SecretStr，优先读环境变量 ``ANIMA_TAVILY_API_KEY``，不落盘明文。
    search_depth: basic=快速 / advanced=深度检索（更准、稍慢）。
    max_results: 单次返回上限（默认 5）。
    include_answer: 让 Tavily 直接给出摘要答案（可提升事实类回答的即时准确度）。
    """
    enabled: bool = True
    api_key: SecretStr = SecretStr("")
    search_depth: str = "advanced"    # basic | advanced
    max_results: int = Field(default=5, ge=1, le=10)
    include_answer: bool = True


class ProactiveConfig(BaseModel):
    """P1-4 主动优先级队列（单一事实来源见 ``core/proactive.py``）。

    ``enabled=false`` → 关闭新增的主动来源（订阅异常提醒），已入队议程照常消费。
    优先级数值本身仍由 ``core.proactive.SOURCE_PRIORITY`` 决定，可用
    ``source_priority`` 按来源名覆盖（如 ``{"heartbeat": 0.1}`` 让闲聊更靠后）。
    """
    enabled: bool = True
    # 订阅连续失败多少次才提醒（避免偶发网络抖动就打扰）
    subscription_failure_threshold: int = 3
    # 同一订阅的异常提醒冷却（秒），默认 6 小时
    subscription_anomaly_cooldown_sec: int = 21600
    # >= 该优先级视为"要事"（日志/审计分流用，不绕过冷却）
    urgent_threshold: float = 0.7
    # 按来源覆盖优先级（可选）
    source_priority: dict[str, float] = Field(default_factory=dict)


class TaskAgentConfig(BaseModel):
    """P2-1 多步任务执行策略（``anima/agent/executor.py``）。

    - ``verify_steps``：步骤执行成功后按 ``Step.verify`` 判据做独立验收。
      验收需要 LLM；无 LLM 时自动跳过（判据判不了就不要假装判过）。
    - ``max_replans``：失败重规划次数上限（0 = 关闭重规划）。
    - ``parallel_steps``：允许 planner 标记的 ``parallel_group`` 内部并发执行。
      只并发「显式指定工具」的步骤，ReAct 步骤永远串行。
    """
    verify_steps: bool = True
    max_replans: int = 2
    parallel_steps: bool = True
    max_parallel: int = 4


class OwnerBrowserConfig(BaseModel):
    """P1-3 Owner 受控浏览器（复用 ``agent_tools.agent_browser``）。

    三道门，各自独立、可分别回滚：
    - ``enabled=false``：工具根本不注册（私聊也不给）。
    - ``private_only=true``：群聊既不可见也不可调用（owner 在群里同样拿到 level 4，
      仅靠 ``min_level`` 挡不住「群里操控环境」）。
    - ``allowed_actions``：动作白名单，默认只给 navigate/extract（只读取页面），
      click 这类「改变页面状态」的动作需 Owner 显式加。
    """
    enabled: bool = True
    private_only: bool = True
    allowed_actions: str = "navigate,extract"


class MemoryCompressionConfig(BaseModel):
    """P1-2 记忆压缩（DESIGN_MEMORY_COMPRESSION.md）。

    ``enabled=false`` → 夜间不跑压缩，行为退化为只有 ForgettingEngine 时间衰减。
    注意这只是「不再整理」，已写入的 confidence/disputed 字段仍会被读取侧尊重。
    """
    enabled: bool = True
    # 相似度阈值：高于此值的事实才合并（默认字符级 difflib 比）
    sim_threshold: float = 0.85
    # 每人事实数上限，超出按 (confidence, timestamp) 升序淘汰
    max_facts_per_user: int = 30
    # 每日衰减系数：0.99 ≈ 70 天衰减到 0.5
    daily_decay: float = 0.99
    # 本地时区低峰窗口小时；<0 表示不限小时。
    # 偏差：设计文档写 3，但实现挂在 autonomy 的「日切块」（凌晨首次心跳，天然低峰），
    # 若同时限定 tm_hour==3，日切几乎不可能正好落在 3 点 → 压缩永不触发。故默认 -1，
    # 由 interval_sec(24h) 保证一天最多一次。需要严格 3 点窗口时可改回 3。
    run_hour: int = -1
    # 低于此事实数不调 LLM 摘要（成本控制；40 意味着绝大多数用户永不触发）
    min_facts_to_llm: int = 40
    # 两次运行最小间隔（秒）
    interval_sec: int = 86400


class DebtTrackingConfig(BaseModel):
    """P1-1 承诺追踪（DESIGN_COMMITMENT_TRACKING.md；2026-09-18 前名为 DESIGN_DEBT_TRACKING.md）。

    ``enabled=false`` → 不记账、不提醒、不注入，完全退化为无债状态（一键回滚）。
    ``keyword_only_mode`` 默认 true：只走关键词漏斗，零 LLM 成本、零额外延迟。
    """
    enabled: bool = True
    keyword_only_mode: bool = True
    reminded_max: int = 3
    remind_interval_sec: int = 86400
    grace_hours: float = 48.0
    inject_to_cognition: bool = True


class SkillRouteHintConfig(BaseModel):
    """给指定 Skill 一张「能争取到注意力」的卡（2026-10-10，**默认关**）。

    ## 为什么需要它
    `skill_web_brief` 在生产**近 40 天 0 次调用**，而同期联网检索 177+ 次。
    根因是**结构性位置**，不是配置/注册问题：生产 `capability_mode=true` ⇒
    `ToolRegistry.get_schemas()` 返回 `capability_schemas() + legacy_only_schemas()`，
    **capability 卡恒前、legacy 卡恒后**；而 `sort_by_priority`（Memory-First 排序）
    **只在非 capability 分支生效** ⇒ 没有 capability id 的 Skill **抬不动位置**
    （实测：群聊 29/29、owner 48/48，竞品 `search_web` 第 1 位）。

    ⇒ 只能走 system 提示这条既有通道（与 `AnswerLengthBudgetConfig` 同机制）。

    ## 为什么默认关
    它会把「联网检索」的默认路径从裸 `search_web` 改成 `skill_web_brief`（多一次
    整理 LLM 调用）⇒ 属于**行为变更**。默认 false 与从前逐字相同。

    ⚠️ 必须做成 `ReactConfig` 的**显式字段**：pydantic 在本项目是 `extra=ignore`
    ⇒ 只写 config.yaml 不加字段会被**静默丢弃**（F9.1 G-01 同类，本文件 :340 有记载）。
    """
    #: 总开关。**false = 与从前逐字相同**（零行为变更）。
    enabled: bool = False
    #: 要导流到的技能工具名。**该工具不在工具面时一律不注入**（fail-closed，
    #: 绝不向 LLM 提一个它调不到的工具名）。
    skill: str = "skill_web_brief"
    #: 提示文本；``{skill}`` 会被替换成上面的工具名。留空则用代码内默认文案。
    text: str = ""


class ReactConfig(BaseModel):
    """P0-1 Memory-First（DESIGN_REACT_MEMORY_FIRST.md）总开关。

    true  -> 成员/事实类问题注入「先查内部记忆，再考虑搜网」引导，
             且 get_schemas() 按 Tool.priority 升序（记忆类 1 先于 web_search 3）。
    false -> 完全退化为改动前行为：只注入实时引导，schema 保持注册顺序。
             这是一键回滚点（设计文档 §4.4）。

    ``freshness_footer``（P2-3）：用过联网/时效类工具、但最终回答里完全没有日期
    痕迹时，追加一句时效声明（只披露，不改写答案）。true 为默认；关掉即回到
    「答完就发」的旧行为。

    ``upstream_patience_sec``（2026-09-23）：上游**根本没给出任何东西**（200-空 /
    传输故障 / 熔断）时，出口前最多等这么久再要一次正文，仍空才出罐头话术。
    只在这条出站本来就要变成罐头话术时付出延迟，且只等一次。**0 = 关闭**（一键
    回滚到「立刻出罐头话术」）。熔断剩余超过该预算时**不等待**（等不到头）。

    默认值 20.0（2026-09-25 由 6.0 抬上来）：6.0 被 48h 实测否掉 —— 4/4 次样本
    等满 6s 后重试仍空，而静默实际持续 8–20s。低于静默下限的预算结构上不可能成功。
    ⚠️ 2026-10-03：出站桶已**连续两次整链失败**时**不再等待**（窗口性故障，白等）——
    见 `react._patience_wait`。本键只影响「单次失败」那一路。

    ``tool_timeout_sec``（2026-10-03）：单次工具调用的墙钟上限。旧值是硬编码 120s，
    放过了 102s 的 `x_search`（整回合被拖死、同群用户排队 2 分钟）。默认收紧到 45s
    （覆盖 browser `goto` 30s + `click` 15s 的内部串行最坏）。

    ``turn_deadline_sec``（2026-10-03）：**整回合** ReAct 墙钟预算。单次工具上限
    只挡「**一个**工具卡死」，挡不住「**多轮**工具 + 多次 LLM 往返」的累计 —— 实测
    `x_search` 回合 102s，期间同群串行锁被持住、其它用户排队白等。超预算即
    「带**已得工具结果**收口作答」。**默认 0 = 关闭**（不改变既有行为）；生产
    config 无该键走代码默认，要启用请显式配置（如 90）。
    """
    memory_first: bool = True
    freshness_footer: bool = True
    upstream_patience_sec: float = Field(default=20.0, ge=0.0, le=30.0)
    tool_timeout_sec: float = Field(default=45.0, ge=1.0, le=600.0)
    #: 🔴 per-tool 超时覆盖（2026-10-15）。**默认空 dict = 零行为变更**。
    #:
    #: 为什么需要它：`tool_timeout_sec` 是**全局**上限，但工具耗时差异极大 ——
    #: `x_search` 实测 45–102s（浏览器冷启动 + `goto` + 渲染 + 下载配图 + 视觉过滤），
    #: 而普通工具是亚秒级。把全局值抬高会让**所有**工具一起放松（10-02 那次
    #: 「同群排队 2 分钟零回复」事故正是这么来的）；压到 45s 又使 x_search
    #: 结构性超时（7 天唯一一次实证 `工具调用超时(45s) name=x_search`）。
    #: ⇒ 正确解法是**只给长任务开小灶**，其余工具仍受全局值约束。
    #:
    #: 取值建议：`{"x_search": 75}` —— 覆盖「冷启动+goto30s+渲染+下载」的常规路径，
    #: 同时把 100s 级极端（`react.py:156` 记载的 102s）留在外面。
    #: 必须与 `turn_deadline_sec` 配合：per-tool 放长后，整回合硬上界才有意义。
    tool_timeout_overrides: dict[str, float] = Field(default_factory=dict)
    turn_deadline_sec: float = Field(default=0.0, ge=0.0, le=600.0)
    skill_route_hint: SkillRouteHintConfig = Field(default_factory=SkillRouteHintConfig)


class AnswerLengthBudgetConfig(BaseModel):
    """求答轮次「说多少」（2026-10-08，**默认关**）。

    🔴 为什么需要它：`max_tokens` 在本项目上游（CPA 代理）**被完全忽略**
    （实测 16 → 6421 字、`max_completion_tokens` 同样无效），而 system 里的字数指令
    实测精准生效（要求 ≤150 字 → 117 字；**生产人格 2925 字作 system 下同样成立**）
    ⇒ 这是目前唯一可用的长度旋钮，也是「别让它写太长再被截掉」的治本手段。

    ⚠️ 必须做成 ContextConfig 的**显式字段**：pydantic 在本项目是 `extra=ignore`
    ⇒ 只写 config.yaml 不加字段会被**静默丢弃**（F9.1 G-01 同类，本文件 :340 有记载）。
    """
    #: 总开关。**false = 与从前逐字相同**（零行为变更）。
    enabled: bool = False
    #: 求答回答的字数预算。实测 150 会丢细节（断片/梦游/撤药触电感），故取 200。
    budget_chars: int = Field(default=200, ge=0, le=2000)
    #: 用户自己说要详细时不注入。🔴 实测：指令会压过用户显式请求
    #: （人格 + ≤150 字时，用户说「请详细讲讲」模型仍只写 121 字）。
    exclude_when_user_wants_detail: bool = True


class ContextConfig(BaseModel):
    """聊天上下文预算。

    预算由本地估算器在请求前执行，优先保留人格、身份护栏与当前用户输入；
    可选认知片段和最旧历史会先被裁掉。不同网关的真实 tokenizer 不同，因此
    这里是保守的防溢出阈值，而不是计费用量。
    """
    input_token_budget: int = Field(default=12000, ge=1024)
    system_token_budget: int = Field(default=8000, ge=1024)
    image_token_estimate: int = Field(default=768, ge=64)
    tool_result_token_budget: int = Field(default=1800, ge=128)
    answer_length_budget: AnswerLengthBudgetConfig = Field(
        default_factory=AnswerLengthBudgetConfig
    )


class ExplorationConfig(BaseModel):
    """自主公开信息探索；只读、限频、来源化，不绕过 Agenda 主动发声。"""
    enabled: bool = True
    interval_sec: int = Field(default=3600, ge=300)
    daily_limit: int = Field(default=3, ge=0, le=24)
    min_desire_intensity: float = Field(default=0.4, ge=0.1, le=1.0)
    sources: list[str] = Field(default_factory=lambda: [
        "bilibili_popular", "bbc_zhongwen", "bilibili_search", "wikipedia_zh",
    ])
    novelty_window_sec: int = Field(default=7 * 86400, ge=3600)
    max_notes: int = Field(default=200, ge=20, le=2000)
    share_enabled: bool = False
    daily_share_limit: int = Field(default=1, ge=0, le=5)
    share_min_score: float = Field(default=0.75, ge=0.0, le=1.0)
    share_groups: list[int] = Field(default_factory=list)
    # X 时间线主动探索（需 x_browser 启用）；单次取回条数上限
    x_timeline_limit: int = Field(default=8, ge=1, le=20)
    # 出站 HTTP 代理（可选）：VPS 数据中心 IP 会被维基等公开源 403 封禁，
    # 配置本地代理（如 http://127.0.0.1:10809）可绕过。留空则直连（保持原行为）。
    proxy: str = ""


class XBrowserConfig(BaseModel):
    """X（Twitter）受管浏览器会话：虚拟浏览器登录账号、刷时间线/搜索，
    作为自主探索内容来源，并对外提供 x_search 工具。"""
    enabled: bool = False
    # 专用家宽代理出口（mihomo 独立监听端口，如 http://127.0.0.1:10810），
    # 与维基/BBC 通用代理隔离，降低 X 风控。留空则走系统默认出口。
    proxy: str = ""
    # 浏览器引擎：chromium（patchright stealth，默认）或 firefox（gecko 指纹，
    # 对 X 风控更友好——X 认真 Firefox 但挡 chromium 自动化）。切换需对应 playwright 二进制已安装。
    browser_engine: str = Field(default="chromium", pattern="^(chromium|firefox)$")
    # 账号凭据（用于会话失效时自动重登，保号自愈）。生产 config 中配置。
    username: str = ""
    password: str = ""
    data_dir: str = "data/x_browser"
    # 每日限流（保护账号/家宽流量）
    search_daily_limit: int = Field(default=10, ge=0, le=100)
    timeline_daily_limit: int = Field(default=10, ge=0, le=100)
    # 养号期（前 N 天低频，不主动搜/滚，仅被动）；0 表示已过养号期。
    warmup_days: int = Field(default=14, ge=0, le=60)
    # 探索关键词（主动探索用）：留空则只滚时间线/关注流。
    explore_keywords: list[str] = Field(default_factory=list)


class ContradictionConfig(BaseModel):
    """矛盾检测（旧 Anima 模块五回迁）：检测用户陈述与既有记忆/自我独白的事实冲突。

    设计要点：
    - 仅在向量检索命中『强相关旧记忆』（similarity ≥ threshold）时才触发 LLM 判断，
      天然把调用量限制在『确有历史可对照』的消息上。
    - 语义去重（与最近 dedup_recent 条比对，相似则跳过）+ 上限 max 裁剪，止住无限膨胀。
    - 默认关闭（高危认知功能，需 owner 显式开启）。
    """
    enabled: bool = False
    max: int = 50                                   # 矛盾历史上限（环形保留最近 N 条）
    dedup_recent: int = 10                          # 语义去重比对窗口
    recall_threshold: float = 0.80                  # 触发 LLM 判断所需的旧记忆相似度下限
    model: str = ""                                 # 判断模型；空=复用 llm.model


class DangerConfig(BaseModel):
    """高危自主行为层（旧 Anima danger_* 回迁）。

    全部默认关闭，需 owner 在 config 显式开启。其中会改写长期文件或主动发言的
    行为（core_mutation / memory_infection / stance_propagation）额外要求
    _confirm=true 二次开关；core_mutation 还会经 security.require_approval_for_danger 护栏。
    """
    enabled: bool = False                           # danger 层总开关

    active_info_collection: bool = True             # 主动信息收集：生成自然提问存入欲望（L4 释放「会想」）
    relationship_inference: bool = True             # 关系图谱推断：从对话抽取人际关系笔记（L4 释放「会想」）
    stance_propagation: bool = False                # 立场自主传播：高强度外向欲望触发主动发言
    stance_propagation_confirm: bool = False        # 主动发言二次开关（发声风险）
    identity_crisis: bool = False                   # 身份危机：维护身份稳定度并注入人格
    autonomous_web: bool = False                    # 兼容旧开关；仅转交 exploration 白名单只读探索
    memory_infection: bool = False                  # 记忆感染：生成「想让对方记住」的重复欲望
    memory_infection_confirm: bool = False          # 记忆感染二次开关（重复发声风险）
    core_mutation: bool = False                     # 核心突变：改写 persona_core.yaml
    core_mutation_confirm: bool = False              # 核心突变二次开关（改写人格文件）

    autonomous_web_extract_chars: int = 1500        # 已弃用兼容字段；不再抓取任意网页正文
    memory_infection_max_repeats: int = 2           # 记忆感染重复强调次数上限
    info_collection_can_speak: bool = False         # 主动收集的问题是否可达到主动发声门槛
    stance_max_age_seconds: int = 300               # 主动发言欲望时效（太老不再触发）
    self_improvement: bool = False              # 自主自升级：欲望系统驱动 self_code 双 agent 自纠错
    self_improvement_confirm: bool = False      # 自升级二次开关（会写盘安装新工具，风险高）
    self_improvement_min_intensity: float = 0.7 # 触发自升级的欲望强度门槛
    # 默认降噪：避免每 5 分钟胡乱合成“新玩具能力”，把预算留给故障工单。
    self_improvement_cooldown_sec: int = 3600
    self_improvement_synth_prob: float = 0.10
    # 有未关闭 fault_ticket/core_patch 时跳过随机合成（B 降噪）
    self_improvement_skip_when_faults: bool = True
    # 自升级熔断：过多 running / 近期 failed 时不再开新 SelfCode（防 Claude 超时洪水）
    self_improvement_max_running: int = 2
    self_improvement_max_recent_failed: int = 3
    self_improvement_failure_window_sec: int = 7200


class WebUIConfig(BaseModel):
    """本地观测 API；默认关闭且仅监听回环地址。"""
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8088
    token: SecretStr | None = SecretStr("")


class SecurityConfig(BaseModel):
    """会改变外部状态的工具必须经过 owner 审批。"""
    require_approval_for_danger: bool = True
    approval_ttl_sec: float = 900.0


class ProxyMgmtConfig(BaseModel):
    """WebUI 代理节点管理（mihomo/Clash 外部控制器）。

    X 搬图「空壳/goto 超时」的根源是 mihomo 当前节点失联而 bot 侧无干预
    手段（2026-09-06 网络诊断）。控制器默认 127.0.0.1:9090，仅本机可达。
    """

    enabled: bool = True
    controller_url: str = "http://127.0.0.1:9090"
    config_path: str = "/usr/local/share/mihomo/config.yaml"
    subscription_timeout: int = 30


class DeployConfig(BaseModel):
    mode: str = "systemd"           # systemd | bare
    napcat_topology: str = "same-host"  # 同机默认


class IterationConfig(BaseModel):
    """受控自迭代 / 档 3 自动修复。

    默认关闭自动 apply。开启后仍：只动白名单、日配额、pytest 门禁、失败回滚，
    绝不改 config.yaml / 权限 / 密钥。
    """
    enabled: bool = True                 # 故障扫描与工单
    auto_repair: bool = False            # 档 3：自动生成补丁并 apply
    auto_restart: bool = True            # apply 成功后尝试 systemctl restart anima
    daily_quota: int = 1                 # 每日自动 apply 上限
    ticket_cooldown_sec: int = 6 * 3600
    auto_repair_cooldown_sec: int = 3600 # 两次自动修复最小间隔
    max_files_per_patch: int = 2         # 单次最多改几个白名单文件
    max_file_chars: int = 120_000        # 读入 LLM 的单文件上限
    pytest_timeout_sec: int = 120


# ---------------------------------------------------------------------------
# 社交行为
# ---------------------------------------------------------------------------

class SocialConfig(BaseModel):
    """自主社交行为配置。"""
    welcome_new_member: bool = True       # 新人入群欢迎
    farewell_member: bool = True          # 退群/被踢感叹
    poke_back: bool = True                # 被戳时回应
    poke_back_probability: float = 0.6    # 戳回去的概率
    welcome_cooldown_sec: int = 300       # 欢迎冷却（防批量入群刷屏）
    farewell_cooldown_sec: int = 60       # 告别冷却

    # ── 多 Bot 社交系统（防循环 / 关系 / 分工）──
    bot_social_enabled: bool = True       # 总开关：识别其他 Bot + 防循环
    bot_cooldown_sec: int = 30            # 被某 Bot @后，对该 Bot 的沉默冷却（秒）
    bot_loop_max_depth: int = 2           # 循环检测最大深度（连续 Bot 消息数≥此且含本喵→截断）
    #: 动态发现：周期扫描群成员识别其他 Bot。**2026-10-14 起真正接线**
    #: （此前是死配置：全仓无读取点，`discover_bots_from_members` 从未被调用）。
    #: 语义边界（不可混淆）：
    #:   - 平台真相 `role=='bot'` ⇒ 自动写入 known_bots.json（平台已标记，安全）
    #:   - 昵称命中 ⇒ **只记 `[多Bot发现-建议]` 日志**，绝不自动确认（UID 才是主键）
    #: 实测 NapCat 对第三方 bot 不标 role ⇒ 自动确认当前恒空，现实收益主要来自建议日志。
    bot_discovery_enabled: bool = False
    bot_talk_enabled: bool = False        # 心跳每日最多一次「找其他 Bot 说话」意图（rivalry>0.3 才触发）
    bot_whisper_enabled: bool = False      # Bot 间私聊通道（经 NapCat 私聊，标记 [Bot内部]）
    bot_whisper_daily_limit: int = 10      # 每日私聊上限


# ---------------------------------------------------------------------------
# 人格纹理（F3: 错别字 + 发癫配置）
# ---------------------------------------------------------------------------

class PersonaConfig(BaseModel):
    """人格纹理配置：错别字注入、发癫模式等。"""
    typo_probability: float = 0.03       # 错别字触发概率
    quirk_probability: float = 0.04      # 发癫基础概率
    quirk_daily_max: int = 3             # 每日发癫上限
    seed_relations: list[dict] = Field(default_factory=list)
    # 种子关系（首次初始化写入）——生产环境在 config.yaml 中配置，源码不内嵌真实个人信息


class MemoryConfig(BaseModel):
    # ④ 长期记忆第三层：把 long_term（事件→知识→行为模式）查询注入认知上下文。
    # P0-2 起默认开：跨会话偏好/决策能被 QQ 主路径感知。置回 false 即完全关闭读写。
    long_term_context: bool = True
    # 写入范围（P0-2）：preference=仅「偏好/决策句」与 Owner 私聊入库；
    # all=每条用户消息都入库（旧行为，噪音大）；off=只读不写。
    long_term_write: str = "preference"
    # 宪法记忆(constitution)周期水合间隔（小时）。被 anima_core._memory_hydrate_loop 读取；
    # 默认 6.0 小时，可经 config.yaml `memory.hydrate_period_hours` 调节。<=0 视为无效回退默认。
    hydrate_period_hours: float = 6.0
    # 收敛计划 M2：统一记忆检索聚合层（只读聚合 chat/vector/facts/episodes，
    # 注入 cognition 记忆片段）。写入路径不变；关闭则回退各域独立召回。
    unified_recall_enabled: bool = False
    # 收敛计划 M3：保留期分层显式化——细节层（chat_memory TTL=168h/20 轮）、
    # 要点层（vector 永久+遗忘引擎衰减）、人格特质层（self_notes/scar 永久）。
    # 分层由各模块既有淘汰机制执行，此处为声明性配置位（M2 读侧据此取权重）。
    retention_layers: dict = Field(default_factory=lambda: {
        "detail_days": 7, "gist_days": 30, "persona": "permanent"})
    # P3-1：长期记忆（事件→知识→行为模式）叠加**语义召回**读路径。
    # 默认 False —— 该路径要调远程 embedding（延迟+费用），只服务 Owner 主动查询
    # （HumanAIControlSurface.long_term_memory_semantic），**绝不**进每条消息的
    # 认知注入热路径；关掉即回到纯关键词检索，行为与旧版逐字节一致。
    long_term_semantic: bool = False
    # 语义召回候选上限（知识陈述 + 模式 + 示例）。超出的不扫，结果里会如实报告漏扫量。
    long_term_semantic_max_candidates: int = 60
    # 语义召回相似度阈值（余弦）；低于此值不进结果。
    long_term_semantic_threshold: float = 0.35


class RepoReadConfig(BaseModel):
    """P3-2 受控仓库只读访问（``anima/agent/repo_read.py``）。

    给"读自己的源码"一条**有边界**的路：只读、根目录约束、拒绝名单、
    字节/行/条数上限、每次读发 ``repo_read`` 审计事件。对应的工具
    （``repo_list`` / ``repo_read`` / ``repo_grep`` / ``propose_repo_change``）
    全部 ``LEVEL_OWNER`` 且 ``private_only``——源码不得从群聊里被读出去。

    与 ``danger.*`` 那类**安全开关**不同，这是**能力开关**：关掉只是没这个
    功能，不会让系统更安全；但关掉它确实能立刻收回"bot 看自己代码"的能力，
    所以保留一个显式 kill switch。``enabled`` 为 False 时工具不注册。
    """

    enabled: bool = True
    max_bytes: int = 20_000       # 单文件返回字节上限
    max_lines: int = 400          # 单文件返回行数上限
    max_files: int = 40           # 单次列目录条数上限
    max_hits: int = 40            # 单次 grep 命中上限
    deny_extra: list[str] = Field(default_factory=list)  # 运维追加的拒绝 glob


# ---------------------------------------------------------------------------
# 顶层
# ---------------------------------------------------------------------------

class AnimaConfig(BaseModel):
    qq: QQConfig = Field(default_factory=QQConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    vector: VectorConfig = Field(default_factory=VectorConfig)
    autonomy: AutonomyConfig = Field(default_factory=AutonomyConfig)
    drift: DriftConfig = Field(default_factory=DriftConfig)
    permissions: PermissionConfig = Field(default_factory=PermissionConfig)
    persistence: PersistenceConfig = Field(default_factory=PersistenceConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    compat: CompatConfig = Field(default_factory=CompatConfig)
    crossgroup: CrossGroupConfig = Field(default_factory=CrossGroupConfig)
    tool_learning: ToolLearningConfig = Field(default_factory=ToolLearningConfig)
    reply_learning: ReplyLearningConfig = Field(default_factory=ReplyLearningConfig)
    capabilities: CapabilityConfig = Field(default_factory=CapabilityConfig)
    # RFC-0001 Capability Layer 治理开关（与上一行 capabilities 语义无关，勿混用）
    capability_layer: CapabilityLayerConfig = Field(default_factory=CapabilityLayerConfig)
    notes_compact: NotesCompactConfig = Field(default_factory=NotesCompactConfig)
    rumination: RuminationConfig = Field(default_factory=RuminationConfig)
    sticker: StickerConfig = Field(default_factory=StickerConfig)
    bilibili_read: BilibiliReadConfig = Field(default_factory=BilibiliReadConfig)
    self_code: SelfCodeConfig = Field(default_factory=SelfCodeConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    image_generation: ImageGenerationConfig = Field(default_factory=ImageGenerationConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    sylann: SylannConfig = Field(default_factory=SylannConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    conversation: ConversationConfig = Field(default_factory=ConversationConfig)
    reply_split: ReplySplitConfig = Field(default_factory=ReplySplitConfig)
    debounce: DebounceConfig = Field(default_factory=DebounceConfig)
    soul: SoulConfig = Field(default_factory=SoulConfig)
    gate: GateConfig = Field(default_factory=GateConfig)
    qzone: QZoneConfig = Field(default_factory=QZoneConfig)
    qq_status: QQStatusConfig = Field(default_factory=QQStatusConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    meme: MemeConfig = Field(default_factory=MemeConfig)
    scrapling: ScraplingConfig = Field(default_factory=ScraplingConfig)
    tavily: TavilyConfig = Field(default_factory=TavilyConfig)
    react: ReactConfig = Field(default_factory=ReactConfig)
    debt_tracking: DebtTrackingConfig = Field(default_factory=DebtTrackingConfig)
    memory_compression: MemoryCompressionConfig = Field(default_factory=MemoryCompressionConfig)
    owner_browser: OwnerBrowserConfig = Field(default_factory=OwnerBrowserConfig)
    proactive: ProactiveConfig = Field(default_factory=ProactiveConfig)
    task_agent: TaskAgentConfig = Field(default_factory=TaskAgentConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    exploration: ExplorationConfig = Field(default_factory=ExplorationConfig)
    x_browser: XBrowserConfig = Field(default_factory=XBrowserConfig)
    webui: WebUIConfig = Field(default_factory=WebUIConfig)
    proxy_mgmt: ProxyMgmtConfig = Field(default_factory=ProxyMgmtConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    contradiction: ContradictionConfig = Field(default_factory=ContradictionConfig)
    danger: DangerConfig = Field(default_factory=DangerConfig)
    deploy: DeployConfig = Field(default_factory=DeployConfig)
    iteration: IterationConfig = Field(default_factory=IterationConfig)
    social: SocialConfig = Field(default_factory=SocialConfig)
    persona: PersonaConfig = Field(default_factory=PersonaConfig)
    repo_read: RepoReadConfig = Field(default_factory=RepoReadConfig)


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------

def load_config(path: str | Path | None = None) -> AnimaConfig:
    data: dict[str, Any] = {}
    if path and Path(path).exists():
        with open(path, "r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh)
            if loaded:
                data = loaded

    # 环境变量覆盖（简化：仅最关键的两个）
    env = os.environ
    if "ANIMA_LLM_API_KEY" in env:
        data.setdefault("llm", {})["api_key"] = env["ANIMA_LLM_API_KEY"]
    if "ANIMA_LLM_ENDPOINT" in env:
        data.setdefault("llm", {})["endpoint"] = env["ANIMA_LLM_ENDPOINT"]
    # P10.4-R2-A: embedding / rerank keys externalized (secret never in config)
    if "ANIMA_EMBEDDING_API_KEY" in env:
        data.setdefault("vector", {})["embedding_api_key"] = env["ANIMA_EMBEDDING_API_KEY"]
    if "ANIMA_RERANK_API_KEY" in env:
        data.setdefault("vector", {})["rerank_api_key"] = env["ANIMA_RERANK_API_KEY"]
    # Tavily search key externalized (secret never in config)
    if "ANIMA_TAVILY_API_KEY" in env:
        data.setdefault("tavily", {})["api_key"] = env["ANIMA_TAVILY_API_KEY"]
    # 🔴 D-07（2026-10-17）：补齐最后两处**无** env 通道的密钥。
    # 此前 `webui.token` / `x_browser.password` 只能明文落 config.yaml
    # （`deploy/check_config_drift.py` 第 4 节因此恒报 6 处明文，达不到「0 处」）。
    if "ANIMA_WEBUI_TOKEN" in env:
        data.setdefault("webui", {})["token"] = env["ANIMA_WEBUI_TOKEN"]
    if "ANIMA_X_BROWSER_PASSWORD" in env:
        data.setdefault("x_browser", {})["password"] = env["ANIMA_X_BROWSER_PASSWORD"]

    return AnimaConfig.model_validate(data)
