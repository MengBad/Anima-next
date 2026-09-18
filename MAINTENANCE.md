# Anima-next 维护文档（MAINTENANCE）

> 面向维护/运维/接管者。本文档细分到各模块/子模块，并给出部署、运维、监控、备份、恢复与故障处置的完整操作路径。
> 主线参考：[README.md](README.md)、[DEPLOYMENT_OPERATOR_GUIDE.md](DEPLOYMENT_OPERATOR_GUIDE.md)、[RELEASE_POLICY.md](RELEASE_POLICY.md)、[PROJECT_STATE.md](PROJECT_STATE.md)、[AGENTS.md](AGENTS.md)。

**关键事实**（以源码为准，勿信过时快照）：
- 版本 `0.3.0rc2`（`anima/__init__.py` 是唯一部署版本源；pyproject 亦写 0.3.0rc2）。
- 生产 = VPS `/opt/anima` 裸 systemd 进程，经 `_deploy.py` 发布；Docker（Dockerfile/docker-compose）为可选的容器化替代路径。
- `anima/` 下 **23 个顶层包**（含 `__init__.py`）+ `util/` 命名空间目录、约 400 个 `.py`；`tests/` 平铺 420 个 `test_*.py`。
- **很多模块是「库级 / 试点 / 测试层」**，未接生产 `cli/startup` 主链路——维护前先确认某功能是否真的上线（见 §3 接线清单）。
- 🔴 **配置有三个不同的真源，互不等价**，禁止互相替代：
  | 文件 | 角色 | 是否生产实配 |
  |---|---|---|
  | `/opt/anima/config.yaml` | **生产真源**（服务器手工件） | ✅ 唯一 |
  | `config/default.yaml` | 仓库**默认值模板**（已过期，缺 7 个段，见台账 D-06） | ❌ |
  | `config/production.example.yaml` | 生产键集的**脱敏模板**（2026-09-18 建立） | ❌（键集等价、值脱敏） |
  ⇒ **任何"开关状态"结论必须读生产文件**，读代码默认值或读文档都会得出错误结论（这正是审查 P0-2 的成因）。
  漂移检查：`python deploy/check_config_drift.py`（只读；未登记键应为 0）。
- 🔴 **配置变更没有可查台账**：生产无 `data/config_changes.json`、无 `data/config_backups/`。
  `ConfigDriftMonitor`（`anima/core/config_drift.py`）虽然已接线
  （`anima/core/anima_core.py:109` 建基线 + `anima/core/autonomy.py:725` 每 tick 自检），
  但**基线是每次开机时从同一份配置快照的** ⇒ **结构上无法发现"停机改配置再启动"这类翻转**。
  史上 `config_permission_drift` 审计事件 **0 条**（2026-09-18 实测）。跨重启的变更只有走
  `config_service`（WebUI / change-set）才会留下台账。
- 🔴 **生产 `config.yaml` 含 6 处明文密钥**：`llm.api_key` · `vector.embedding_api_key` ·
  `vector.rerank_api_key` · `webui.token` · `tavily.api_key` · `x_browser.password`。
  前 5 类中已有 5 个支持环境变量注入（见 `anima/core/config.py:1019-1032`）；
  **`webui.token` 与 `x_browser.password` 无 env 通道**，需代码改动（台账 D-07）。

---

## 目录

1. [运维模型总览](#1-运维模型总览)
2. [部署](#2-部署)
3. [生产接线清单（哪些真上线）](#3-生产接线清单哪些真上线)
4. [模块级维护指南](#4-模块级维护指南)
   - 4.1 核心运行时 `anima/core/`
   - 4.2 LLM 层 `anima/llm/`
   - 4.3 QQ 渠道 `anima/channels/`
   - 4.4 消息流水线 `anima/pipeline/`
   - 4.5 认知内核 `anima/cognitive/`（含 inner_state）
   - 4.6 记忆 `anima/memory/` 与跨群 `anima/crossgroup/`
   - 4.7 工具/沙箱/Skills `anima/tools/` `anima/skills/`
   - 4.8 自治/演进/OS `anima/agent/` `anima/evolution/` `anima/observation/` `anima/os/` `anima/monitors/`
   - 4.9 治理/契约/宪法 `anima/capability/` `anima/contracts/` `anima/constitution/`
   - 4.10 运行时恢复 `anima/runtime/`
   - 4.11 WebUI 后端 `anima/webui/`
   - 4.12 前端 `webui/`
   - 4.13 独立 Console `web/`
   - 4.14 兼容层/插件 `anima/compat/` `anima/plugins/`
   - 4.15 媒体 `anima/media*.py`
   - 4.16 试点层 `anima/pilot/`
5. [数据文件清单](#5-数据文件清单)
6. [配置维护](#6-配置维护)
7. [监控与健康检查](#7-监控与健康检查)
8. [日志](#8-日志)
9. [备份 / 恢复 / 回滚](#9-备份--恢复--回滚)
10. [常见故障处置](#10-常见故障处置)
11. [CI 与发布纪律](#11-ci-与发布纪律)
12. [维护清单（Checklist）](#12-维护清单checklist)

---

## 1. 运维模型总览

| 项 | 值 |
|---|---|
| 生产目录 | `/opt/anima` |
| 服务 | `anima.service`（systemd，Type=simple，User=anima，WatchdogSec=120） |
| 运行用户 | `anima` |
| Python 环境 | `/opt/anima/venv`（Python 3.12+） |
| 生产配置 | `/opt/anima/config.yaml` |
| 外置密钥 | `/opt/anima/.secrets`（systemd `EnvironmentFile` 注入） |
| 运行数据 | `/opt/anima/data/` |
| WebUI/API | `webui.host:port`（生产通常 `0.0.0.0:8088`） |
| QQ 接入 | NapCat/OneBot11 WS（`qq.napcat_ws_url`，同机 `ws://127.0.0.1:3001`） |
| 发布 | `_deploy.py`（paramiko SSH → `/opt/anima`） |
| 版本证据 | `/opt/anima/version.json`（commit/branch/version/dirty/sha256/deployed_at） |
| 源码入口 | `/opt/anima/public/anima-next-source.zip` + `release-manifest.json` |

安全铁律（贯穿所有运维）：**Fail Closed · No Hidden Authority · Identity Cannot Be Forged · Audit Everything · Production Is Sacred**；**自改进 ≠ 自我授权**。

---

## 2. 部署

### 2.1 前置条件

- 开发/发布机：Python 3.12+，`pip install -e ".[qq,vector,dev,sylann,docs]"`。
- VPS：Linux + systemd + Python 3 + 网络；NapCat 已就绪；LLM/WebUI 凭据已配。
- 环境变量：`ANIMA_DEPLOY_HOST`、`ANIMA_DEPLOY_USER`、`ANIMA_DEPLOY_PASS`。

### 2.2 发布流程

```powershell
# 0) 本地验证
pytest tests/ -q                 # 全量绿
cd webui && npm run build        # WebUI 变更必须重 build → anima/webui/static

# 1) 提交完整、工作区干净（脏工作区默认被 _deploy.py 拒绝）
git add -A && git commit -m "..."

# 2) 预检（不改远程）
python _deploy.py --dry-run

# 3) 正式部署
python _deploy.py
# --with-config  连本地 config.yaml 一起上传（默认保留生产配置，高风险）
# --allow-dirty  允许脏工作区（仅紧急恢复）
```

### 2.3 部署阶段详解

1. **预检**：SSH/python3/venv/待上传文件数。
2. **发布清单**：`git ls-files` 已跟踪文件；排除 `data/ .env .venv *.pyc 密钥 *.log reports/` 等。
3. **源码归档**：`anima-next-{version}.zip` + `release-manifest.json`（逐文件 sha256）→ 上传 `/opt/anima/public/`。归档按**白名单**生成（anima/ + webui/ + schemas/ + 构建/许可文件），不含 persona_core.yaml 等任何隐私数据（2026-09-06 收口）。
4. **数据保护**：先快照 `data/` → `data.deploy-bak`，解包后**强制恢复 `data/`**（生产数据永不被源码覆盖）。
5. **依赖**：`/opt/anima/venv/bin/pip install -e '[qq,vector,dev,sylann,docs]'`；镜像源失败自动以官方 PyPI 重试（2026-09-06 新增，清华镜像拉不到 setuptools>=77 的实测容灾）。
6. **systemd**：渲染写入 `/etc/systemd/system/anima.service`（含 `EnvironmentFile=-/opt/anima/.secrets`）→ `daemon-reload`。
7. **重启**：`systemctl restart anima`；核对 MainPID + ActiveEnterTimestampMonotonic **确实变化**。
8. **Smoke**：`/api/health`（running/heartbeat_running/channel_connected）、`/api/modules/status` 非空、`/api/evaluation`、固定工具 runtime_status（内存/磁盘/负载）。
9. **version.json**：写发布身份。
10. **doctor 自检**。

### 2.4 部署后确认

```bash
systemctl is-active anima
systemctl show --property=ActiveEnterTimestamp anima
journalctl -u anima -n 100 --no-pager
curl -H "Authorization: Bearer <token>" http://127.0.0.1:8088/api/health
curl -H "Authorization: Bearer <token>" http://127.0.0.1:8088/api/modules/status
```

正常重启必须确认 `ActiveEnterTimestamp` 已刷新、服务仍 `active`、日志无新增持续性 warning/error。

### 2.5 容器化替代路径（可选）

`Dockerfile` + `docker-compose.yml` 提供 Anima + NapCat 同机容器（见 [deploy/README.md](deploy/README.md)）。生产当前用裸 systemd；容器路径需自行管理 `data/` volume 与 config mount。

---

## 3. 生产接线清单（WIRED vs 库级 OFF）

> **改任何东西之前先查这张表** —— 它决定「改完到底会不会影响线上」。

三档语义，不要混用：

| 档位 | 含义 | 判定依据 |
|---|---|---|
| **WIRED** | 代码无条件接线，改它 = 改生产行为 | 在 `cli.py` / `AnimaCore.start` / `create_default_registry(core)` 调用链上 |
| **WIRED · 默认 OFF** | 接线完整、有真实入口，仅配置默认关闭；置 `true` 即生效 | 有生产调用点 + 默认 `false` |
| **LIBRARY** | 代码与测试齐全，但**没有任何生产入口** | import 图里只被自身/同簇模块引用 |

⚠️ 表中「默认」= **代码默认值**（Pydantic 默认 + `config/default.yaml`）。
生产真实值以 VPS `/opt/anima/config.yaml` 为准 —— `_deploy.py` 默认**不覆盖**生产配置。
核实手段：`python -m anima doctor`、WebUI 配置页、`journalctl -u anima` 启动日志。

> 本表依据（2026-09-15）：`AnimaConfig` 反射枚举 **392 字段 / 42 个默认 `False`** + 全树 import 图扫描。
> 新增开关后必须同步本节，否则这张表会立刻开始骗人。

### 3.1 WIRED（无条件接线）

| 组件 | 入口 | 备注 |
|---|---|---|
| `channels.onebot11.OneBot11Adapter` | `AnimaCore` 在 `qq.enabled` 时绑定 | WS 入站 + HTTP 出站；`_call_action`/`get_group_*` 供工具同步取数 |
| `llm.LLMClient` + `llm.handler.LLMHandler` | 配了 LLM 即接线 | 重试 / 熔断 / `fallback_models` / 413 再编码 |
| `tools.create_default_registry(core)` | `cli.py:385` | **唯一**工具注册入口；`core` 必传（不传则 core 依赖类工具整体缺席） |
| Skills / SelfCode / 记忆 / Heartbeat | `AnimaCore.start` | Heartbeat 另受 `autonomy.enabled` 约束 |
| `runtime.recovery_module` → `bootstrap.recover_all` → `recovery.recover_run` + `persistence.WorkflowRunStore` | `AnimaCore.start` | `data/workflow_runs/*.json` |
| `core.paths.set_data_dir` | `anima_core.py:499`（**唯一接线点**） | 其余 `Paths` / `child()` 未用（见 §3.3） |
| `core` 下绝大多数子系统 | gateway / event_bus / audit / persistence / health_history / approvals / backup / evaluation / config_service / config_drift / intents / workflows / tasks / permissions | — |
| `capability.*` 受治理层 | `tools/__init__.py`（resolver / governance.gateway / health / audit sink / ops.observer / lifecycle） | 代码已接线，但**模式默认 OFF** → 走 legacy 路径（见 §3.2） |
| `tools/builtin/reminder.py` | `create_default_registry(core)`（P2-2） | 复用 `cognitive.debt` 债账本，不另起存储 |
| `tools/builtin/honor.py` | `create_default_registry(core)`（P2-4） | P2-4 修复：改走真实 OneBot 通道（原实现调不存在的 `get_gateway`，恒失效） |

**默认 ON 的能力开关（关掉就是主动退化，不是"更安全"）**

| 配置键 | 默认 | 作用 |
|---|---|---|
| `react.memory_first` | `true` | P0-1 先查记忆再动手 |
| `react.freshness_footer` | `true` | P2-3 时效工具用过后，答案缺日期时补一句时效披露 |
| `debt_tracking.enabled` | `true` | P1-1 承诺追踪（`keyword_only_mode: true`，零 LLM 成本） |
| `task_agent.verify_steps` / `parallel_steps` / `max_replans` | `true` / `true` / `2` | P2-1 步骤验收 / 并行批次 / 重规划上限（`max_replans: 0` 退回"一失败即终止"） |
| `owner_browser.enabled` | `true`（`private_only: true`） | P1-3 Owner 受控浏览器（群聊不暴露） |
| `proactive.enabled` | `true` | P1-4 主动优先级队列（债/订阅异常/健康 > 闲聊） |
| `iteration.enabled` / `auto_restart` | `true` | 故障扫描与工单 / 补丁 apply 后重启服务（`auto_repair` 另见 §3.2） |
| `social.bot_social_enabled` | `true` | 识别其他 Bot + 防循环（发现/搭话/私聊三个子开关默认 OFF） |

### 3.2 WIRED · 默认 OFF（需在 `/opt/anima/config.yaml` 显式开启）

**a) 认知 / 自主 / 记忆**

| 配置键 | 默认 | 作用 |
|---|---|---|
| `autonomy.drive_gate` | `false` | Constitution Drive 门：主动表达须源于趋近 Drive 才开口，并把 Drive 写进审计 trace |
| `autonomy.quiet_hours_enabled` | `false` | 免打扰时段 |
| `memory.unified_recall_enabled` | `false` | 收敛计划 M2 统一检索聚合层（只读聚合 chat/vector/facts/episodes）；关 = 回退各域独立召回 |
| `conversation.natural_feedback_ack` | `false` | §17 安全版反馈自然回话（仅私聊 + 已有主回复时追加轻确认，不双发） |
| `contradiction.enabled` | `false` | 矛盾检测（仅在向量命中强相关旧记忆时才调 LLM） |

**b) 能力治理（代码默认全 OFF；⚠️ 生产实配已开启其中四项）**

> 🔴 **本表第 3 列是 2026-09-18 对生产 `/opt/anima/config.yaml` 的实测值，不是推测。**
> 此前本表只有"默认"一列，读起来像"生产也关着"，而这正是「治理文档说关、生产开着」
> 之所以能长期不被发现的直接原因（审查 P0-2/P0-3）。**开关状态结论必须读生产文件。**

| 配置键 | 代码默认 | 生产实配 (2026-09-18) | 作用 |
|---|---|---|---|
| `capability_layer.capability_mode` | `false` | **`true`** | 输出 capability schema（否则 legacy Tool schema）；`false` 时逐字节保持旧行为 |
| `capability_layer.capability_gateway_mode` | `false` | **`true`** | 受治理 Gateway pilot（Line B）；pilot 白名单见 `_GATEWAY_PILOT_CAPABILITIES` |
| `capability_layer.governance_audit_enabled` | `false` | **`true`** | Resolution 审计链 `resolution_events.jsonl`（写入 `<data_dir>/governance/`） |
| `capability_layer.python_exec_enabled` | `false` | **`true`** 🔴 | `python.exec` provider 注册与**可达**（需与上面两个 mode 同时为真 —— 生产三者皆真） |
| `capability_layer.health_evidence_enabled` | `false` | `false` | Health 证据链 `health_evidence.jsonl` |
| `capability_layer.transition_audit_enabled` | `false` | `false` | 生命周期迁移链 `lifecycle_transitions.jsonl` |
| `capability_layer.pilot_capability_allowlist` | `[]` | `[]` | 空 = 拒绝全部 **pilot-only** 路由（fail-closed） |
| `iteration.auto_repair` | `false` | `false` | 自治运维档 3：自动生成补丁并 apply（`iteration.enabled` 本身默认 ON，生产为 `true`；`iteration.auto_restart` 生产亦为 `true`） |

🔴 **关于 `python_exec_enabled` 的易误读点（2026-09-18 实测钉死）**

- 该翻转**经 owner 授权**，代码留痕：`anima/tools/__init__.py:193`
  「R5 PHASE 13 re-open (owner-authorized 2026-08-15)：追加 python.exec 到受治理 pilot 清单」。
- 翻转**未走** `config_service` / `config_recovery` ⇒ 生产无 `data/config_changes.json`、
  无 `data/config_backups/`，**时间点与操作者不能由服务器自证**；只能由最早一条
  `search.web` 路由事件反推上界（≤ 2026-09-05 13:48）。
- ⚠️ **空 `pilot_capability_allowlist` 挡不住 `python.exec`。** pilot allowlist 门只作用于
  `capability_only=True` 的 provider（`anima/tools/__init__.py:977-979`），而 python.exec
  不是（同文件 `:194-195` 明写「不经 pilot allowlist 门，直达 `_governed_capability_route`」）。
  它的真实门是 gateway 内四道 fail-closed 门（kill_switch / grant / audit_sink / isolator）
  + provider 的 **owner-only** 边界；前两门在代码里以 `kill_switch=False, grant=True` 构造
  （`anima/tools/__init__.py:607-609`）。
  ⇒ **结论：owner 主体在生产可通过受治理路径调用 `python.exec`。**
- 实测使用量 = **0**：`resolution_events.jsonl` 的 16 条事件**全为 `search.web`**；
  `python.exec` 在审计与 journal 中 0 命中。

**c) 危险自升级 `danger.*`（默认全 OFF；只生成 WebUI 待审工程任务，**绝不自动安装**）**

`danger.enabled`、`danger.self_improvement(_confirm)`、`danger.core_mutation(_confirm)`、
`danger.stance_propagation(_confirm)`、`danger.memory_infection(_confirm)`、
`danger.identity_crisis`、`danger.autonomous_web`、`danger.info_collection_can_speak` —— 全部 `false`。

**d) QQ 外围 / 可选能力（参考 AstrBot 插件回迁，默认全 OFF）**

| 配置键 | 默认 | 作用 |
|---|---|---|
| `qzone.enabled` | `false` | QQ 空间说说（`daily_max: 2`、冷却 4h） |
| `qq_status.enabled` | `false` | QQ 在线状态同步 |
| `voice.enabled` | `false` | AI 语音 TTS（`auto_probability: 0.1`） |
| `meme.enabled` | `false` | 趣味图片互动（被戳摸头回击概率 0.3） |
| `gate.enabled` | `false` | 轻重双模型前置判断（超时/失败回落完整流程） |
| `x_browser.enabled` | `false` | X（Twitter）受管浏览器（刷时间线 + 对外提供 `x_search`） |
| `scrapling.enabled` | `false` | Scrapling 反反爬网页抓取 |
| `image_generation.enabled` | `false` | LLM 生图（仅 owner；启用前确认网关 `images/generations` 可用） |
| `self_code.enabled` | `false` | SelfCode 双 Agent 产物热注册 |
| `self_code.auto_install` | `false` | 字段仅兼容旧配置；**运行时始终要求 Owner 审批** |
| `sticker.share_across_groups` | `false` | 表情跨群复用（默认仅来源群） |
| `exploration.share_enabled` | `false` | 探索笔记主动分享（先自主学习，Owner 验证后再开）。配套 `share_groups` 非空即为**群白名单**；空=仅沿欲望来源群，绝不广播 |
| `social.bot_discovery_enabled` | `false` | 动态发现群内其他 Bot（昵称启发式，默认关避免误判） |
| `social.bot_talk_enabled` | `false` | 心跳每日最多一次「找其他 Bot 说话」（`rivalry > 0.3`） |
| `social.bot_whisper_enabled` | `false` | Bot 间私聊通道（经 NapCat 私聊，标记 `[Bot内部]`） |
| `compat.astrbot_enabled` | `false` | AstrBot 兼容层（tier 1） |
| `sylann.enabled` / `sylann.mode` | `false` / `disabled` | SylannEngine 适配（`disabled` / `shadow` / `active`） |
| `webui.enabled` | `false`（`host: 127.0.0.1`） | ⚠️ **生产实际为 ON**（8443 HTTPS 控制台 + 8088 native）：这是"代码默认 ≠ 生产值"的典型例子 |

### 3.3 LIBRARY（代码在、生产无入口）

改动这些**不会**影响线上；声称"修好了某问题"前请先确认它到底有没有入口。

| 模块 | 实测 |
|---|---|
| `runtime/resolving_executor.py`、`runtime/bridge_bootstrap.py` | 全树 **0 import** |
| providers `sandbox_write`、`mock_http` | 全树 **0 import** |
| `runtime/startup_recovery.py` + `resume_coordinator.py` + `recovery_marker_store.py` | 仅同簇互引，无生产入口 |
| `runtime/pilot_rollout.py` + `pilot_control.py` + `pilot_scope.py` + `execution_events.py` | 仅同簇互引，无生产入口 |
| `core/exceptions.py` 异常层级（`AnimaError` 家族） | 全树 **0 import** |
| `core/paths.py` 的 `Paths` 类与 `child()` | 仅 `set_data_dir()` 被接线，二者未用 |
| `capability/_archive/*` | 刻意归档的兼容再导出层（`from ... import *`），非活跃代码 |

### 3.4 易踩坑

- ✅ **已修（2026-09-15）** `gateway.get_gateway` —— 该符号**从未定义**（`core/gateway.py` 只有 `Gateway` 类，无全局单例）。曾被两处引用：
  `cognitive/intent_generator.py`（模块级 import → 一 import 即 `ImportError`）与
  `tools/builtin/honor.py`（`try/except` 吞掉 → 工具**注册在表里但 100% 失效**，还把"我们的 bug"说成"群没开这个功能"）。
  现两处均已改为依赖注入 / 真实通道访问，全树零引用；
  `tests/test_p24_honor_wired.py` 用 AST 扫描（剔除注释与字符串后）锁死回归。
- ✅ **已澄清** `memory.hydrate_period_hours` **已声明**于 `MemoryConfig`（`core/config.py:891`，默认 `6.0`）。旧版本文档称"未声明"是**过期信息**，勿再据此改代码。
- ⚠️ `Tool.required` 缺省时 `to_openai_schema()` 会**回退为「所有参数必填」**。参数可省略的工具必须显式传 `required=[]`——`honor.py` 踩过这个坑：描述写「可省略」，schema 却声明必填，模型因此不敢省略参数。
- ⚠️ 默认 `False` 的开关里混着**两种语义**：**安全开关**（`danger.*` / `self_code.*` / `capability_layer.*`，坏掉即 fail-closed）与**能力开关**（`qzone` / `voice` / `meme` / `gate`…，关着只是没这个功能）。调配置时不要把前者当后者顺手打开。
- ✅ **已修（2026-09-15）** `exploration` 的 `0` 语义曾**未定义**：`daily_limit` / `daily_share_limit` 都写成 `getattr(..., N) or N`，而 `0` 是 falsy → 被静默吞成 `N`。结果 `daily_share_limit: 0` 本意"今天不分享"，实际是"每天分享 1 条"——**关不掉的开关**。现两处均已按显式语义实现并写进 `config/default.yaml` 与 schema 描述：
  - `exploration.daily_limit: 0` = **不限制**（与 `image_generation.daily_limit` 同义，属"内部读取配额"）
  - `exploration.daily_share_limit: 0` = **当天完全不分享**（属"外发闸门" → fail-closed，**语义与上面相反，勿统一**）
  回归由 `tests/test_p34_exploration_share.py` 双向锁死（含 RED 验证）。
  注意：`image_generation.daily_limit` 的 `0 = 不限制` 是**既有成文约定**，本次未改动。

### 3.5 术语与命名排查口径（`capability` / `allowlist`）

> 起因：`capability` 有**两个同名不同物**（债 D-15），`allowlist` 有**一张被普遍误读的表**
> （债 D-10）。凡在日志 / 配置 / 代码里看到这两个词，先按下表确认**是哪一层**再下结论。

**① 看到 `capability`，先确认是哪一层**

实测依据：抽查 journal 近 30 天 **1063 行**含 `capability` 的日志，**全部**来自
`anima.tools.capability`（自生长能力层），治理关键字命中 **0** —— 也就是说，日志里
出现的几乎都不是治理面。

| 层 | 形态 | 是什么 | 排查命令 |
|---|---|---|---|
| 治理面 | `capability_layer.*`（config） | 受治理 Gateway / pilot 门（**安全开关**） | `grep -n capability_layer /opt/anima/config.yaml` |
| 自生长能力 | `capabilities.*`（config）+ `anima/tools/capability.py` | bot 自己注册的工具能力 | `journalctl -u anima \| grep anima.tools.capability` |
| 组件目录 | `anima/capability/` | 治理组件（gateway / contracts / health） | `ls /opt/anima/anima/capability/` |

**② 看到 `allowlist`，先确认是哪张表**

| 名字 | 管什么 | 空值含义 |
|---|---|---|
| `capability_layer.pilot_capability_allowlist` | **只管** `capability_only=True` 的 provider | 空 = 拒绝全部 **pilot-only** 路由（**不是**「拒绝一切」） |
| `_GATEWAY_PILOT_CAPABILITIES`（`tools/__init__.py:189+`） | Gateway pilot 的精确豁免集合 | 与上表配合，**不是同一张表** |
| `crossgroup.cross_group_allowlist` | **只**作用于群聊隔离（私聊 `group_id=None` 不适用） | 空 = 不隔离任何群 |
| 发布归档白名单（`_deploy.py`） | 打包哪些目录 | 与权限无关 |

🔴 **易误读点**：`python.exec` **不经** pilot allowlist 门 —— `anima/tools/__init__.py:194-195`
明写它 `capability_only=False`、直达 `_governed_capability_route`。所以**空 allowlist
挡不住它**；它真实的门是 Gateway 四道 fail-closed + provider 的 owner-only 边界。
⇒ 任何「空 allowlist = 全拒」的表述都是错的（债 D-10）。

### 3.6 系统提示词预算阶梯与「片段被丢」排查口径

> 起因：债 D-27 原写「`capabilities` 段**从不进** prompt / 100% 被丢弃」，
> 2026-09-18 用生产落盘计数器**实测证伪**。本节给出"一次就能读到真相"的路径，
> 避免再用「翻 journal 数日志」这种考古法。

**① 三层预算，各有各的口径**

| 层 | 配置键 | 生产实测 | 代码默认 | 被裁对象 |
|---|---|---|---|---|
| system 段 | `context.system_token_budget` | **10000** | 8000 | 可选片段（`PromptFragment`） |
| messages 段 | `context.input_token_budget` | 12000 | 12000 | 历史消息 |
| 工具结果 | `context.tool_result_token_budget` | 1800 | 1800 | 单条工具返回 |

`base`（人格模板）与 `suffix`（身份锁定 / 猫娘口吻 / 话题纪律 / 身份边界）**不可裁**；
`protected=True` 的片段（`inner_state` / `hard_facts`）也**不可裁**。
`required = base + suffix` 一旦超过预算，日志出
`Context system protected content exceeds budget` —— **这才是真事故**。

**② 谁先死：优先级阶梯（值低者先丢）**

裁剪算法（`pipeline/context.py::compose_system`）：按 `(priority, 注入顺序)` **升序**丢，
丢到 `total <= budget` 即停。`capabilities` 的 priority = **20**
（`pipeline/stages/cognition.py:697`），**是全表最低** ⇒
**只要发生任何片段丢弃，它必是第一个被丢的**。相邻梯队：

| priority | key | 说明 |
|---|---|---|
| **20** | `capabilities` | 个人能力片段（自生长能力） |
| 25 / 30 | `external_exploration` / `tool_learning` | 探索笔记 / 工具心得 |
| 40–45 | `identity_crisis` / `sylann_expression` / `milestones` | |
| 50–60 | `group_presence` / `social_graph` / `vector_recall` / `conversation_depth` | |
| 70–100 | `mood` / `relationship` / `episodic_context` / `scar_triggered` | 最高 |

⚠️ 两点纪律：

1. `capabilities` 的"首要被弃"是**优先级设计的必然结果，不是 bug**。
2. **不要用 `grep journal` 反推发生率** —— 那条 INFO 日志**只在真发生丢弃时才打**，
   分母不在日志里。照它计数会得出"100% 被丢"的错误结论（D-27 就是这么写错的）。

**③ 正确读法：查逐日落盘的计数器（比 journal 权威）**

```bash
# 生产机，只读
/opt/anima/venv/bin/python - <<'PY'
import json, pathlib
d = json.loads(pathlib.Path("/opt/anima/data/reply_quality.json").read_text("utf-8"))
for k in sorted(d["days"])[-14:]:
    r = d["days"][k]
    req = r.get("context_requests") or 0
    tot = r.get("context_system_tokens_total") or 0
    print("%s req=%-4d avg_sys=%-6.0f frag_dropped=%-4d hist_dropped=%-4d protected_over=%d" % (
        k, req, (tot / req) if req else 0,
        r.get("context_fragments_dropped") or 0,
        r.get("context_history_dropped") or 0,
        r.get("context_protected_over_budget") or 0))
PY
```

| 字段 | 语义 |
|---|---|
| `context_requests` | **分母**：本日 cognition 运行次数 |
| `context_fragments_dropped` | **分子**：发生片段丢弃的运行数 |
| `context_system_tokens_total ÷ context_requests` | 系统段均值；**逼近 budget 就是顶格信号** |
| `context_history_dropped` | 消息历史被裁条数 |
| `context_protected_over_budget` | **不可裁核心超预算次数**（>0 必须查） |

**④ 2026-09-18 基线（供日后对比）**

| 窗口 | requests | avg 系统段 tokens | 片段丢弃 | 丢弃率 |
|---|---|---|---|---|
| 2026-07-12 .. 08-31 | 1077 | 7816 | 969 | 90.0% |
| 2026-09-01 .. 09-16 | 145 | 7270 | **0** | **0.0%** |
| 2026-09-17 .. 09-18 | 124 | **9230** | 59 | 47.6% |
| 65 天合计 | 1346 | 7887 | 1028 | **76.4%** |

⇒ 「系统段顶格」**不是长期常态**：09-01～09-16 连续 16 天均值仅 7270、丢弃 **0 次**；
09-17 起均值跳到 9230 才开始丢。**顶格是"内容增长"的结果，要盯的是均值趋势，不是某一条日志。**

**⑤ 想调整时的门**

- 要 `capabilities` **每次都在** → 抬 `system_token_budget` 至 ≥ 10500
  （每次调用多约 700 tokens，**属行为扩张**，需授权 + 实测）。
- 要**彻底不算** → 删 `cognition.py:690-699` 的片段块
  （会真的改变"系统段没顶格"那类运行的行为）。
- 能力的**主通道是工具面**（`register_as_independent_tool=true` → 启动注册为 Tool，
  日志可见 `内生能力: 启动注册 N 个到工具面`），提示词片段只是「主动倾向」的冗余叙述
  ⇒ 取舍看的是**倾向**，不是**可用性**。

---

## 4. 模块级维护指南

> 每个模块给出：职责 / 关键类 / 依赖配置 / 数据文件 / 运维注意。

### 4.1 核心运行时 `anima/core/`

| 文件 | 职责 | 关键类 | 数据文件 | 运维注意 |
|---|---|---|---|---|
| `anima_core.py` | 编排核，生命周期中枢 | `AnimaCore` | `data/sticker_policy.json` | 改生命周期=改核心，遵循 RELEASE_POLICY |
| `gateway.py` | 消息路由 + 出站 | `Gateway`、`OutboundMsg` | 无 | 出站前过内心独白过滤；**无全局单例**（历史误引用 `get_gateway`，2026-09-15 已全树清除） |
| `event_bus.py` | 异步主题订阅/发布 | `EventBus` | 无 | handler 异常隔离 |
| `config.py` | Pydantic 配置模型 | `AnimaConfig`、`load_config` | 读 YAML | 新增配置节必须同步 `schemas/config.schema.yaml` |
| `config_service.py` | WebUI 配置变更服务 | `ConfigService` | `data/config_changes.json`、`data/config_backups/*.bak` | 变更先成 change-set + 全量校验 + 脱敏 |
| `config_recovery.py` | 候选启动自动回滚 | 模块级函数 | `data/config_recovery.json` | `prepare_start` 必须在 Pydantic 加载前调用 |
| `config_drift.py` | 配置/权限漂移检测（只读） | `ConfigDriftMonitor` | 审计事件 | 只观测，owner 经 WebUI repin |
| `permissions.py` | 运行期权限覆盖 | `PermissionStore` | `data/permissions_override.json` | 只覆盖 `admins` |
| `persistence.py` | 统一原子文件持久化 | `PersistenceProvider` | 相对 `data_dir` | 所有磁盘写应经此类 |
| `module.py` | 模块生命周期抽象 | `Module`、`CognitiveModule` | 经 Persistence | — |
| `autonomy.py` | **Heartbeat 自主心跳引擎** | `Heartbeat`（L280） | `data/heartbeat.json` | 反刍/遗忘/梦境/自我代码/秘密/探索调度都在这 |
| `intents.py` | 主体意图账本 | `IntentLedger`、`SubjectOrchestrator` | `data/intent_ledger.json` | 自主表达唯一入口 |
| `workflows.py` | 工作流账本 | `WorkflowStore` | `data/workflows.json` | 触发↔意图绑定↔最终结果 |
| `tasks.py` | Owner 工程任务队列 | `TaskStore`、`OwnerTaskAgent` | `data/owner_tasks.json` | 只存脱敏摘要 |
| `approvals.py` | 高危工具审批队列 | `ApprovalStore` | `data/pending_approvals.json` | ttl 夹 [30,86400] |
| `audit.py` | 逐日 JSONL 审计 | `AuditLogger` | `data/audit/YYYY-MM-DD.jsonl` | 坏行隔离；`prune(retention_days=90)` |
| `backup.py` | 状态快照备份 | `BackupManager` | `data/backups/anima-state-*.zip` | retention=30 |
| `health_history.py` | 健康趋势 | `HealthHistory` | `data/health_history.json` | 聚合指标，无聊天正文 |
| `metrics.py` | 运行指标 | `MetricsCollector`、`get_metrics` | 无 | 进程内，无 Prometheus 依赖 |
| `evaluation.py` | 只读运行验收 | `RuntimeEvaluator` | 无 | 可验证运行事实代替健康分 |
| `replay.py` | 隔离行为回放 | `BehaviorReplay` | `data/behavior_replays.json` | 不碰生产 QQ/记忆/LLM |
| `reply_splitter.py` | 拟人分段 + 独白过滤 | `split_text`、`filter_inner_monologue` | 无 | 纯函数 |
| `conversation_assembler.py` | 连续输入合并 | `ConversationAssembler`、`GroupDebouncer` | 无 | 私聊/群聊不同窗口 |
| `paths.py` | 中央路径门面 | `set_data_dir/data_dir` | 派生 | `Paths`/`child` 未接线 |
| `exceptions.py` | 异常层级 | `AnimaError` 家族 | 无 | ⚠️ 零 import，未接线 |

### 4.2 LLM 层 `anima/llm/`

| 文件 | 职责 | 关键类 | 数据文件 | 运维注意 |
|---|---|---|---|---|
| `__init__.py` | LLMClient 完整客户端 | `LLMClient` | 经 usage_ledger | 重试/熔断/fallback/413 再编码都在此 |
| `handler.py` | LLMHandler 对话处理器 | `LLMHandler` | `data/output/anima_export_*.json` | 驱动五段 pipeline |
| `request_executor.py` | 意图分类执行 | `RequestExecutor` | `data/request_memory.json` | 意图 remember/remind/ignore/query/do_task/chat |
| `usage.py` | 用量账本 | `UsageLedger`、`scoped_llm` | `data/llm_usage.sqlite3` | 30+ 模块用 scoped_llm |
| `group_gate.py` | 群聊门控 | `classify_group_reply_skip` | 无 | mention/familiar/open |
| `ignore_list.py` | 忽略名单 | `IgnoreStore` | `data/ignore_list.json` | scope 群号/global |

**配置要点**：`llm.endpoint/api_key/model`、`fallback_models`（同网关容灾）、`image_generation.fallback_models`（生图跨模型容灾）、`module_models`（模块级路由）、`active_provider`/`providers`。熔断参数 `circuit_failure_threshold`/`circuit_cooldown_sec`。

### 4.3 QQ 渠道 `anima/channels/`

- `onebot11.py`：`OneBot11Adapter` — 正向 WS 收 + HTTP/WS 发；`get_image_path/resolve_image/get_msg`；并发 `Semaphore` + 背压；协议过滤（heartbeat meta、bot 回声、去重）。
- 配置：`qq.napcat_ws_url`、`napcat_http_url`、`self_id`、`access_token`（`Authorization: Bearer`，**代码不读 NAPCAT_TOKEN**）、`event_concurrency`、`max_pending_events`、`known_bot_ids`。
- 数据：`data/known_bots.json`。
- 运维注意：`channel_connected` 依赖 NapCat WS 重连，部署后轮询最多 60s 容忍瞬时未连。

### 4.4 消息流水线 `anima/pipeline/`

`anima/pipeline/stages/` 五阶段：**permission → memory → cognition → generation → guardrail**。
- `permission.py`：权限等级 + 群策略。
- `memory.py`：对话记忆读写。
- `cognition.py`：人格 + 认知注入 + 多模态。
- `generation.py`：LLM 生成 + 工具引导 + 分段 + 剥标签。
- `guardrail.py`：危险行为检测 + 身份泄漏拦截 + 记忆写入。
- 运维注意：阶段 try/except 降级，可选模块静默失败；改护栏=核心，遵守发布门禁。

### 4.5 认知内核 `anima/cognitive/`（含 inner_state）

按功能分组（均写入 `data/` 下的各自 json，经 `PersistenceProvider` 或单例）：

| 功能 | 文件 | 数据文件/注意 |
|---|---|---|
| 欲望 | `desire.py` | `data/desires.json` + `data/aspirations.json` |
| 世界观 | `world_model.py` | 只进唯一 system |
| 时间感 | `time_sense.py` | `data/time_sense.json`；M3 统一到配置目录消 split-brain |
| 关系 | `relation.py` | `data/relation.json` |
| 遗忘 | `forgetting.py` | — |
| 反刍 | `rumination.py` | `data/rumination_state.json` |
| 伤痕 | `scar.py` | `data/scars.json`；含回避期 |
| 矛盾 | `contradiction.py` | — |
| 情绪/灵魂 | `mood.py`、`soul.py` | `data/mood.json`；EMA 平滑 |
| 日程/日历 | `agenda.py`、`calendar.py` | agenda/calendar_daily/weekly |
| 梦境 | `dream_engine.py` | — |
| 探索 | `exploration.py` | 自主读取公开信息 |
| 学习/能力 | `learning.py`、`tool_learning.py`、`capabilities.py`、`capability_dedup.py` | 内生能力自生长 |
| 回复质量 | `reply_quality.py` | `data/reply_quality*` |
| 人格检查点 | `persona_checkpoint.py` | 漂移快照 |
| 危险行为 | `danger.py` | danger.self_improvement 只生成待审工程任务 |
| 模拟生活 | `simulated_life.py`、`sleep_state.py` | 生活模拟/睡眠 |
| 秘密 | `secret_auto.py`、`secret_injector.py` | B2 每日生成 |
| 表情 | `stickers.py`、`sticker_*.py`、`sticker_embedding/phash/emotion/search/select/similarity` | 表情资产 |
| 视觉 | `vision.py` | 多模态视觉理解 |
| SelfCode | `selfcode.py` | `data/selfcode/`（运行工具，不改核心） |
| Sylann | `sylann_adapter.py`、`sylann_suggestion_controller.py` | `data/sylann/` |
| X 搬图 | `x_browser.py` | `data/x_browser/`（cookies/profile） |
| 运营清理 | `ops_cleanup.py` | `anima ops-cleanup` 清理僵尸 SelfCode |
| inner_state | `inner_state/*.py` | 情绪连续性/硬事实/自我述说/矛盾感受/被忽视感/QZONE |

**运维注意**：多数模块为单例（`get_xxx(data_dir)`），数据目录统一到 `persistence.data_dir`；认知模块关闭用 `close_*` 单例清理（`AnimaCore.stop` 已遍历）。

### 4.6 记忆 `anima/memory/` 与跨群 `anima/crossgroup/`

- `chat_memory.py`：按 `(群,用户)` 隔离的对话记忆；`get_chat_memory(persist_path=...)`。文件 `data/chat_memory.json`。
- `vector_memory.py`：sqlite-vec + 远程 embedding/reranker；`data/anima.db`；未配自动降级 BM25。
- `long_term.py`：三层长记忆（事件/知识/行为模式）+ why-described；`data/` 下 events/knowledge/patterns jsonl。
- `episodes.py`、`continuity*.py`、`identity_verification.py`、`runtime_identity_attestation.py`、`authority_convergence.py`、`handoff_verification.py`、`secrets.py`、`web_knowledge.py`。
- `crossgroup/`：跨群记忆（memory/manager、user_memory、user_relations）+ 群氛围（atmosphere/analyzer、profiles）+ 生成（generation/prompt_builder）。
- **运维注意**：会话隔离按 `(群,用户)`；跨群/氛围只进唯一 system；`data/crossgroup/`。

### 4.7 工具/沙箱/Skills `anima/tools/` `anima/skills/`

- `tools/__init__.py`（1133 行）：`Tool`/`ToolRegistry`、`create_default_registry`（唯一装配点）。权限等级 `LEVEL_BANNED=0/READONLY=1/USER=2/ADMIN=3/OWNER=4`。`ToolRegistry.call` 依次做 schema-name→capability 转发、capability_only 拦截、`can_call` 权限、danger 审批、`_invoke`。exec 工具（run_python/run_shell/agent_exec）须 `exec_permitted(granted_scopes)`。
- `tools/sandbox.py`（993 行）：`bwrap→unshare→拒绝`（fail-closed，`SandboxDenied` 不降级）。`SandboxTier` 按权限分层：USER（无网络/10s/128MB/严格 import 黑名单）、ADMIN（30s/256MB/无网络）、OWNER（network/120s/512MB/`data/agent_workspace`）。`build_sandbox_env` 白名单 + 敏感 env 二次剔除。`run_python` 无条件用可信 ctx 覆盖 permission_level（反特权注入）。
- `tools/builtin/*`（`register_all_builtin`）：agent_tools、bilibili、code_exec、file、honor、merge、owner（propose_skill_draft/self_code/generate_image）、social、sticker、system、web、web_memory、x_search。**danger=True 仅两处**：`subscribe_bilibili_live`、`self_code`（均 OWNER 需审批）。另有 `code_exec`/`python_exec`（capability_only，默认不可达）。
- `tools/capability*.py`（capability_fs/capability_web）：Line B 治理引导 adapter（FilesystemRead/Write/SearchWeb Provider），`capability_gateway_mode=ON` 时惰性装配。
- `tools/meme_interact.py`/`qzone.py`/`voice.py`：**旧式 `TOOL_META`+`async run` 契约**（与 builtin 新式 `Tool` 对象并存）。
- `skills/`：`registry.py`、`drafts.py`、`models.py`、`evolution.py`（个人能力生态闭环）、`builtin/compose_notice`。
- **运维注意**：danger 工具经 approvals 需 owner 批准；SelfCode 只写 `data/selfcode/`，安装/删除/禁用需 owner；`auto_install` 恒不自动装；capability/gateway 治理默认 OFF。

### 4.8 自治/演进/OS `anima/agent/` `anima/evolution/` `anima/observation/` `anima/os/` `anima/monitors/`

**接线说明**：多数自治模块为「库函数/条件接线」，只有部分 WIRED 进生产。关键区分：
- **✅ WIRED（默认启用/运行时引用）**：`owner_agent`、`react`（ReActLoop）、`intent`、`owner_proposal`、`audit`、`guardrails`、`state`、`executor`、`planner`、`planning_interface`/`workflow`（宪法）；`os/control_surface`（R14）、`os/long_running`（P0-02）；`monitors` 整包；`evolution/{ci_dod,jury,impact,evaluator}`；`observation` 全模块（经 control_surface/handler）。
- **⚠️ 库函数/仅测试/未全局接线**：`agent/{devloop,orchestrator,multi_agent_society,memory_evolution,issue_sources,autonomous_maintenance,society_review}`；`evolution/orchestrator.ObservationOptimizationLoop`；`os/reliability`（运行时代码无调用方，仅 tests/手动）；**`pilot` 全链**。

**agent/**（R6–R15 自治主体层）
- `owner_agent.py`：`OwnerAgent`（submit/approve/cancel/status_text），入口 `permission_level>=4`（真 Owner）。写 `data/agent_audit.jsonl`+`data/agent_tasks.json`。**WIRED**。
- `react.py`：`ReActLoop`（max_rounds/permission_level/反工具死循环）。**WIRED**。
- `self_evolution.py`：R9 受控自我进化；`CORE_PROTECTED` 冻结集；`EvolutionGate` 默认 DENY 仅 Owner approve。写 `data/evolution_evidence.jsonl`。
- `devloop.py`：R8 自治开发闭环；Deploy 默认 **DENY**（仅 `owner_authorized`+注入 deploy_fn 才部署）。
- `orchestrator.py`：R7 多 Agent 编排；阶段权限不足→DENY。库函数。
- `multi_agent_society.py`/`agent_society.py`：R12/P3 辩论+裁判；Judge 无能力→inconclusive；建议性。
- `autonomous_operation.py`（R13）、`autonomous_maintenance.py`（P5）、`memory_evolution.py`（R11）、`guardrails.py`（AGENT_BLOCKED_PATTERNS）。

**evolution/**（评估/影响/陪审团）
- `orchestrator.py`：`ObservationOptimizationLoop`（`dry_run=True` 默认不部署）。仅测试构造。
- `impact.py`：P2 影响评分，读 `data/evolution_evidence.jsonl`；`has_data=False`→不编造。
- `jury.py`：G4 客观晋升尺；回归→rollback_suggest 仍需 Owner。
- `ci_dod.py`：CI DoD 门（backend pytest 全绿+frontend 构建+安全触核需 Owner）。
- `evaluator/`：R10-02 指标/Before-After/Benchmark。

**observation/**（R10/P1/P3/R18-R20，全部 WIRED）
- `collector.py`（collect_all→to_dev_issues）、`runtime_monitor.py`、`audit_analyzer.py`、`user_feedback.py`、`environment.py`（P1 真实环境）、`feedback_detect.py`（P3 实时纠错识别）、`self_evaluation.py`（R18 六轴）、`engineering_loop.py`（R19）、`github_source.py`（R17）、`long_term_operation.py`（R20，读 `data/stability/`）。

**os/**（Human-AI OS 控制面）
- `control_surface.py`：R14 控制面（聚合健康/演化/恢复/观测+受限 NL 指令），白名单+确定性、不持执行权限、绝不自我授权。WIRED。
- `long_running.py`：P0-02 稳定性，写 `data/stability/{window}d.jsonl`。WIRED。
- `reliability.py`：P0-01 每日健康证明，写 `data/health_proof/YYYY-MM-DD.jsonl`；经 `attestation.attest_runtime`。**库/按需（运行时代码无调用方）**。

**monitors/**（全部 WIRED）
- `scheduler.py`（低频轮询、异常隔离）、`bilibili.py`（只查公开房间）、`http_page.py`（**SSRF fail-closed**，拒本机/内网）、`subscriptions.py`（`data/monitors/subscriptions.json`）。

**核心铁律**：所有演化只生成提案，落地均需 Owner 审批；fail-closed；绝不自我重启/重部署/自我授权；canary/pilot 独立 data dir + kill_switch 独立熔断，人类可单点切断自治失控。

### 4.9 治理/契约/宪法 `anima/capability/` `anima/contracts/` `anima/constitution/`

**接线说明**：capability 治理层分「运行时适配线（受 `capability_mode`/`capability_gateway_mode` 等旗标门控）」与「规格/离线原型线（synthetic-only，只记审计事实、绝不激活运行时）」两层。
- **✅ 运行时适配线（WIRED，旗标门控）**：`contract/canonical/scopes/index/resolver/schema`（经 tools，`capability_mode`）、`governance/{enforcement,audit,lifecycle,validation,state,attestation}`、`adapters` 双投影、`audit/{storage,persistent_sink}`、`health/{adapter,store,normalizer}`、`ops/{observer,transition_store}`。
- **⚠️ 库函数/仅测试（未全局接线）**：`rbac`（`RUNTIME_WIRED=False`，运行时唯一权威=`pipeline/stages/permission._check_permission`）、`isolation`、`evidence`、`audit/event_chain`、`audit/query`、`governance/audit_chain`、`governance/surface`、`ops/policy`（`POLICIES={}` 出厂为空=不猜阈值）、`ops/health_policy_evaluator`，以及 P3–P6 证据/事实切片（activation/authorization_fact/intent_fact/execution_dispatch 只读事实桥）。**2026-09-12 归档**：`runtime_*` 事实桥 7 个 + `evolution_contract` + 16 个顶层 `capability_*.py` shim 已 git mv 至 `capability/_archive/`（只读冻结快照，pytest 不收集），留守模块 import 已改指 canonical 路径。

**capability/**（Capability 治理 OS 层）
- `contract.py`/`canonical.py`：`CapabilityContract`、`CanonicalCapabilityContract`（CANONICAL/LEGACY/RUNTIME 来源并集）。
- `scopes.py`：`PermissionScopes`（5 scope）+`map_scopes_to_sandbox`（只收窄+`exec_permitted`）。
- `governance/state.py`：R6.2 `GovernanceState`（revoke/kill switch）；revoked/kill-switch=true→所有 governed capability DENY（含 owner）。**WIRED（tools get_governance_state）**。
- `governance/attestation.py`：`attest_runtime`/`config_hash`；缺 approved 参考或任一不匹配→DRIFT（fail-closed）。**WIRED（os/reliability:314）**。
- `governance/enforcement.py`：`EnforcementLevel`，默认 **PERMISSIVE** 只拦 ERROR；注册期 fail-closed 拒。**WIRED（resolver register）**。
- `governance/lifecycle.py`：六态 `REGISTERED→VALIDATED→ACTIVE/DEPRECATED/QUARANTINED→REMOVED`，唯一写入点 `_apply`。
- `governance/gateway.py`：F8.5 `CapabilityGateway` 全闭环；仅 `capability_gateway_mode=True` 才注册 → 生产默认 OFF。
- `rbac.py`/`isolation.py`/`evidence.py`：R6.3/6.4/6.5 工业级组件，**库函数未接线**（RUNTIME_WIRED=False）。
- `audit/event_chain.py`：R6.2 端到端审计事件链（caller/capability/permission/provider/sandbox/result）。
- `health/`、`ops/`、`audit/`：三链正交（ResolutionEvent/HealthEvidence/LifecycleTransition 各自独立 store 不合并）+ HashChain 防篡改。
- 数据：**落盘目录可配** —— `capability_layer.governance_data_dir`（默认 `"governance"`，
  相对 `persistence.data_dir` 解析；给绝对路径则按原样使用。实现见 `anima/tools/__init__.py:1219-1228`）。
  三个文件名**固定不可配**（刻意：避免运维把三条链写进同一文件而丢失链间边界）。
  - 解析后路径：`<data_dir>/<governance_data_dir>/resolution_events.jsonl`
    · `.../health_evidence.jsonl` · `.../lifecycle_transitions.jsonl`
  - 生产实配：`governance_data_dir: "governance"` ⇒ 实际为 **`data/governance/resolution_events.jsonl`**
    （2026-09-18 实测存在，8555 B / 16 条事件）。⚠️ 此前本行写作 `data/resolution_events.jsonl`
    且未提 `governance_data_dir` 可配 —— 照着找会找不到文件（审查 P1-8）。
  - 门控：`governance_audit_enabled` / `health_evidence_enabled`（默认 False）/
    `transition_audit_enabled`；生产仅第一项为 `true`。

**contracts/**（M0.1 内核协议层，纯契约零运行时逻辑）
- `access.py`：`DeprecatedCoreAccess`（memory/config/outbound/telemetry/permission/agenda/tools 7 facade）+ `RAW_ACCESS_*` 后门 allowlist；raw 访问强制记 caller+审计。**WIRED（llm/handler ctx.core）**。
- `capability.py`/`errors.py`/`execution.py`/`facades.py`/`lifecycle.py`/`pipeline.py`/`adapters.py`：ABI 契约与边界组装。

**constitution/**（宪法派生链 + 唯一写者 Authority）
- `memory_engine.py`/`memory_authority.py`：M1.1/M1.8 权威 Memory 单写者（hydrate fail-soft/dedupe/provenance）。**WIRED**。
- `identity/need/affect/drive.py`：M1.2-1.4 派生链（无 setter、无存储权）。**WIRED（core/autonomy `_constitution_drive`）**。
- `planning_interface.py`/`workflow.py`：Plan run-state 唯一写者（私有 `_transition`、非法迁移抛 violation）。**WIRED（cli + runtime）**。
- `reflection.py`/`persona.py`/`relationship.py`：只读投影（仅 invariants/test）。库函数。
- `errors.py`：`ConstitutionViolation`（全层唯一拒止异常）。

**关键设计原则**：Fail Closed（空配置=拒绝、EvolutionGate 默认 DENY、attestation 缺参考即 DRIFT）；No Hidden Authority（派生生态无 setter、CORE_PROTECTED 冻结权限/安全/部署/证据）；Audit Everything（多套 append-only 证据链 + 敏感参数只留 SHA256 fingerprint）；self-improvement ≠ self-authorization（细节自动、落地等 Owner）。

### 4.10 运行时恢复 `anima/runtime/`

生产唯一接线链：`cli.py → recovery_module.RecoveryModule → bootstrap.recover_all → recovery.recover_run` + `persistence.WorkflowRunStore`。
- 数据：`data/workflow_runs/{id}.json`。
- ⚠️ 其余（executor/real_task_executor/resolving_executor/execution_ledger/pilot_*/bridge_bootstrap/startup_recovery/resume_coordinator/recovery_marker_store/execution_events）**未接生产**，属试点/测试层。

### 4.11 WebUI 后端 `anima/webui/`

- `server.py`（3865 行）：aiohttp 服务。**鉴权模型**：token fail-closed（`webui.token` 未配则锁定，仅登录页可渲染）+ HttpOnly Cookie + CSRF（写操作 `X-Anima-CSRF`）。免认证仅 `/login`,`/source`,`/source/archive`,`/api/auth/login`,`/assets/*`。控制端点 `_owner_id()` 前置校验（单 token=owner 模型）。
- `routes/__init__.py`（328 行）：按域注册（页面/认证/健康/配置/LLM/认知/记忆/工程·SelfCode·OS/自主·一生/社交/表情/Agent 等）。`routes/agent.py` 为 Owner Agent 面板。
- `dashboard.py`/`login_page.py`/`source_page.py`/`life_*.py`/`platform_summary.py`。
- **数据**：配置变更 `data/config_changes.json`、`data/config_backups/`；审计读 `data/audit/`。
- **运维注意**：控制类接口需 token + owner；`webui.token` 为空则控制接口全拒；生产监听 `0.0.0.0` 必须保留 token + 建议反代 HTTPS；`/vnc` noVNC 反代也已纳入 token 保护。

### 4.12 前端 `webui/`

- Vue3 + TS + Vite SPA；`npm run build` → `anima/webui/static`（哈希 asset）。
- 视图：overview/life/memory/cognition/abilities(skills/exploration/automation/stickers/plugins)/engineering/settings/os。
- **运维注意**：`vite.config.ts` `base=/assets/`，outDir `../anima/webui/static`，`emptyOutDir`；构建后静态资源带 hash，部署脚本会清理未引用旧 `app-*`。

### 4.13 独立 Console `web/`

- 独立 FastAPI 运维面板（非废弃，旁路）：`server.py`（576 行）+ `console.html`，读 `config.yaml`/`data/`，`ANIMA_CONSOLE_TOKEN` Bearer（缺失或占位 → startup `sys.exit(1)` fail-fast）。端点：`/api/health|status|config|persona|memory|logs|restart|plugins`。
- 相关 systemd：`web/anima-console.service`（root 运行，读 journal + restart anima，`EnvironmentFile=/opt/anima/web/.env`）、`web/anima-napcat.service`（NapCat 启动，xvfb-run 起 QQ，硬编码 QQ 号）。
- 反代配置：`web/deploy/anima-console.nginx.conf`、`web/deploy/Caddyfile`（自动 HTTPS）。
- **运维注意**：`anima-console.service` 以 root 运行，仅在可信网络/反代后暴露；console 与主 WebUI 是两个不同入口；已知限制：PUT 配置需重启生效、插件不直接安装（Tier1 垫片）、`yaml.safe_dump` 重排格式丢注释。

### 4.14 兼容层/插件 `anima/compat/` `anima/plugins/`

- `compat/loader.py`（243 行）：`load_plugins/load_plugin/unload_plugins`；把 `anima.compat.astrbot` 注册为 `sys.modules["astrbot"]`；支持旧 `plugins/*.py` 与标准 `plugins/<name>/main.py`；防同名覆盖。
- `compat/astrbot/`：**Tier 1 兼容层**（docstring 明确）——filter、message chain、事件、Context 注入、LLM 提示钩子、AstrBotConfig、StarTools、llm_tool 桥接、register_web_api 元数据托管；core/ 子包多为占位垫片。`config.compat.astrbot_enabled`。**建议灰度验证**。
- `plugins/runtime.py`（138 行）：**Anima 原生声明式插件运行时**（plugin.yaml：id/name/version/min_level/entrypoint/enabled/capabilities），`PluginContext.register_tool` → 命名空间 `plugin_<id>_<name>`，仅加载 owner 安装的包。
- 数据：`plugins/` 目录。

### 4.15 媒体 `anima/media*.py`

- `media.py`（571 行）：大图下载 + WebP 缩放/GIF 关键帧/线缆体量预算；进程内 LRU 转码缓存（64 项）+ 可选磁盘缓存 `media_prepare_cache/`（128MB）。关键常量：`DEFAULT_MAX_INLINE_BYTES=2MB`、`DEFAULT_MAX_LLM_IMAGE_BYTES=1MB`、`DEFAULT_MAX_IMAGE_DIMENSION=1280`、`DEFAULT_MAX_ANIMATION_FRAMES=3`。图片仅进内存/不落盘。
- `media_generation.py`（100 行）：`ImageGenerationManager`，Gemini 生图 + 日配额（`image_generation_usage.json`）+ 串行锁 + 原子写，输出 `data/generated/<day>/image_<hash>.<ext>`。
- `media_observation.py`（31 行）：富媒体隐私最小化观察对象。
- **运维注意**：图片走 `resolve_image` 三级兜底；NapCat 取图必须 `get_image`；413 先再压缩后剥图；大图先本地转码再进 LLM。

### 4.16 试点层 `anima/pilot/`

`pilot_runtime.py`、`wiring.py`、`canary*.py`、`monitoring.py`、`safety_filter.py`、`permission_boundary.py`、`message_adapter.py`、`entry_gate.py`、`media_recovery*.py`、`production_monitoring.py`、`identity_*`、`authorization.py`、`conversation_evidence.py`。
- **运维注意**：多为试点/验收层，PilotEntry 生产仅作 `None` 注入；改此层不影响生产主链路。

---

## 5. 数据文件清单

`data/`（生产 `/opt/anima/data/`）主要文件：

| 文件/目录 | 内容 |
|---|---|
| `chat_memory.json` | 对话记忆（按 群,用户 隔离） |
| `anima.db` | 向量库（sqlite-vec） |
| `llm_usage.sqlite3` | LLM 用量账本 |
| `audit/YYYY-MM-DD.jsonl` | 逐日审计 |
| `heartbeat.json` | 心跳状态 |
| `intent_ledger.json` / `workflows.json` / `owner_tasks.json` | 意图/工作流/任务账本 |
| `pending_approvals.json` | 高危审批队列 |
| `config_changes.json` / `config_recovery.json` / `config_backups/` | 配置变更/恢复/备份 |
| `permissions_override.json` | 运行期管理员覆盖 |
| `sticker_policy.json` | 表情策略 |
| `health_history.json` / `health_proof/` / `stability/` | 健康趋势/证明/稳定性 |
| `backups/anima-state-*.zip` | 状态快照备份 |
| `crossgroup/` | 跨群记忆 + 群氛围 |
| `selfcode/` + `selfcode_backup_*` / `selfcode_audit.json` | SelfCode 运行时工具/审计 |
| `x_browser/` | X cookies/profile |
| `evolution_evidence.jsonl` | 演化证据 |
| `resolution_events.jsonl` / `health_evidence.jsonl` / `lifecycle_transitions.jsonl` | 治理三链 |
| `workflow_runs/{id}.json` | 运行时恢复快照 |
| `stickers.json` + 表情资产 | 表情库索引/图片 |

> **纪律**：`data/`、生产 `config.yaml`、`.secrets`、密钥、用户数据不得提交 Git，不得泄露消息正文/凭据/traceback。

---

## 6. 配置维护

### 6.1 修改方式

1. **推荐**：WebUI 设置页 → 配置变更集（change-set）→ 全量校验 → apply（必要时受控重启，启动健康点自动回滚）。
2. 直接改 `/opt/anima/config.yaml`（SSH）→ `systemctl restart anima`。
3. `config_service.MANIFEST` 是可变更键的权威来源；`config.py` `AnimaConfig` 是完整 schema；`schemas/config.schema.yaml` 是声明式模板。

### 6.2 敏感配置

- 密钥只放生产配置或 `.secrets`（systemd `EnvironmentFile`）；WebUI 响应恒脱敏。
- 环境变量：`ANIMA_LLM_API_KEY/ENDPOINT`、`ANIMA_EMBEDDING_API_KEY`、`ANIMA_RERANK_API_KEY`、`ANIMA_TAVILY_API_KEY`。
- 部署默认不覆盖生产 `config.yaml`；`--with-config` 是高风险显式选项。

### 6.3 配置回滚

- 变更集 apply 后触发重启；若候选启动在 `AnimaCore.start` 前崩溃，`config_recovery` 自动恢复上一份 YAML（change 标 `rolled_back`）。
- 源码回滚 ≠ 配置回滚 ≠ 数据回滚，三者必须分别确认（RELEASE_POLICY §6）。

---

## 7. 监控与健康检查

### 7.1 进程 / 服务

```bash
systemctl is-active anima
systemctl status anima --no-pager -l
systemctl show --property=ActiveEnterTimestamp anima
```

### 7.2 API 健康

```bash
curl -H "Authorization: Bearer <token>" http://127.0.0.1:8088/api/health
curl -H "Authorization: Bearer <token>" http://127.0.0.1:8088/api/modules/status
curl -H "Authorization: Bearer <token>" http://127.0.0.1:8088/api/evaluation
curl -H "Authorization: Bearer <token>" http://127.0.0.1:8088/api/reply-quality
curl -H "Authorization: Bearer <token>" http://127.0.0.1:8088/api/ops-quality
```

重点核对：`running`、`heartbeat_running`、NapCat `channel_connected`、LLM、无持续熔断/权限错误。

### 7.3 自治健康（OS 层）

- `/api/os/overview`：聚合健康/恢复/提案/观测/稳定性/影响/记忆/维护/技能全景。
- `data/health_proof/YYYY-MM-DD.jsonl`：每日健康证明（fail-closed）。
- `data/stability/{7,30,90}d.jsonl`：长期稳定性。
- `scripts/health_regression.py`：线上健康回归基线（`ANIMA_HEALTH_URL/TOKEN` 冒烟生产）。

### 7.4 自主异常自检

- `anima.os.reliability` 的 `answer_was_anomaly_yesterday` 回答「昨天有无异常」。
- R16：health_proof 漂移/降级/异常转 owner 待办。

---

## 8. 日志

- 服务日志在 systemd journal：
  ```bash
  journalctl -u anima -n 100 --no-pager
  journalctl -u anima -f
  journalctl -u anima --since "30 min ago" --no-pager
  ```
- 审计日志：`data/audit/YYYY-MM-DD.jsonl`。
- 用量：`data/llm_usage.sqlite3`。
- 运行事件：`data/logs/execution.jsonl`（执行桥）。
- **纪律**：日志/报告不得泄露消息正文、凭据、token、QQ 号敏感上下文。

---

## 9. 备份 / 恢复 / 回滚

### 9.1 备份

- `data/backups/anima-state-*.zip`：Owner 状态快照（不含聊天正文/密钥），`BackupManager` retention=30。
- 部署前自动单份滚动备份：`chat_memory.json.pre-isolation`、`stickers.json.pre-reconcile`。
- `data.deploy-bak`：部署前 `data/` 快照。

### 9.2 恢复

- WebUI `/api/life/snapshot/restore`（owner_ok + confirm，fail-closed，产物落 `data/life_restore/`）+ `/api/life/snapshot/rollback`。
- SelfCode 备份恢复：`/api/selfcode/backups/{id}/restore`。
- 运行数据回滚是**高风险动作**，不得自动删/覆/回退 `/opt/anima/data/`；仅在 owner/governance 授权 + 已确认备份 + 可验证恢复步骤下执行。

### 9.3 回滚（发布）

- 回滚前先保存证据：`version.json`、服务状态、最近日志、健康检查、待回滚 commit。
- 源码回滚 = 回到上一已验证 commit，按同一 `_deploy.py` 流程；不要手工混合复制源码。
- 配置/数据回滚分别独立确认。

---

## 10. 常见故障处置

### 10.1 服务无法启动

先看 `systemctl status` 与 `journalctl`，区分：配置解析 / 依赖 / 权限 / 端口 / NapCat 连接 / LLM 凭据。配置错误修配置并验证，不应借机改架构。

### 10.2 active 但健康检查异常

区分本地 API、NapCat WS、LLM、心跳、出站回执。记录哪个子系统失败及持续时间；不要因单个可选模块异常改动核心流水线。部署后瞬时未连 NapCat 属正常（轮询最长 60s）。

### 10.3 发布前后状态不一致

比较 commit、`version.json`、systemd `ActiveEnterTimestamp`、日志、`/api/health`。无法证明运行版本身份则停止发布，先恢复可识别状态。

### 10.4 QQ 不回复 / 回执异常

- 确认 NapCat 存活（`web/anima-napcat.service` 或 `systemctl status napcat`）、`qq.napcat_ws_url` 可达、`channel_connected` 为真。
- `anima doctor` 检查 WS 连通。
- 查看 `data/audit/` 是否有 DENY / 熔断 / 权限错误。

### 10.5 LLM 异常

- 检查 `circuit_open`（熔断）、`fallback_models` 是否生效、413 再编码是否降级。
- `/api/llm/usage` 查看失败率/延迟；`/api/llm/providers/test` 测 `/models` 连通。

### 10.6 X 搬图不工作

- `python -m anima x-diag` 诊断：启用/引擎/代理/登录态/额度。
- 常见：patchright 未装、`data/x_browser/cookies.json` 无 x.com cookie、代理不可达、风控退避。
- 重登 X：`scripts/x_login.py` / `scripts/x_vnc_login_start.sh` → `x_export_cookies.sh`。

### 10.7 配置变更后反复崩溃

- `config_recovery` 会标 `rolled_back`；查看 `data/config_recovery.json` 与 `data/config_changes.json`。
- 手工恢复受控备份并重启。

### 10.8 需要「顺手优化」

不是运维故障。记录观察、停止变更，按 RELEASE_POLICY 判断是否有真实 contradiction，不要自行扩大范围。

---

## 11. CI 与发布纪律

- CI：`.github/workflows/anima-ci.yml` 四 job（backend pytest / frontend build / security guard / evidence），只读验证，无部署凭证，不自动批准 Evolution。
- 本地：`make ci` 复跑 CI（pytest + anima_guard + ci_evidence）。
- 发布门禁：baseline verify → test result → diff audit → frozen boundary check → commit → release。
- 历史上 6 个「预存 broken」测试（`test_r3_4_audit_completeness`、`test_r3_5_kill_switch`、`test_f9_2_operational_activation`、`test_memory_read_r2a`、`test_audit_persistence`、`test_health_observer`）**现已全部修复并通过**（2026-09-01 复核）。其中后两者早期被认为「subprocess 硬编码 Windows 路径」，实为 `sys.executable` + 相对路径（可移植），无需忽略。以全量 pytest 绿为准。
- **禁止自动修复**：改 Foundation contract/冻结 schema/新增 runtime layer/daemon/timer/polling/改权限边界/删生产数据而无授权 → 必须 STOP 并上报（事实→证据→判断→风险→下一步）。

---

## 12. 维护清单（Checklist）

**日常（每日/每次部署）**
- [ ] `systemctl is-active anima` = active；`ActiveEnterTimestamp` 刷新。
- [ ] `/api/health` running + heartbeat_running + channel_connected。
- [ ] `/api/modules/status` 无持续降级项。
- [ ] 日志无新增持续 warning/error。
- [ ] 数据目录未异常膨胀。

**部署前**
- [ ] `pytest tests/ -q` 全量绿（或按范围）；WebUI 变更 `npm run build`。
- [ ] 工作区干净，commit 完整。
- [ ] `python _deploy.py --dry-run` 预检通过。
- [ ] diff 只含授权范围，无密钥/数据/生成物。

**部署后**
- [ ] `systemctl restart anima` + 核对 PID/Timestamp。
- [ ] smoke 通过（health/modules/evaluation/runtime_status）。
- [ ] `version.json` 与发布 commit 一致。
- [ ] 备份存在（chat_memory.pre-isolation / stickers.pre-reconcile / data.deploy-bak）。

**安全 / 治理**
- [ ] 未触碰 CORE_PROTECTED 而未获 Owner 审查。
- [ ] 未自动批准 Evolution / 未自动部署 / 未自我授权。
- [ ] 生产 `config.yaml` 未被覆盖（除非显式 `--with-config`）。
- [ ] 无密钥/Token/聊天正文泄漏到日志/报告/Git。
