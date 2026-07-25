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
    circuit_failure_threshold: int = 3  # 连续失败达到此值后短暂熔断，保护 QQ 主链路
    circuit_cooldown_sec: float = 30.0
    # 可选的命名提供商配置。保留旧字段是为了兼容现有 config.yaml；
    # WebUI 的模型控制中心优先使用 active_provider，未配置时回退到旧字段。
    providers: dict[str, dict[str, Any]] = Field(default_factory=dict)
    active_provider: str = "default"
    # module -> model；空字符串表示继承默认模型。调用点显式传 model 时优先级更高。
    module_models: dict[str, str] = Field(default_factory=dict)
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

    # —— 主动发言（自主心跳 speak）策略 ——
    # 通过全部主动发言门控后的实际开口概率。默认保守，避免群里突然刷屏。
    active_reply_prob: float = 0.08
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


class CapabilityConfig(BaseModel):
    """个人能力自生长系统（P1）：bot 从经历里长出可复用方法论。"""
    enabled: bool = True              # 总开关
    initial_confidence: float = 0.3   # 新建能力基线置信度（脱钩 LLM 自评）
    auto_register_confidence: float = 0.65  # 仅作参考阈值（实际注册走 value_score 晋升）
    dynamic_daily_quota: int = 3      # 每日新注册能力工具上限
    max_total: int = 40               # 能力库硬上限
    unused_drop_days: int = 30        # 未使用超此天数淘汰
    unused_decay_days: int = 14       # 未使用超此天数降权
    dedup_text_threshold: float = 0.5 # 语义去重文本相似度兜底阈值（0.5：表面相近的中文物也走兜底合并；真·语义重复由 LLM 去重把关）
    max_zero_usage: int = 12         # 零调用能力软上限：超出则按价值分淘汰最低价值者，止住冗余堆积
    min_create_text_len: int = 6      # 能力名+描述+用法总内容最小长度（防碎碎念）


class NotesCompactConfig(BaseModel):
    """自我笔记压缩（兑现 learning.py docstring 承诺，止住 self_notes.md 无限膨胀）。"""
    enabled: bool = True             # 总开关
    notes_max_keep: int = 300        # self_notes.md 保留最近 N 行，超出部分交给 LLM 摘要归档
    compact_every_ruminations: int = 6  # 每 N 次反刍触发一次压缩（避免频繁调用 LLM）
    compact_model: str = ""          # 压缩所用模型；空=复用 llm.model


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
    always_atmosphere: list[str] = ["humorous", "casual"]  # 允许带图的群氛围
    recent_window: int = 4          # 同一群最近 N 张不重复（候选不足时允许复用）
    gif_min_interval_sec: int = 1800  # 同一群动图最小间隔，避免刷屏
    classify_at_collect: bool = True   # LLM 视觉闸门：导入时判定是否为表情包并分类（否=拒收）
    llm_select: bool = True            # LLM 视觉终选：发送时从候选里挑最贴的（否=退回本地标签匹配/随机）
    select_model: str = ""             # 选图/分类视觉模型；空=复用 label_model
    # --- 对话中被动自动附带（照 astrbot_plugin_stealer 概率/冷却模式精神）---
    send_probability: float = 0.35    # LLM 未主动选图时，被动附带表情的概率（0=关，1=必带）
    send_by_emotion: bool = True      # 被动附带优先按回复情绪匹配；否则随机
    send_cooldown_sec: float = 20.0   # 同群被动带图最小间隔（秒），防连续刷屏
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
    comprehend_in_chat: bool = True  # 用户在聊天里发图时，bot 当场看图并接话
    store_desc_to_memory: bool = True  # 把图片一句描述存进对话记忆，供后续轮次忆起


class ImageGenerationConfig(BaseModel):
    """图像生成；默认关闭以避免意外成本。daily_limit=0 表示不限制每日张数。"""
    enabled: bool = False
    model: str = "gemini-3.1-flash-image"
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


class ReplySplitConfig(BaseModel):
    """拟人分段发送 + 拟真延迟（参考 astrbot_plugin_splitter）。"""
    enabled: bool = True
    max_segments: int = 4
    min_segment_len: int = 40
    max_segment_len: int = 120
    trigger_threshold: int = 150      # 低于此字数不分段（避免短回复被切成多条）
    tail_merge_threshold: int = 15    # 尾段低于此字数合并到前段
    delay_enabled: bool = True
    delay_base: float = 0.4           # 基础延迟（秒）
    delay_factor: float = 0.02        # 每字延迟（秒/字）
    delay_jitter: float = 0.3         # 随机抖动上限（秒）


class DebounceConfig(BaseModel):
    """群聊消息防抖（参考 astrbot_plugin_continuous_message）。"""
    group_enabled: bool = True
    group_wait_sec: float = 2.0
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

    active_info_collection: bool = False            # 主动信息收集：生成自然提问存入欲望
    relationship_inference: bool = False            # 关系图谱推断：从对话抽取人际关系笔记
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


# ---------------------------------------------------------------------------
# 人格纹理（F3: 错别字 + 发癫配置）
# ---------------------------------------------------------------------------

class PersonaConfig(BaseModel):
    """人格纹理配置：错别字注入、发癫模式等。"""
    typo_probability: float = 0.03       # 错别字触发概率
    quirk_probability: float = 0.04      # 发癫基础概率
    quirk_daily_max: int = 3             # 每日发癫上限
    seed_relations: list[dict] = Field(default_factory=lambda: [
        {"qq": 1562290139, "name": "吴雨萌", "stage": "CLOSE",
         "familiarity": 0.9, "trust": 0.95, "valence": 0.7,
         "tags": ["开发者", "主人", "MTF", "HRT"]},
        {"qq": 1371704917, "name": "华初", "stage": "FRIEND",
         "familiarity": 0.6, "trust": 0.7, "valence": 0.4,
         "tags": ["雨萌的对象", "MTF"]},
    ])  # 种子关系（首次初始化写入）


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
    compat: CompatConfig = Field(default_factory=CompatConfig)
    crossgroup: CrossGroupConfig = Field(default_factory=CrossGroupConfig)
    tool_learning: ToolLearningConfig = Field(default_factory=ToolLearningConfig)
    capabilities: CapabilityConfig = Field(default_factory=CapabilityConfig)
    notes_compact: NotesCompactConfig = Field(default_factory=NotesCompactConfig)
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
    context: ContextConfig = Field(default_factory=ContextConfig)
    exploration: ExplorationConfig = Field(default_factory=ExplorationConfig)
    webui: WebUIConfig = Field(default_factory=WebUIConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    contradiction: ContradictionConfig = Field(default_factory=ContradictionConfig)
    danger: DangerConfig = Field(default_factory=DangerConfig)
    deploy: DeployConfig = Field(default_factory=DeployConfig)
    iteration: IterationConfig = Field(default_factory=IterationConfig)
    social: SocialConfig = Field(default_factory=SocialConfig)
    persona: PersonaConfig = Field(default_factory=PersonaConfig)


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

    return AnimaConfig.model_validate(data)
