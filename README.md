# Anima-next

Anima-next 是一个**独立运行的、OpenClaw 风格的全智能 QQ 机器人**（Autonomous Agent Operating System）。它不依赖 AstrBot：通过 OneBot11 / NapCat 接收和发送 QQ 消息，使用 OpenAI 兼容的 LLM 完成对话、工具调用与多模态理解，并拥有独立的记忆、时间、关系、心跳、日历、审计、权限、SelfCode 工程扩展与受控自我进化能力。

> **定位（Post-R15，2026-08-17 起）**：本项目从「具备自治能力的 Agent 系统」演进为 **Owner 长期使用的个人 Autonomous Agent Operating System（非商业产品）**。终态是 `Owner → ANIMA OS →（记忆/技能/Agent/知识/运行时/演化/运维）`。明确不做：商业化、Marketplace、SaaS、多租户、企业权限扩展、用户增长。

当前版本：`0.3.0rc2`（`anima/__init__.py`）。项目按 **AGPL-3.0-only** 许可发布。生产部署在 VPS `/opt/anima`，经 `_deploy.py` 发布，本仓库是唯一生产源码。历史 AstrBot 插件形态 `G:/Anima` 已冻结，不再开发新功能。

> **文档坐标（2026-09-18 建立，避免同一问题有四份答案）**
> - 定位 / 许可证 / 理论层关系的**唯一权威**：**[`docs/VISION.md`](docs/VISION.md)**
> - 阶段编号（六套并行编号 → 单一坐标）：**[`docs/PHASE_COORDINATES.md`](docs/PHASE_COORDINATES.md)**
> - 未决事项（唯一工程债台账）：**[`ENGINEERING_DEBT_LEDGER.md`](ENGINEERING_DEBT_LEDGER.md)**
> - 运行时执行面（F5 `runtime/` · `pilot/` · A/B 双轨归属）：**[`docs/RUNTIME_SURFACE_STATUS.md`](docs/RUNTIME_SURFACE_STATUS.md)**
> - 生产实配（脱敏模板 + 漂移检查）：`config/production.example.yaml` · `deploy/check_config_drift.py`
>
> 🔴 引用本文档的"能力治理开关默认关闭"时请一并注意：**代码默认 ≠ 生产实配**
> （见 `MAINTENANCE.md` 治理开关表的第 3 列）。

---

## 目录

1. [当前能力](#当前能力)
2. [系统架构](#系统架构)
3. [代码库总览（模块地图）](#代码库总览模块地图)
4. [WebUI 控制台](#webui-控制台)
5. [WebUI API](#webui-api)
6. [Owner QQ 命令](#owner-qq-命令)
7. [配置](#配置)
8. [开发与测试](#开发与测试)
9. [CI/CD](#cicd)
10. [部署](#部署)
11. [数据与安全边界](#数据与安全边界)
12. [自治与自我进化](#自治与自我进化)
13. [维护](#维护)
14. [许可证与对应源码](#许可证与对应源码)

---

## 当前能力

### 消息与渠道（QQ）

- **OneBot11 WebSocket** 入站 + HTTP 出站（NapCat）；私聊 / 群聊 / @ 检测 / 去重 / 引用消息取图 / 图片与表情采集。
- **背压与并发**：`event_concurrency` + `max_pending_events` 控制长业务事件上限与高峰等待；消息防抖（群聊合并连续输入）、私聊连续输入收束为一个语义回合、同会话回合串行锁、同会话回复在途合并。
- **出站**：拟人分段发送（`reply_split`）+ 拟真打字延迟；出站前统一过滤内心独白；表情 / 图片 / GIF / 语音 / 文档附件出站。
- **渠道鉴权**：`qq.access_token`（`Authorization: Bearer`），不读 `NAPCAT_TOKEN` 环境变量。

### 认知内核

- **人格**：外置哈基米人格（`config/persona_core.yaml`），单一 system 消息护栏；身份泄漏拦截、anti_derailment、人格漂移上限（`drift.*`）。
- **状态模型**：时间感、欲望、世界观、关系、矛盾、伤痕（含回避期）、遗忘、反刍、群氛围、情绪（灵魂能量槽 EMA）、模拟生活、睡眠状态、自我笔记压缩。
- **主动性与生活模拟**：Heartbeat（30s tick）驱动内省与主动发言冲动；昼夜 / 冷却 / 频率 / Token 预算约束；免打扰时段（quiet_hours）；晨间反思、梦境分享、秘密生成、Q 空间说说、群在场感。
- **多模态**：全量视觉理解（`vision`）、图片描述入库记忆、大图下载 + WebP 缩放 / GIF 关键帧 + 预算控制、LLM 生图（含跨模型 failover）、语音转写（可选）。
- **表情包系统**：群图片自动收集、pHash 感知哈希去重、LLM/VLM 入库判断、情绪/场景/标签分类、Owner 审核池、语义选图、群作用域、黑白名单、发送冷却、自然遗忘、GIF 发送节流。

### 记忆（多源、分层）

- **对话记忆**：按「群号 + QQ 号」隔离（`chat_memory`），不会把整群混成一个会话。
- **向量记忆**：`sqlite-vec` + 远程 embedding/reranker，语义召回；未配向量库自动降级 BM25 / 无语义召回。
- **跨群记忆**（`crossgroup`）：跨群事实抽取 + 群氛围差异化，只进唯一 system。
- **长期记忆**（`memory/long_term.py`）：事件 → 知识 → 行为模式三层，含「为什么这么设计」专项（服务自治维护护栏）。
- **宪法记忆**（`constitution/memory_engine.py`）：只读 / 有界 / fail-soft 开机水合 + 周期去重累积。
- **情节记忆 / 连续性 / 运行时身份 attestation**：episodes、continuity、identity/attestation 契约。

### 工具 / 沙箱 / Skills

- **工具注册表**：内置工具 + 沙箱；`min_level` 权限分级（banned/readonly/user/admin/owner），ReAct 二次校验。
- **沙箱隔离**：SandboxTier 分级、bwrap/unshare、RLIMIT/cgroup 资源限制、seccomp。
- **Skills**：声明式 SkillDraft（自然语言生成）→ Owner 审批 → 安装执行；内置 `compose_notice`；个人能力生态闭环（提案→沙箱→Benchmark→采纳，须 Owner）。
- **SelfCode 双 Agent**：Coder 写隔离 workspace 模块 → Critic APPROVE/REVISE + 静态契约回灌 → 沙箱 import 自测 → 工程任务 → Owner 批准 → 备份 → 热注册。
- **工具自学习**（`tool_learning`）与 **内生能力自生长**（`capabilities`，Top-K 晋升注册进 ReAct 工具面）。

### 主动联网 / 订阅 / 探索

- **联网搜索**：X（Twitter）搬图（受管浏览器 patchright，会话级去重、风控退避、VNC 图形化登录）、Web 搜索、网页正文提取摘要。
- **订阅监控**（`monitors`）：B 站直播间开播通知、公开网页状态监控；Owner 自然语言建订阅，持续订阅进入审批。
- **探索**（`exploration`）：自主读取公开信息形成带来源的知识笔记，外部内容视为不可信资料。
- **B 站视频理解**：字幕抓取 + LLM 总结。

### 工程 / 自治 / 自我进化

- **Owner Agent**（`agent/`）：多步任务规划 / 执行 / 存储 / 审计 / 通知。
- **受控自我进化**（R9）：ANIMA 可提自身优化但绝不自我授权；`CORE_PROTECTED` 安全围栏禁止自动改核心/权限/安全/部署代码；`EvolutionGate` 默认 DENY，仅 Owner 显式 approve。
- **多 Agent 协作**（R7/R12/P3）：Orchestrator 流水线、Debate & Judge、真实 Agent 社会（Architect/Security/Research/Coding/Critic/Judge 多视角加权裁决）。
- **自治开发闭环**（R8）：DevIssue → Plan → Develop → Test → Review → Deploy，部署需 Owner 授权。
- **观测 / 评估 / 闭环**（R10/P1/P2）：Observation 事件层、Evolution Evaluation（Before/After 指标证据）、Evolution Impact Score、自治运维闭环（默认 dry_run）。
- **自治运维**（P5）：过期依赖检测 → 风险分析 → 待审提案 → Owner 批准。
- **Human-AI OS 控制面**（R14/P0）：`/api/os/overview` 聚合健康/恢复/提案/观测全景；受限 NL 指令解析；每日健康证明、7/30/90 天稳定性记录（fail-closed、诚实）。

### 安全 / 治理 / 审计

- **权限模型**：QQ 号 + 群号分级（owner/admin/readonly/banned + 只读/静默群 + 群策略）；RBAC 引擎（R6.3）。
- **Fail Closed · No Hidden Authority · Identity Cannot Be Forged · Audit Everything · Production Is Sacred**（五条不可违反）。
- **审计**：JSONL append-only 审计、端到端审计事件链（event_chain）、审计哈希链、运行时 attestation。
- **行为证据**：execution trace → artifact → audit → replay（任何行为可事后还原）。
- **执行隔离**：CPU / 内存 / 超时 / 进程数 / seccomp 策略档 + 资源记账。
- **SylannEngine**（可选，`sylanne-core 2.5.0`）：语义评估 / 可选 embedding / 会话状态 / 伤害影响注入 / 心跳 tick；只提供粗粒度语气建议，不发送 QQ 消息、不读聊天记忆、不调用工具、不绕过护栏。默认 `disabled`，低权重 `active`。

### 生产接线状态（WIRED vs 默认 OFF）

上面列的是「代码里有」，不等于「线上在跑」。完整三档清单（WIRED / WIRED·默认 OFF / 库级无入口）与踩坑记录见 **[MAINTENANCE.md §3](MAINTENANCE.md)**。最容易误判的几条：

| 状态 | 例子 |
|---|---|
| **WIRED（默认开启）** | 工具注册表 / 五段 pipeline / Heartbeat / 记忆 / 审计 / `react.memory_first` / `react.freshness_footer` / `debt_tracking` / `task_agent.verify_steps`（步骤验收 + 失败重规划 + 并行批次） / `repo_read.enabled`（只读代码访问，工具本身仅 Owner + 私聊） |
| **WIRED 但默认 OFF** | `self_code.enabled`、`image_generation.enabled`、`x_browser.enabled`、`qzone` / `voice` / `meme` / `gate`、`danger.*` 全家、`capability_layer.*`（受治理能力层）、`memory.long_term_semantic`（语义召回，可选增量读路径）、`exploration.share_enabled`、`social.bot_*` |
| **库级（无生产入口）** | `runtime.resolving_executor`、`runtime.pilot_*`、`runtime.bridge_bootstrap`、`runtime.startup_recovery`、`core/exceptions.py`、`core/paths.Paths` |

⚠️ 表中是**代码默认值**：生产真实值以 VPS `/opt/anima/config.yaml` 为准（`_deploy.py` 默认不覆盖生产配置）。核实手段：`python -m anima doctor`、WebUI 配置页、`journalctl -u anima`。

⚠️ **同一个 `0`，语义可能相反**（易踩坑，详见 MAINTENANCE §3.4）：`exploration.daily_limit: 0` = 不限制（内部读取配额）；`exploration.daily_share_limit: 0` = 当天完全不分享（外发闸门 → fail-closed）。

---

## 系统架构

```text
QQ / NapCat
   → OneBot11 WS（事件入站） + OneBot11 HTTP（action/echo 出站）
   → Gateway / EventBus
   → LLMHandler → Pipeline（权限 → 记忆 → 认知 → 生成 → 护栏）
   → NapCat WS 出站

认知与运行时：Memory / Relations / Time / Agenda / Heartbeat / Sleep / Simulated Life
行动能力：ReAct Tools / Skills / Monitors / Image generation / X browser / SelfCode
自治与控制：autonomy / agent / evolution / observation / os / capability governance
观察与运维：Audit JSONL / WebUI / owner QQ commands / CI / _deploy.py
```

### 消息流水线（Pipeline）

`anima/pipeline/stages/` 五阶段：**permission → memory → cognition → generation → guardrail**。各阶段职责：
- **permission**：按用户/群权限等级过滤，群策略判定。
- **memory**：写入/读取对话记忆与上下文。
- **cognition**：注入人格、时间、欲望、关系、世界观、跨群事实、多模态视觉。
- **generation**：调 LLM 生成，注入工具引导、错别字、分段、剥标签。
- **guardrail**：危险行为检测、身份泄漏拦截、内心独白过滤、记忆写入。

### 核心生命周期

- `cli.py` → `AnimaCore.start()`：初始化认知单例 → Sylann → 向量记忆 → 订阅调度器 → 原生插件 → 各 Module `restore/init` → 社交监听 → X 浏览器 → WebUI → 心跳 → 宪法记忆水合 → 运行健康采样。
- `AnimaCore.run()`：启动 systemd watchdog，`SIGTERM/SIGINT` 优雅停止，逐个 `persist/shutdown` 认知模块与资源。
- **配置变更恢复**：`config_recovery`（`awaiting_start → attempting → healthy`），候选启动失败自动回滚上一版本配置。

---

## 代码库总览（模块地图）

`anima/` 下 **23 个顶层包**（含 `__init__.py`，另含 `util/` 无 `__init__.py` 的命名空间目录；约 400 个 `.py` 文件）：

| 包 | 职责 |
|---|---|
| `anima/core/` | Gateway、EventBus、AnimaCore、Config、Permissions、Persistence、Audit、autonomy(Heartbeat)、intents、workflows、tasks、approvals、backup、evaluation、replay、config_recovery/config_service/config_drift、reply_splitter、conversation_assembler |
| `anima/channels/` | QQ 渠道适配（`onebot11.py`，OneBot11 WS+HTTP） |
| `anima/llm/` | LLMClient（重试/熔断/fallback/413 再编码/用量账本）+ LLMHandler 对话处理器 + group_gate |
| `anima/pipeline/` | 消息流水线五阶段 |
| `anima/cognitive/` | 认知内核：欲望/关系/时间/遗忘/反刍/伤痕/矛盾/情绪/灵魂/日程/日历/梦境/探索/学习/能力自生长/回复质量/表情/视觉/SelfCode/模拟生活/睡眠/X浏览器/秘密 |
| `anima/cognitive/inner_state/` | 内在状态（情绪连续性/硬事实/自我述说/矛盾感受/被忽视感/QZONE 生活） |
| `anima/memory/` | 对话/向量/长期/情节/连续性/权威水合/身份验证/运行时 attestation |
| `anima/crossgroup/` | 跨群记忆 + 群氛围 |
| `anima/skills/` | Skill 注册表/草案/演进 + builtin compose_notice |
| `anima/tools/` | 工具注册表 + 沙箱 + builtin 工具 + 能力工具工厂 |
| `anima/runtime/` | 运行时编排/执行/恢复/持久化 + providers |
| `anima/agent/` | Owner 多步 Agent、多 Agent 编排/社会、自治开发闭环、自我进化、自治运维 |
| `anima/capability/` | 能力治理层：RBAC/隔离/证据/审计链/运行时契约/激活/健康/ops |
| `anima/constitution/` | 宪法（记忆权威/人格/身份/需求/情感/驱动/反射/规划/工作流） |
| `anima/contracts/` | 契约抽象（access/capability/execution/facades/lifecycle/pipeline） |
| `anima/evolution/` | 演化评估/影响/陪审团/orchestrator |
| `anima/observation/` | 观测事件层（运行时/审计/反馈/环境/工程闭环） |
| `anima/os/` | Human-AI OS 控制面、可靠性、长期运行稳定性 |
| `anima/monitors/` | 订阅调度器 + B 站 + HTTP 监控 |
| `anima/pilot/` | 试点/rollout 层（canary/权限边界/安全过滤/生产监控） |
| `anima/plugins/` | 原生插件运行时 |
| `anima/compat/` | AstrBot 兼容层（api/core 垫片，Tier 1）+ 插件加载 |
| `anima/adapters/` | 运行时契约适配器 |
| `anima/util/` | 工具（`name_clean.clean_display_name`，命名空间目录无 `__init__.py`，被 crossgroup/llm 使用） |

顶层模块：`anima/cli.py`（CLI 入口）、`anima/media.py` / `media_generation.py` / `media_observation.py`、`anima/watchdog.py`、`anima/release_preflight.py`、`anima/__main__.py`。

> **注意**：部分模块为**库级 / 试点 / 测试层**，未接入生产 `cli/startup` 主链路（例如 `runtime` 下的 executor/pilot_*/bridge_bootstrap/startup_recovery 等，以及 `capability` 下的治理 gateway 在默认配置下 OFF）。生产接线路径以 `cli.py` 与 `AnimaCore.start()` 为准，见「维护」文档。

---

## WebUI 控制台

Vue 3 + TypeScript 单一控制台（源码 `webui/src/`，`npm run build` 产出 `anima/webui/static/`）。默认生产监听 `0.0.0.0:8088`（公网入口见部署），Token 认证已启用。

| 页面 | 入口 | 用途 |
|---|---|---|
| 生命总览 | `/` | QQ/LLM/NapCat/Sylann/心跳/看门狗健康；待办、活动、用量摘要 |
| 一生 | `/life` | 一生档案/传承：时间线、快照恢复/回滚、遗忘、日记、伤痕、议程 |
| 对话与记忆 | `/memory` | 连续性、群体感知、自我笔记、长期记忆、脱敏时间线 |
| 认知状态 | `/cognitive` | 身份稳定度、欲望、关系、矛盾、伤痕、漂移、Sylann 状态 |
| 能力与自动化 | `/abilities` | Skills、探索、订阅、议程、日历、表情资产、插件 |
| 审批与工程 | `/engineering` | 工程任务、能力治理、影响、diff/测试证据、受控自我进化、回滚点 |
| 设置与权限 | `/settings` | LLM 快速配置、模块模型路由、Token 统计、行为策略、权限、配置变更集 |
| ANIMA OS | `/os` | 自治工作台：健康/恢复/提案/观测/稳定性/演化影响/多 Agent 社会 |
| 表情资产工作室 | `/stickers` | 表情审核池、图库、导入、搜索排序、批量管理、LLM/VLM 重审 |
| 登录 | `/login` | 写入安全 Cookie；密钥不回显 |
| 对应源码 | `/source` | 许可证、运行版本、源码归档与校验信息；无需登录 |

主控制台各区域独立加载，某非核心接口失败只降级对应卡片。支持系统「减少动态效果」；窄屏设置页模型路由保留可横向滚动表格。

### WebUI Token

Token 保存在生产配置 `/opt/anima/config.yaml` 的 `webui.token`（≥32 位随机串），不写入本 README / 群聊 / Git。查看：

```bash
grep -A4 '^webui:' /opt/anima/config.yaml
systemctl restart anima
systemctl show --property=ActiveEnterTimestamp anima
```

首次访问 `http://127.0.0.1:8088/?token=<token>` 会种下 HttpOnly Cookie；调用 API 用 `Authorization: Bearer <token>`。`webui.token` 未配置时**全部接口 fail-closed 锁定（503），仅登录页可渲染**——生产必须先配置 ≥32 位随机 token。

---

## WebUI API

所有 `/api/*` 均需 Bearer token（或登录 Cookie）认证；观察类接口无需 owner，控制类接口需 owner。主要端点分组：

**健康 / 运维**
- `GET /api/health`、`/api/health/history`、`/api/modules/status`、`/api/evaluation`
- `POST /api/diagnostics/napcat`、`/api/reply-quality`、`/api/ops-quality`
- `GET/POST /api/backups`、`/api/provenance`、`/api/audit`
- `GET/POST /api/replays`、`/api/replays/run`

**配置 / LLM**
- `GET/POST /api/config/manifest|state|validate`、`/api/config/change-sets[...]`（apply/rollback/reject）
- `GET/POST /api/config/drift`、`/api/config/drift/repin`
- `GET/POST /api/llm/providers`、`/api/llm/routes`、`/api/llm/usage[/events|/export]`

**认知 / 生活**
- `GET /api/cognitive/overview|contradictions|relations|identity|desires|scars|prompt|persona|drift|config`
- `POST /api/cognitive/persona/rollback`、`/api/cognitive/drift`、`/api/cognitive/config`
- `GET /api/subject`、`/api/life`、`/api/timeline`、`/api/life/timeline`、`/api/life/snapshot/staged|restore|rollback|apply|live-rollback|settings`
- `GET /api/platform/summary`、`/api/soul/state`、`/api/soul/history`
- `GET /api/agenda`、`/api/intents`、`/api/workflows`、`/api/calendar`；`DELETE /api/agenda/{id}`、`DELETE /api/calendar/{id}`、`POST /api/workflows/{id}/cancel`

**自主 / 受控迭代**
- `GET /api/autonomy/status`、`POST /api/autonomy/pause|resume`
- `GET /api/iterate/status`、`POST /api/iterate/scan`、`/api/iterate/{id}/escalate|prepare|approve|apply|diffs`

**能力 / Skills / 订阅 / 探索**
- `GET/POST /api/skills`、`/api/skills/{id}/execute`
- `GET/POST /api/skill-drafts`、`/api/skill-drafts/{id}/preview|approve|reject`
- `GET/POST/DELETE /api/subscriptions`、`/api/exploration`、`/api/exploration/run`
- `GET /api/compat/plugins`、`/api/native-plugins`

**SelfCode / 工程 / 自治**
- `GET/POST/DELETE /api/selfcode/tools...`、`/api/selfcode/backups/{id}/restore`、`/api/selfcode/evaluate`
- `GET/POST /api/tasks`、`/api/tasks/{id}/execute|approve|reject|redo`
- `GET/POST /api/approvals`、`/api/approvals/{id}/approve|reject`
- `GET/POST /api/evolution/proposals|propose|{id}/approve|reject`
- `GET /api/os/overview|self-evaluation|engineering-loop|long-term-operation`、`POST /api/os/command`

**权限 / 工具 / 表情 / 社交**
- `GET/POST/DELETE /api/permissions`、`/api/permissions/admins`（`DELETE /api/permissions/admins/{qq}`）
- `GET /api/tools`
- `GET/POST /api/stickers`、`/api/stickers/assets|policy|import|batch|maintenance|reclassify`、`/api/stickers/{id}/image|approve|reject`
- `GET /api/social/status`、`/api/qzone/history`、`POST /api/qzone/post`
- `GET /api/events`（SSE 审计流）、`GET /api/memory/health`、`GET/POST /api/agent/tasks`、`/api/agent/audit`、`POST /api/agent/tasks/{id}/cancel`
- `GET /api/x-browser/stats`
- `GET /api/auth/login|logout`、`POST /api/webui/change-token`

---

## Owner QQ 命令

只有配置为 owner 的 QQ 号可用 `!anima` 命令。常用：

```text
!anima status | pause | resume
!anima skills | abilities | skills reload
!anima draft create 帮我学习一个只读公开网页状态查询能力
!anima drafts | draft preview <ID> | draft approve <ID>
!anima subscribe bili <直播间号> <群号> | subscriptions | unsubscribe <ID>
!anima remind <群号> <分钟> <内容>
!anima calendar daily|weekly ... | calendar list | calendar cancel <ID>
!anima approvals | approve <ID> | deny <ID>
!anima os 查看健康           # 受限自然语言 OS 指令（确定性规则，无 LLM）
!anima os 近 30 天稳不稳｜观察环境趋势｜演化影响评分｜依赖自检｜列出演化提案
!anima os 你还记得我的习惯吗  # 长期记忆查询（语义召回需 memory.long_term_semantic）
```

`!anima os` 的三条边界（P3-3，均 fail-closed）：解析器不认识的输入只回用法、不执行；
**特权意图（批准/拒绝提案）只返回待确认动作 + 既有 Owner 授权端点，QQ 侧不执行**；
只跑只读白名单（落草案的 `skill`、多轮 LLM 的 `society` 请在 WebUI 控制面走）。

---

## 配置

模板在 [config/default.yaml](config/default.yaml)；生产配置 `/opt/anima/config.yaml`（部署默认不覆盖）。配置 schema 见 [schemas/config.schema.yaml](schemas/config.schema.yaml)。`AnimaConfig` 顶层节（`anima/core/config.py`，共 45 个配置 Model）：

- **qq**：NapCat WS/HTTP、self_id、access_token、并发与背压。
- **llm**：endpoint/api_key/model、embedding、timeout/retries、fallback_models、circuit、providers、active_provider、module_models（模块级模型路由）。
- **vector**：向量库路径、embedding/reranker 后端。
- **autonomy**：心跳、冷却、每日上限、主动发言概率/沉默门控/全局冷却、quiet_hours、睡眠。
- **drift**：人格漂移上限、自校正预算、7 日窗口、相似度与价态下限。
- **permissions**：owners/admins/readonly/banned + 只读/静默群 + 群策略。
- **persistence**：data_dir、use_sqlite。
- **memory / compat / crossgroup / tool_learning / capabilities / capability_layer / notes_compact / rumination / sticker / bilibili_read / self_code / vision / image_generation / audio / sylann / skills / conversation / reply_split / debounce / soul / gate / qzone / qq_status / voice / meme / scrapling / tavily / context / exploration / x_browser / webui / security / contradiction / danger / deploy / iteration / social / persona**。

环境变量：`ANIMA_LLM_API_KEY`、`ANIMA_LLM_ENDPOINT`；生产部署用 `ANIMA_DEPLOY_HOST/USER/PASS`；外置密钥经 systemd `EnvironmentFile=/opt/anima/.secrets`。

人格在 [config/persona_core.yaml](config/persona_core.yaml) 调整。不要在 prompt/文档写入真实密钥。

---

## 开发与测试

```bash
# 安装（推荐 .venv）
pip install -e ".[qq,vector,dev,sylann,docs]"   # 或 make install
make test          # pytest tests/ -q
make test-verbose  # pytest tests/ -v
make doctor        # python -m anima doctor
make run           # python -m anima start --config config.yaml
make ci            # 本地复跑 CI（pytest + guard + evidence）

# 单独测试
pytest tests/test_xxx.py::test_func_name

# WebUI 前端
cd webui && npm install && npm run build    # 产出 anima/webui/static

# 生成配置 / 自检 / 启动
python -m anima init --agent --spec spec.json
python -m anima doctor
python -m anima start --config config.yaml
python -m anima ops-cleanup                 # 清理僵尸 SelfCode / 粘滞笔记
python -m anima x-diag                      # X 搬图模块诊断
```

- Python `3.12+`；`tests/` 平铺 420+ 个 `test_*.py`，覆盖核心/认知/治理/演化/WebUI/安全/试点等（现有若干 `.pytest-*` scratch 目录为缓存，勿并入版本控制）。
- 测试不会读写生产 `data/`；以全量绿为准。

---

## CI/CD

`.github/workflows/anima-ci.yml`（push/PR 到 `master` 与 `anima-autonomous-evo`），四 job：

1. **backend**：`pip install -e ".[qq,vector,dev,docs,sylann]"` + `pytest tests/ -q --cov`。
2. **frontend**：`webui/` 下 `npm ci && npm run build`。
3. **security**：`scripts/anima_guard.py --base origin/master` 检测是否触碰 `CORE_PROTECTED`/敏感路径 → 标记 `requires_owner_review`（需 Owner 审查，不阻断）。
4. **evidence**：`scripts/ci_evidence.py` 聚合三阶段 → `ci-report.json` + `commit-attestation.json`。

**安全纪律**：CI 只有读取与验证逻辑，绝不持有生产部署凭证，绝不自动批准 Evolution，绝不自动部署/改码/跳过失败。

---

## 部署

部署脚本 `_deploy.py`（paramiko SSH）同步 Git 已跟踪文件到 VPS `/opt/anima`，保留生产 `config.yaml` 与 `data/`，生成不含密钥/运行数据的源码归档，装依赖、刷新 systemd、重启、smoke。

```powershell
$env:ANIMA_DEPLOY_HOST = 'your-vps-host'
$env:ANIMA_DEPLOY_USER = 'root'
$env:ANIMA_DEPLOY_PASS = 'your-password'
python _deploy.py --dry-run   # 仅预检
python _deploy.py             # 正式部署
# --with-config 上传本地 config.yaml（默认保留生产配置）
# --allow-dirty 允许脏工作区（紧急恢复专用）
```

部署流程：预检 → 源码归档（zip + release-manifest.json 逐文件 sha256）→ SFTP 上传 → 快照并恢复 `data/` → `pip install -e` → 渲染 systemd 单元（注入 `EnvironmentFile=/opt/anima/.secrets`）→ `systemctl restart anima` → 核对 MainPID/ActiveEnterTimestamp → smoke（/api/health · modules · evaluation · runtime_status）→ 写 `version.json` → `doctor`。

部署后必须确认：

```bash
systemctl is-active anima
systemctl show --property=ActiveEnterTimestamp anima
journalctl -u anima -n 100 --no-pager
curl -H "Authorization: Bearer <token>" http://127.0.0.1:8088/api/health
```

> **容器化替代路径**（可选）：`Dockerfile` + `docker-compose.yml` 提供 Anima + NapCat 同机容器部署（见 [deploy/README.md](deploy/README.md)）。生产当前用裸 systemd。

---

## 数据与安全边界

- 运行时数据在 `/opt/anima/data/`（记忆、审计、订阅、Skill 草案、表情资产、SelfCode、向量库、健康证明、稳定性、演化证据等）。
- `data/`、生产 `config.yaml`、`.secrets`、密钥、用户数据不得提交 Git。
- 权限模型：QQ 号 + 群号分级；工具二次校验；危险工具审批；审计 JSONL；沙箱；失败隔离；Fail Closed。
- 自建 Skill 只支持受限声明式 prompt / 固定公开 HTTPS 查询；不允许 bot 自行执行任意代码或自行部署。
- SelfCode 只写 `data/selfcode/` 运行时工具，不改核心源码；安装/删除/禁用需 Owner；`auto_install` 恒不自动装。
- **自改进 ≠ 自我授权**：任何自动演化只生成提案，落地均需 Owner 审批。

---

## 自治与自我进化

五条安全铁律（贯穿所有阶段）：

> **Fail Closed · No Hidden Authority · Identity Cannot Be Forged · Audit Everything · Production Is Sacred**

- **R6–R15**：权限边界 / 治理加固 / RBAC / 执行隔离 / 行为证据 / 多 Agent 编排 / 自治开发闭环 / 受控自我进化 / CI+观测+评估闭环 / Agent Intelligence / Multi-Agent Society / Autonomous Operation / Human-AI OS / ANIMA OS 终验。
- **P0–P6**：每日健康证明 / 长期运行稳定性 / 真实环境观测 / 演化影响评分 / 真实 Agent 社会 / 长期记忆 / 自治维护 / 个人能力生态。
- 详细演化历史见 [ROADMAP.md](ROADMAP.md) 与 [CHANGELOG.md](CHANGELOG.md)。

---

## 维护

模块级维护、部署运维、常见故障与回滚见 **[MAINTENANCE.md](MAINTENANCE.md)**（含各模块/子模块职责、数据文件、运维命令、监控、备份/恢复与故障处置）。另有：
- [DEPLOYMENT_OPERATOR_GUIDE.md](DEPLOYMENT_OPERATOR_GUIDE.md)：部署运维操作指南（接管前检查/启停/健康检查/回滚/禁止自动修复）。
- [RELEASE_POLICY.md](RELEASE_POLICY.md)：发布门禁与回滚纪律。
- [PROJECT_STATE.md](PROJECT_STATE.md)：当前状态快照与增量。
- [AGENTS.md](AGENTS.md)：开发协作总纲（**历史快照，停更于 2026-07-23**，测试基线/环境变量等描述已过时；现行入口以本文件与 [MAINTENANCE.md](MAINTENANCE.md) 为准）。

---

## 许可证与对应源码

Anima-next 按 [`AGPL-3.0-only`](LICENSE) 发布。通过网络使用 WebUI 的用户可在公开 `/source` 页面取得与当前部署版本对应的源码归档（由部署脚本按**白名单**从发布清单生成：仅含 `anima/`、`webui/`、`schemas/` 与构建/许可文件；不含 `config.yaml`、`config/persona_core.yaml`（含主人个人数据）、`.env`、`data/`、密钥/Token、测试、运维文档、虚拟环境/缓存/日志）。修改后向网络用户提供服务时，应继续保留 Source 入口并提供修改版本的完整对应源码。许可证正文见 [LICENSE](LICENSE)。
