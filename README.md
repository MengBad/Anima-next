# Anima-next

Anima-next 是一个独立运行的 QQ AgentBot。它不依赖 AstrBot：通过 OneBot11 / NapCat 接收和发送 QQ 消息，使用 OpenAI 兼容的 LLM 进行对话、工具调用与媒体理解，并拥有独立的记忆、时间、关系、心跳、日历、审计和权限系统。

当前发布候选版本为 **0.3.0rc2**。项目整体按 **GNU Affero General Public License v3.0 only（AGPL-3.0-only）** 发布，与 `sylanne-core==2.5.0` 的生产集成路径保持一致。

生产部署目录为 VPS 的 `/opt/anima`；本仓库是唯一生产源码。历史 AstrBot 插件项目 `G:/Anima` 已冻结，仅作参考，不应继续开发新功能。

## 当前能力

- QQ：OneBot11 WebSocket 入站与出站、私聊/群聊、@ 检测、去重、引用消息取图、图片与表情采集。
- 人格与认知：外置哈基米人格、单一 system 消息护栏、时间感、欲望、关系、世界观、遗忘、反刍、伤痕和群氛围。
- 记忆：按“群号 + QQ 号”隔离的对话记忆、向量召回、跨群事实记忆；不会把整群对话混成同一个会话。
- 主动性：受昼夜、冷却、频率和预算约束的心跳；Agenda 一次提醒、每日/每周日历均通过同一受控发送链路执行。
- 媒体：图片/表情语义、引用图、附件文本提取、受预算的 LLM 生图；语音转写为可选能力。
- 表情系统：群图片自动收集、LLM/VLM 入库判断、情绪/场景/标签分类、Owner 审核池、语义选图、群作用域、黑白名单和发送冷却；模型故障时降级为本地语义匹配。
- 自扩展：LLM 可为 owner 生成声明式 SkillDraft；可从 QQ 或 WebUI 用自然语言描述需求。HTTP Skill 必须预览，所有安装均需 owner 审核。固定公网 HTTPS 可使用查询字符串参数并按需提取网页正文交给 LLM 摘要，仍禁止改 host/path、登录、Cookie、重定向和写操作。内置 `compose_notice` 可把事实通知整理为自然 QQ 文本。
- 验收与恢复：WebUI 可生成不含聊天正文、图片、向量库、配置或密钥的运行状态快照；隔离行为回放会复用真实消息 Pipeline，验证私聊回复、群聊克制和人格身份护栏，但不连接真实 QQ 或生产 LLM。
- 联网与订阅：公开网页状态监控、B 站直播间开播通知。owner 可自然语言提出需求；持续订阅会进入审批流程。
- 安全：QQ 号 + 群号权限、工具二次校验、危险工具审批、审计 JSONL、沙箱和失败隔离。
- SylannEngine：已完整接入 `sylanne-core 2.5.0` 的语义评估、可选 embedding、会话状态、Surface、伤害影响注入和心跳 tick；生产当前为低权重 `active`。它只提供粗粒度语气建议，不直接发送 QQ 消息、读写聊天记忆、调用工具或覆盖人格与权限护栏。仍记录匿名化观测数、错误率、延迟和引擎去重统计。

## 系统结构

```text
QQ / NapCat
  → OneBot11 WebSocket（事件入站 + action/echo 出站）
  → Gateway / EventBus
  → LLMHandler
  → Permission → Memory → Cognition → Generation → Guardrail
  → NapCat WS 出站

认知与运行时：Memory / Relations / Time / Agenda / Heartbeat / Sylann active
行动能力：ReAct Tools / Skills / Monitors / Image generation
控制与观察：Audit JSONL / WebUI / owner QQ commands
```

## WebUI

当前生产配置监听 `0.0.0.0:8088`，公网入口为：

```text
http://pve.aicaowo.com:8088
```

Token 认证已启用。若以后迁移 VPS，需要同时在云防火墙 / 安全组中放行入站 **TCP 8088**。

WebUI 是一个 Vue 3 + TypeScript 单一生命控制台，按六个任务导向区域组织能力：

| 页面 | 入口 | 用途 |
|---|---|---|
| 生命总览 | `/` | QQ、LLM、NapCat、Sylann、心跳和看门狗健康；待办、活动与用量摘要 |
| 对话与记忆 | `/memory` | 连续性、群体感知、自我笔记摘要和脱敏时间线 |
| 认知状态 | `/cognitive` | 身份稳定度、欲望、关系、矛盾、伤痕与 Sylann 状态 |
| 能力与自动化 | `/abilities` | Skills、订阅、议程、日历与表情资产 |
| 审批与工程 | `/engineering` | 工程作用、影响、diff/测试证据、回滚点与 Owner 审批 |
| 设置与权限 | `/settings` | LLM 快速配置、模块模型路由、Token 统计、行为策略和权限 |
| 表情资产工作室 | `/stickers` | 直接打开能力区的表情审核池、图库、导入、搜索排序、批量管理与 LLM/VLM 重审 |
| 登录 | `/login` | 写入安全 Cookie；密钥和原始配置不会回显 |
| 对应源码 | `/source` | 许可证、运行版本、源码归档与校验信息；无需登录 |

主控制台的各区域独立加载：某个非核心接口失败时只降级对应卡片，不会让整页停在“读取状态中”。页面支持系统“减少动态效果”设置；窄屏设置页的模型路由保留可横向滚动的表格，不会挤压其它字段。

如不希望直接暴露端口，仍可使用 SSH 隧道在本机访问：

```bash
ssh -L 8088:127.0.0.1:8088 root@pve.aicaowo.com
```

然后访问：

```text
http://127.0.0.1:8088/
```

### 如何获取 WebUI Token

Token 不会写入本 README，也不应发送到群聊、截图或提交到 Git。它保存在 VPS 的生产配置 `/opt/anima/config.yaml` 中的 `webui.token`。

登录 VPS 后仅查看该字段：

```bash
grep -A4 '^webui:' /opt/anima/config.yaml
```

如果配置了 Token，可任选一种方式认证：

```text
http://127.0.0.1:8088/?token=<你的-token>
```

首次通过这个地址打开后，浏览器会得到 HttpOnly Cookie；之后同一浏览器不必在 URL 中重复携带 Token。调用 API 时使用：

```bash
curl -H 'Authorization: Bearer <你的-token>' http://127.0.0.1:8088/api/health
```

若 `webui.token` 为空，观察接口可以使用，但所有控制接口会被拒绝。应在 `/opt/anima/config.yaml` 设置一个随机长 Token 后重启服务。公网部署使用：

```yaml
webui:
  enabled: true
  host: "0.0.0.0"
  port: 8088
  token: "请替换为至少 32 位随机字符串"
```

```bash
systemctl restart anima
systemctl show --property=ActiveEnterTimestamp anima
```

公网访问时必须保留 Token。更推荐后续使用 HTTPS 反向代理与额外访问控制，避免在不受信任网络中传输 Token。

## WebUI API

所有 `/api/*` 路径均可用于观察；控制类接口还要求配置 Token 和 owner。

仪表盘页面已覆盖自主开关、议程/日历/订阅的创建与取消、SkillDraft 的预览/批准/拒绝，以及已安装声明式 Skill 的 owner 执行；不需要手工拼接 API 请求。

| API | 用途 |
|---|---|
| `GET /api/health` | 服务、NapCat 实时 WS、出站回执、LLM、心跳、Sylann 健康状态 |
| `GET /api/health/history` | 最近 24 小时/7 天的脱敏健康趋势样本；包括心跳、QQ、LLM、Sylann、语音、订阅和 SkillDraft 聚合状态，不含聊天正文、群号或提示词 |
| `GET /api/modules/status` | 23 个运行模块的语义状态和非敏感指标 |
| `GET /api/subject` / `GET /api/life` | 时间、欲望、关系摘要和生活轨迹 |
| `GET /api/timeline` | 最近审计事件 |
| `GET /api/skills` / `GET /api/skill-drafts` | 已加载 Skill 与待审核草案 |
| `POST /api/skills/{skill_id}/execute` | owner 执行已加载的声明式 Skill |
| `POST /api/skill-drafts` | 以自然语言需求创建待审核 SkillDraft |
| `GET /api/approvals` | 待审批危险操作 |
| `GET/POST/DELETE /api/agenda` | 一次性主动议程 |
| `GET /api/calendar` | 每日与每周日历 |
| `POST /api/calendar/daily` / `POST /api/calendar/weekly` | 创建日历事件 |
| `GET/POST/DELETE /api/subscriptions` | B 站直播与网页监控订阅 |
| `GET /api/stickers` | 表情资产统计 |
| `GET /api/stickers/assets` | 审核池/图库搜索、分类、作用域和排序数据 |
| `GET/POST /api/stickers/policy` | 读取或热更新收集、审核、检索和发送策略 |
| `POST /api/stickers/import` | Owner 批量导入并进入 AI 审核流程 |
| `POST /api/stickers/batch` | 批量批准、删除、改分类或改作用域 |
| `POST /api/stickers/maintenance` | 清理失效索引并重新核对资产 |
| `POST /api/stickers/reclassify` | Owner 显式触发全库 LLM/VLM 重审 |
| `GET/POST /api/tasks` | 工程任务与待审批部署证据 |
| `GET /api/events` | Server-Sent Events 审计流 |
| `GET /api/config/manifest` / `GET /api/config/state` | 设置字段说明与脱敏后的当前状态 |
| `POST /api/config/validate` | 只校验配置，不保存 |
| `POST /api/config/change-sets` | Owner 创建配置变更预览 |
| `POST /api/config/change-sets/{id}/apply` / `rollback` / `reject` | 应用、回滚或驳回配置，必要时受控重启；驳回不改运行配置 |
| `GET /api/llm/providers` / `POST /api/llm/providers/test` | 查看提供商状态、测试 `/models` 连通性 |
| `GET /api/llm/routes` | 查看可独立切换模型的模块路由与当日用量 |
| `GET /api/llm/usage` / `events` / `export` | Token、延迟、失败、模型/模块排行与导出 |

LLM 路由按功能隔离，而不是把所有后台调用记成主聊天。当前可独立配置并统计的模块包括：主聊天、主动发言、语音转写、视觉理解、表情审核/选图、生图、Sylann 评估、反刍与生活模拟、跨群事实、个人能力、工具学习、Skill 生成/执行、B 站总结、工程代理、笔记压缩、Embedding 和 Rerank。账本只保存时间、模块、模型、Token、延迟和成功状态，不保存 prompt 或聊天正文。

表情的策略修改独立持久化到运行数据目录，不会改写包含 API Key 的主配置。新收集素材默认先进入审核池，Owner 批准前绝不会被发送。

## Owner QQ 命令

只有配置为 owner 的 QQ 号可使用 `!anima` 命令。常用示例：

```text
!anima status
!anima pause
!anima resume
!anima skills
!anima skills reload

!anima draft create 帮我学习一个只读公开网页状态查询能力
!anima drafts
!anima draft preview <草案ID>
!anima draft approve <草案ID>

!anima subscribe bili <直播间号> <目标群号>
!anima subscriptions
!anima unsubscribe <订阅ID>

!anima remind <群号> <分钟> <提醒内容>
!anima calendar daily <群号> <HH:MM> <提醒内容>
!anima calendar weekly <群号> <星期1-7> <HH:MM> <提醒内容>
!anima calendar list
!anima calendar cancel <事件ID>

!anima approvals
!anima approve <审批ID>
!anima deny <审批ID>
```

自然语言提出“某直播间开播时通知到某群”时，LLM 也可以调用受控订阅工具；若只提供 UP 主昵称而没有直播间号或链接，bot 会先追问，避免订阅错目标。

## 配置

配置模板在 [config/default.yaml](config/default.yaml)。生产配置是 `/opt/anima/config.yaml`，部署时默认不会覆盖它。

重点配置：

```yaml
llm:
  endpoint: "https://example.com/v1"
  api_key: "仅保存在生产配置或环境变量"
  model: "gemini-3.5-flash-low"

qq:
  napcat_ws_url: "ws://127.0.0.1:3001"
  # NapCat 正向 WS 同时承载事件与 action/echo；只有另开 HTTP API 时才配置下项
  # napcat_http_url: "http://127.0.0.1:<NapCat-HTTP端口>"

permissions:
  owners: [你的QQ号]

webui:
  enabled: true
  host: "0.0.0.0"
  port: 8088
  token: "随机长Token"

sylann:
  enabled: true
  mode: active  # 仅注入粗粒度表达提示；可随时切回 shadow
  engine_mode: lite
  assessor_enabled: true
  pel_core_enabled: true
  tick_sessions_limit: 32
```

人格在 [config/persona_core.yaml](config/persona_core.yaml) 调整。不要在 prompt 或公开文档中写入真实 API Key、QQ Token 或 WebUI Token。

## 开发与测试

```bash
pip install -e ".[qq,vector,dev,sylann,docs]"
pytest tests/ -q
python -m anima doctor
python -m anima start --config config.yaml
```

当前本地基线为 **455 项测试通过**。测试不会读取或写入生产 `data/`。

## 部署

部署脚本只同步 Git 已跟踪文件，保留生产 `config.yaml` 与 `data/`，生成不含密钥和运行数据的对应源码包，然后安装依赖并重启 systemd。默认拒绝脏工作树；只有明确使用 `--allow-dirty` 才能执行破窗部署：

```powershell
$env:ANIMA_DEPLOY_HOST = 'your-vps-host'
$env:ANIMA_DEPLOY_USER = 'root'
$env:ANIMA_DEPLOY_PASS = 'your-password'
.\.venv\Scripts\python.exe _deploy.py
```

部署后必须确认：

```bash
systemctl is-active anima
systemctl show --property=ActiveEnterTimestamp anima
journalctl -u anima -n 100 --no-pager
```

## 数据与安全边界

- 运行时数据在 `/opt/anima/data/`，包括记忆、审计、订阅、Skill 草案、表情与 Sylann shadow 指标。
- 表情资产与索引也在运行时数据目录；部署脚本不会覆盖它们，并会在部署前滚动备份表情元数据。
- `data/`、生产 `config.yaml`、密钥和用户数据均不得提交 Git。
- 自建 Skill 只支持受限声明式 prompt / 固定公开 HTTPS 查询（可选查询参数与内容摘要）；不允许 bot 自行执行任意代码或自行部署。
- Sylann 不是“人格自主控制”；即使处于 active，也只能给出低分辨率表达建议，不能绕过人格、权限、审计或审批。

## 许可证与对应源码

Anima-next 按 [`AGPL-3.0-only`](LICENSE) 发布。通过网络使用 WebUI 的用户可在公开的 `/source` 页面取得与当前部署版本对应的源码归档；归档由部署脚本从发布清单生成，不包含：

- `/opt/anima/config.yaml`、`.env` 或任何 API Key / Token；
- `/opt/anima/data/` 中的 QQ 消息、记忆、审计、表情与用量数据库；
- 本地虚拟环境、缓存、日志和临时诊断文件。

修改后向网络用户提供服务时，应继续保留 Source 入口并提供修改版本的完整对应源码。许可证正文见 [LICENSE](LICENSE)，GNU 官方说明见 [GNU AGPL v3](https://www.gnu.org/licenses/agpl-3.0.html)。

## 当前上线验收

2026-07-16 的测试 VPS 验收包括：

- 449 项自动化测试全部通过；
- 公网 8088 的登录、生命控制台、深层认知和表情工作室均由真实浏览器验证；
- 24 个运行模块没有降级项；
- 347 项生产表情索引与图片文件逐一核对，无失效图片；
- Owner 授权的真实群议程完成 `Agenda → LLM → Gateway → NapCat WS action/echo → QQ`，NapCat 返回真实 `message_id`；
- 最终服务实例连续探测超过 10 分钟，systemd 重启数为 0、看门狗持续刷新、严重日志为 0。一次外部探测连接短暂失败，但前后实例时间戳未变化且窗口日志无异常，判定为客户端链路抖动。
