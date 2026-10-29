# 私聊/群聊双模式设计（A+B+C）

> 状态：待评审 · 落点：`G:/Anima-next`（生产路径）· 2026-10-29
> 触发：owner 希望「私聊更智能」+「群里能水群」。

## 0. 一句话结论

三条线**都不需要新建大模块**，核心是三个已经存在但未连通的部件：

| 线 | 已有地基 | 缺的那一步 |
|---|---|---|
| **A 记得久/跨场景** | `anima/memory/` 的 `scope` 字段（`authority_convergence.py:148`、`continuity.py:86`、`episodes.py:40`）、`secrets.py` 的隐私先例 | 注入侧的**可见性门**：检索结果按当前通道过滤 |
| **B 更能干活** | `handler.py:1092` 私聊 `agent.submit(auto_run=True)` | 审批从"命令式"改成"私聊自然语言问答" |
| **C 会水群** | `reply_learning.py` 的"开口后是否被接住"统计（`window_sec=300`，生产已启用）、`crossgroup.observe_all_messages` | 一条**独立的主动发言旁路** + 独立预算 |

## 1. 现状证据

- 私聊：owner 说人话 → 直接开跑，回执自然语言（`llm/handler.py:1078-1104`）。
- 群聊：`reply_mode` 三档 `mention|familiar|open`，生产配 `familiar`（`config/production.example.yaml:95`）；Gate B 主动过滤超短闲聊（`llm/group_gate.py:36-50`）——**这正是水群内容，所以水群不能靠放宽 reply_mode**。
- 审批：`security.require_approval_for_danger`（`core/config.py:1184`），唯一执行点 `tools/__init__.py:1414`。
- 隐私先例：`memory/secrets.py` 已确立"秘密不进 chat_memory，只隐式影响生成"。

## 2. A · 记忆三级可见性

### 2.1 数据结构

在现有 `scope` 之外**新增一维** `visibility`（`scope` 管"属于谁"，`visibility` 管"能在哪出现"，两者正交）：

```python
# anima/memory/visibility.py（新文件，纯函数，无状态）
PUBLIC = "public"    # 群里说过 / 公开事实 → 群聊、私聊都可召回
PRIVATE = "private"  # 私聊专属（主人私事）→ 仅私聊召回，群聊永不注入
SEALED = "sealed"    # 敏感（凭据/住址/健康）→ 双通道默认不注入，仅显式追问时按需读取

def admit(item_visibility: str, channel: str) -> bool:
    """channel ∈ {"private", "group"}"""
    if item_visibility == SEALED:
        return False
    if item_visibility == PRIVATE:
        return channel == "private"
    return True
```

### 2.2 落点

- 写入：`memory/chat_memory.py` 与 `memory/long_term.py` 的记录增加 `visibility` 字段，默认 `PUBLIC`；`secrets.py` 写入的记录一律 `SEALED`。
- 读取：`memory/vector_memory.py:624 search()` 增加 `visibility` 入参，检索后过滤（**在打分之后、打包之前**过滤，不参与排序以免拉低相关度）。
- 注入：`pipeline/stages/memory.py` 按 `ctx.detail_type` 传 channel。

### 2.3 升级路径

旧记录无 `visibility` 字段 → 默认 `PUBLIC`（保守但不破坏现状）。一次性迁移脚本只处理 `secrets.json` → `SEALED`。

### 2.4 验收

- 群聊会话中，构造一条 `PRIVATE` 记忆，检索结果**不含**它（断言 + 生产日志抽查）。
- 同一用户从群聊转私聊，`PUBLIC` 记忆可召回（跨场景连贯性）。
- 旧数据零迁移也能正常启动。

## 3. B · 私聊审批的自然语言闭环

### 3.1 原则

**安全边界不动**：`require_approval_for_danger` 仍为 `True`，危险操作仍然要批。只把"批"的入口从命令改成对话。

### 3.2 流程

```
工具调用命中危险门
  → 不抛"需要审批"的机械错误
  → bot 用角色口吻问一句：「本喵要删掉 data/tmp/xxx，可以吗？」
  → 记 pending_approval（带 tool_call_id + 过期时间）
  → owner 回「行/可以/删吧」 → 放行该次调用
  → owner 回「不行/别」 → 取消，并回一句确认
  → 5 分钟无回复 → 过期，取消，不再纠缠
```

### 3.3 关键约束

1. **只在私聊**。群里出现的审批提问会暴露"它不是人"（沿用 `handler.py:1085-1090` 已确立的判据）。
2. **同意必须绑定具体调用**。owner 说「行」只放行当前这一个 pending，不做全局解锁，避免后续误判。
3. **过期即取消**，不默认放行。
4. 群里触发危险操作 → 仍走原路径（建草稿 + 私聊通知），不变。

### 3.4 落点

- 新增 `anima/agent/approval_dialog.py`：pending 队列 + 自然语言意图判定（复用现有 `intent.py`）。
- `tools/__init__.py:1414` 处改为：先查 pending 是否已批准，否则登记 pending 并返回"等待 owner 确认"。
- `llm/handler.py` 私聊分支前置拦截：若存在 pending 且文本是肯定/否定，直接处理，不进入主流水线。

### 3.5 验收

- 私聊让 bot 删文件 → 收到一句人话询问 → 回「行」→ 执行成功。
- 回「不行」→ 不执行，有确认回复。
- 5 分钟不理 → 自动取消，无残留 pending。
- 群里触发同一操作 → 行为与今天完全一致（回归测试）。

### 3.6 管理员授权（并入 P1）

> owner 需求：非 owner 私聊时弹审批，owner 授权后对方即可使用全部功能。

**机制已存在 80%**：`core/permissions.py` 的 `PermissionStore` 已实现 `add_admin` / `remove_admin`，落盘 `permissions_override.json`，重启不丢。**但唯一调用点在 webui**（`webui/server.py:2044`、`webui/routes/__init__.py:265-266`）——D 线删 webui 后这条入口就断了，因此必须并入 P1 一起迁。

权限四档（`pipeline/stages/permission.py:46-49`）：`0 banned / 1 readonly / 2 user / 3 admin / 4 owner`。

#### 流程

```
非 owner 私聊，请求超出 level 2
  → 登记 pending_grant（记录 QQ + 首次请求时间）
  → 私聊通知 owners（不通知 admin）
  → owner 回「授权 <QQ>」/「同意」
       → permission_store.add_admin(qq)
       → 回复被授权者「主人给你开了权限」
  → owner 回「拒绝」→ 丢弃，通知被授权者一句
```

#### 三条硬约束

| 约束 | 理由 | 落点 |
|---|---|---|
| **admin 不得调 `add_admin`** | 否则授权 = 转让 root，被授权者可无限扩散，owner 失去控制 | `add_admin` 入口只对 level 4 开放 |
| **授权永久有效，可手动撤销** | owner 决策：不做 TTL | 私聊「撤销授权 <QQ>」→ `remove_admin` |
| **审批只发 owners** | 沿用 `owner_proposal.py:8` 的 Sacred 约束 | 收件人 = `config.permissions.owners` |

#### 验收

- 非 owner 私聊触发受限请求 → owner 收到私聊审批。
- owner 回「授权 123」→ 对方下次私聊 level=3，工具链放开。
- 被授权者尝试授权第三人 → **被拒绝**。
- owner 回「撤销授权 123」→ 对方回落 level=2。
- 重启后授权仍在（`permissions_override.json` 持久化）。

## 4. C · 水群旁路

> ⚠️ owner 决策：**全局开关，默认开**。此选择风险高于"逐群白名单"，故本设计附带强制护栏（4.3），护栏不达标不允许合并。

### 4.1 架构

```
群消息 ──► group_gate（不动，仍管"该不该回"）
              │
              └─ 未触发 ──► 【新】ambient_listener（只观察）
                                ├─ 累积话题 + 群氛围（复用 crossgroup/atmosphere）
                                └─ 达阈值 → 生成一句 → 低优先级队列 → 发出
                                                     │
                                                     └─► reply_learning 判定"是否被接住"
                                                          └─► 被无视 → 自动降频
```

### 4.2 触发条件（全部满足才开口）

1. 距上次本群开口 ≥ `min_gap_sec`（默认 600）
2. 本群本小时开口数 < `hourly_budget`（默认 3）
3. 话题相关度 ≥ 阈值（有记忆/有历史兴趣命中）
4. 群氛围非严肃（`crossgroup_bot/atmosphere` 已有画像）
5. 当前无人正在与 bot 对话

### 4.3 强制护栏（默认开的前提）

- **熔断**：连续 3 次开口无人接（`reply_learning` 已有该信号）→ 该群自动关闭水群 24h，并私聊 owner 报备。
- **总闸**：`banter.enabled: true` 是总开关，但单群可被熔断覆盖；owner 一条命令可全局关。
- **影子期**：上线首周 `banter.dry_run: true`，只记录"本该说什么"，不真发。owner 看完日志再关 dry_run。

### 4.4 落点

- 新增 `anima/cognitive/ambient_listener.py`（观察 + 阈值）
- 新增 `anima/cognitive/banter.py`（生成 + 预算 + 熔断）
- `config/default.yaml` 增 `banter:` 段
- 模型：`llm.module_models.banter = gemini-3.5-flash-lite`（`module_models` 是 `dict[str,str]`，任意键名有效；调用点用 `@scoped_llm("banter")`，同 `selfcode` 的既有写法）
- 复用 `reply_learning.mark_reply/observe`，**不新造反馈机制**

### 4.5 验收

- dry_run 模式下群里零新增发言（断言出站计数）。
- 关闭开关后，群聊行为与今天逐字一致（回归）。
- 熔断触发后 24h 内该群无水群发言，且 owner 收到一条私聊。

## 5. 交叉影响

| 交叉点 | 风险 | 处置 |
|---|---|---|
| A × C | 水群发言可能泄露 PRIVATE 记忆 | 水群生成**只允许**读 PUBLIC，硬编码 |
| A × B | 私聊审批内容可能含 SEALED | 审批提问不回显敏感值，只说"某个文件" |
| B × C | 无 | 通道隔离 |

## 5.5 D · 剔除 Web 界面

> owner 决策：①②③ 全删，彻底不要 Web 界面。本仓库里 **三个不同的东西** 都叫 webui。

### 5.5.1 三个目标的实际爆炸半径

| 目标 | 规模 | 生产状态（2026-10-29 实测） |
|---|---|---|
| ① `anima/webui/` | 4998 行 Python + `static/` 构建产物 | **`enabled: true`，绑定 `0.0.0.0:8088`**（非本机） |
| ② `web/`（`anima-console` systemd，root 运行） | 独立进程，端口 8080/8443，可读 journal + `systemctl restart anima` | **active**，8443 从本机 curl 返回 200 |
| ③ `webui/`（Vue 源码 + node_modules） | 前端工程 | 构建产物已嵌进 ① 的 `static/` |

**关键事实**：仓库 `config/default.yaml:503` 写 `enabled: false`，生产 `/opt/anima/config.yaml:372` 是 `enabled: true` + `host: 0.0.0.0`。按 AGENTS.md 的规矩，**以生产为准**。鉴权目前靠 `/opt/anima/.secrets` 里的 `ANIMA_WEBUI_TOKEN`（API 实测 401），但**端口对整段内网开放**。

### 5.5.2 前置：审批入口不能先死

webui **同时是审批入口**，而审批背后挂着一批 **fail-closed** 状态机：

| 模块 | 约束 | 删掉入口的后果 |
|---|---|---|
| `agent/self_evolution.py:10` | EvolutionGate 默认 DENY，只有 Owner 显式 `approve()` 放行 | 自进化**永久 DENY**，功能死亡 |
| `agent/owner_proposal.py:8` | owner 逐次批准（Sacred） | 提案永久 `awaiting_approval` |
| `cognitive/stickers.py:548` | 表情需 `status == "approved"` 才生效 | 新表情永不启用 |
| `agent/autonomous_maintenance.py:9` | 绝不自我安装/自我授权 | 只产 `pending_owner`，无人消费 |

**因此 D 依赖 P1**：私聊审批链路（§3）必须先跑通，webui 才能物理删除。这不是保守，是避免造出四个永远卡死的功能。

### 5.5.3 删除清单

```
删除：
  anima/webui/                 （4998 行 + static 构建产物 + routes/）
  web/                         （anima-console 独立服务；含 .service 单元）
  webui/                       （Vue 源码 + node_modules）
  scripts/run_webui_preview.py
  config/default.yaml 的 webui: 段
  core/config.py 的 WebUIConfig / webui 字段
  core/config_drift.py 的 webui.enabled/host/port 三元组
  core/anima_core.py:279-286, 497-499 的启动/关闭钩子
  tests/ 中 166 个引用文件的相关用例
改造：
  observation/audit_analyzer.py:17  —— webui_task_rejected 等事件名改为私聊审批事件名
  tools/__init__.py:1414            —— 审批门改走 approval_dialog（P1 已做）
  evolution/ci_dod.py:5,35          —— 移除 "webui npm run build" 这条 DoD
生产同步：
  /opt/anima/config.yaml 的 webui 段删除或 enabled: false
  systemctl disable --now anima-console
  ss 确认 8088 / 8443 不再监听
```

### 5.5.4 验收

- `grep -rn "webui" anima/ --include=*.py` 除审计事件名外零命中。
- 生产 8088 与 8443 均不再监听（`ss -tlnp` 无输出）。
- 私聊审批链路在 webui 删除后仍能完成一次自进化提案的 approve（端到端）。
- `pytest` 全绿。

## 6. 任务拆解

| 阶段 | 任务 | 依赖 | 验收 |
|---|---|---|---|
| **P0** | `memory/visibility.py` + 检索过滤 + 单测 | 无 | A 验收三条 |
| **P1** | `agent/approval_dialog.py` + 私聊拦截 + **管理员授权/撤销**（§3.6） | P0（读 SEALED 时需 A） | B 验收四条 + §3.6 验收五条 |
| **P2** | `ambient_listener`（只观察，dry_run） | 无 | 出站计数为 0 |
| **P3** | `banter` 生成 + 预算 + 熔断 | P2、reply_learning | C 验收三条 |
| **P4** | 影子期结束，关 dry_run，逐群观察 | P3 | owner 确认 |
| **D1** | 审计事件名迁移 + `ci_dod` 去 webui 依赖 | P1 | 无 webui 字样的构建门 |
| **D2** | 物理删除 ①②③ + config/schema/测试清理 | D1 | 5.5.4 四条 |
| **D3** | 生产下线 8088/8443 + 配置同步 | D2、部署窗口 | 端口无监听 |

## 7. 决策记录

- **D1** 交付方式：先设计评审，再实施。（owner 选）
- **D2** B 安全边界：仍走审批，入口搬进私聊自然语言。（owner 选）
- **D3** C 启用范围：全局开关默认开。（owner 选；本设计以 §4.3 三条护栏作为该选择的前提条件）
- **D4** 水群不靠放宽 `reply_mode`，走独立旁路。（设计判断，理由见 §1）
- **D5** 剔除范围：①②③ 全删，彻底不要 Web 界面。（owner 选）
- **D6** 审批机制保留、入口迁到私聊；D 依赖 P1，webui 不得先于私聊审批删除。（owner 选）
- **D7** 删除方式：代码删除 + 部署时同步改配置，一次做完不留半截。（owner 选）
- **D8** 水群模型：`gemini-3.5-flash-lite`，经 `module_models.banter` 路由。（owner 选）
- **D9** admin 不得再授权他人，授权为 owner 专属。（owner 选）
- **D10** 授权永久有效，无 TTL，靠手动撤销。（owner 选）

## 8. 实施记录（2026-10-29）

**最终范围：A + B + D。C（水群）已按 owner 要求整体撤销（2026-10-29）。**

| 线 | 状态 |
|---|---|
| A 记忆三级可见性 | ✅ 已落地 |
| B 私聊审批 + 管理员授权 | ✅ 已落地 |
| C 水群旁路 | ❌ 已撤销（代码全删，零残留） |
| D 剔除 WebUI | ✅ 代码已删（生产下线待部署） |

### 新增文件

| 文件 | 作用 |
|---|---|
| `anima/memory/visibility.py` | 三级可见性纯函数（public/private/sealed） |
| `anima/agent/approval_dialog.py` | 私聊自然语言审批 + 管理员授权/撤销 |
| `anima/cognitive/banter.py` | 水群治理器：预算、间隔、熔断、dry_run |
| `anima/cognitive/ambient_listener.py` | 群对话流观察器（只观察不发言） |

### 修改文件

| 文件 | 改动 |
|---|---|
| `anima/memory/vector_memory.py` | `add(visibility=)`；`search(channel=)` 过滤（拆出 `_search_raw` 保持旧行为） |
| `anima/pipeline/stages/memory.py` | 写入按通道定 visibility；检索传 channel |
| `anima/core/config.py` | 新增 `BanterConfig`，注册进 `AnimaConfig` |
| `config/default.yaml` | 新增 `banter:` 段（`dry_run: true`） |
| `anima/core/anima_core.py` | 实例化 `ambient_listener` / `banter_governor` |
| `anima/core/autonomy.py` | `count_message` 接水群反馈；心跳接 `_banter_tick` / `_banter_speak` |
| `anima/llm/handler.py` | 私聊前置拦截审批；群消息喂观察器 |

### 关键设计修正（实施中发现）

**水群与既有主动发言是两条互补路径，不是同一件事**：

| | `autonomy._speak`（既有） | `_banter_tick`（新增） |
|---|---|---|
| 触发时机 | 群里**冷场够久**（`_pick_group` 要求沉默 ≥ `active_silence_min_sec`） | 群里**正在热闹聊** |
| 目的 | 打破沉默 | 参与对话 |
| 预算 | `_group_speak_count` / 日预算 | `BanterGovernor` 独立预算 |

两者各自独立预算，互不占用。这是实施前未预见的分叉，写在这里备查。

### 测试

- `tests/test_memory_visibility.py` — 8 passed
- `tests/test_approval_dialog.py` — 34 passed
- `tests/test_banter_guardrails.py` — 18 passed
- 全量回归：6897 passed / 11 failed，11 条**全部为改动前既存失败**（已用 `git stash` 基线对照逐条确认）：
  - `test_crossgroup_injection` ×2（既存）
  - `test_document_text` ×2（缺 python-docx/openpyxl）
  - `test_module_import_health` ×1（子进程 sys.path 问题）
  - `test_sylann_adapter` ×3（既存）
  - `test_vector_health_reporting` ×3（本机无 sqlite-vec）
- `tests/test_reply_continuity.py` 在本机**基线同样卡住**（跑至第 4 例无输出），已排除出全量回归。

### D 线实施细节（2026-10-29）

**已删除**

```
anima/webui/          4998 行 Python + static 构建产物
web/                  anima-console 独立服务（root 运行，8443）
webui/                Vue 前端源码 + node_modules
scripts/run_webui_preview.py
tests/                17 个纯 webui 测试文件
```

**已改造**

| 文件 | 改动 |
|---|---|
| `core/anima_core.py` | 删除 webui 启动/关闭钩子与 `self.webui` 字段 |
| `core/config.py` | 删除 `WebUIConfig`、`webui` 字段、`ANIMA_WEBUI_TOKEN` env 通道 |
| `core/config_drift.py` | 删除 webui.enabled/host/port 三项漂移检测 |
| `observation/audit_analyzer.py` | `webui_task_rejected`→`owner_task_rejected`；`webui_evolution_rejected`→`evolution_rejected` |
| `evolution/ci_dod.py` | 删除 `frontend` 段（DoD 只剩 backend/security/evidence） |
| `config/default.yaml`、`production.example.yaml`、`deploy/staging-config.yaml` | 删除 `webui:` 段 |
| `.github/workflows/anima-ci.yml` | 删除 frontend job，`needs` 收敛为 backend/security |
| `scripts/ci_evidence.py` | 删除 `--frontend-built` 与 frontend_build 阶段 |
| `AGENTS.md` | 运维说明改为「无 WebUI，审批走私聊」 |

**测试适配**：`test_config_drift`(6) / `test_llm_usage`(6) / `test_full_scan_m1_security`(7) / `test_d07_secret_env_channels`(7) / `test_capability_dispatch_m07_da3`(15) / `test_ci_dod`(6) / `test_module_import_health` 全部绿。

**生产止血（已完成）**：`/opt/anima/config.yaml:373` 由 `0.0.0.0` 改为 `127.0.0.1`，重启后 `ss` 确认 8088 只监听回环；从内网访问返回 000。备份在 `config.yaml.bak.<ts>`。

### D 线验收时发现并修复的两个缺口（2026-10-29）

**缺口 1：授权申请没有任何通知出口**

`GrantQueue` 原先只有定义、**零调用点**。陌生人私聊请求受限功能时，owner 完全收不到通知——
"授权"在体验上等于不存在，必须 owner 主动猜"谁想用"。

已补 `LLMHandler._request_grant_notice`（`llm/handler.py`）：
- 非 owner 私聊 → 登记 GrantQueue + 私聊 owner
- 通知里直接给出可复制的「授权 <QQ>」
- 同一申请人 10 分钟内只通知一次（防刷屏，但首次必达）
- 测试：`tests/test_grant_notice.py`（4 passed）

**缺口 2：`permissions_override.json` 会整体覆盖 `config.yaml` 的 admins**

实测：override 文件一旦存在，运维后续在 `config.yaml` 手加的管理员会在重启后**被静默丢弃**。
这是 `core/permissions.py` 的既有设计（`_apply()` 直接整体替换），不是本次引入，
但删除 WebUI 后它成了唯一入口，风险等级上升。

**运维铁律（必须遵守）**：一旦用过私聊授权，**`config.yaml` 的 `admins` 就不再是权威**，
新增/删除管理员只能走私聊「授权/撤销授权」。要回到 config.yaml 管辖，需先删
`data/permissions_override.json`。

## 8.5 生产部署记录（2026-10-29）

### 部署

- 提交 `1894c3c8aa4d`，`dirty: false`，版本 0.3.0rc2
- `_deploy.py` 第一次 `EXIT=1`：冒烟探针仍请求 `8088/api/health`（随 WebUI 删除而消失），
  30s 超时误判整次部署失败——**服务其实是好的**。已改为不依赖 WebUI 的证据链
  （systemd active + 「OneBot11 WS 已连接」+「failed=0」+「自主心跳已启动」+ 确认 8088 已消失），
  第二次 `EXIT=0`。
- 生产机三个 webui 目录已删；`anima-console` 服务已 `disable --now` 且 unit 已移除。

### 生产验收

| 项 | 结果 |
|---|---|
| anima 服务 | active |
| 业务模块 | ok=33 / failed=0 |
| QQ 连接 | OneBot11 WS 已连接 |
| 8088 / 8443 | 均无监听（内网访问返回 000） |
| 生产 config `webui:` 段 | 已删除，YAML 46 键校验通过 |

### 期间发生的独立故障（与本次改动无关）

18:58–19:39 期间 LLM 全量失败（`transport:ClientOSError` / `ServerDisconnectedError`），
bot 回落到既有兜底话术（「本喵这边刚卡了一下」）。

诊断：ping 0% 丢包，TCP 握手成功但服务端立即空回复（0.0017s）；经 mihomo 代理访问
返回正常 401，从阿里云直连也正常 401。⇒ **CPA 侧按源 IP 临时拒绝 VPS 直连**，
疑似 fail2ban 类自动封禁。19:39 自行恢复（响应时间从 0.0017s 变为 1.5s）。

教训：这台 VPS 的 LLM 依赖一条**会按源 IP 熔断的外部链路**，且 `anima.service` 未配代理。
若再次出现「瞬间 000」，先测 `curl -x http://127.0.0.1:7890` 做对照，再去 CPA 查访问控制。

## 10. self_code 下线与 Skill 替代评估（2026-10-29）

### 10.1 下线依据（生产实证）

| 项 | 实测 |
|---|---|
| 调用次数 | 5 次（2026-10-16 ~ 10-26） |
| 成功进入待审批 | **0 次** |
| 已安装工具 | 空（`selfcode_stats.json` = `{"tools": {}}`） |
| workspace 产物 | 17 个 `.py`，**全部**卡在静态审查 |

拒绝原因是同一个死结：

```
selfcode 受理期拒绝（需要网络出口）
静态审查：禁止导入 aiohttp → 改用 urllib → 禁止导入 urllib → 改用 httpx → 禁止导入 httpx
```

**模型的意图（做网页抓取工具）与静态审查的禁令直接冲突**，且这个矛盾从未解开。
叠加 `self_code.anthropic_api_key` 为空、`claude` CLI 虽有但未配 key，
这条链在任何维度上都不具备可用性。

### 10.2 下线范围

| 位置 | 处置 |
|---|---|
| `tools/builtin/__init__.py` | 摘除 `self_code` 工具注册（`danger=True` 工具降至 1 个） |
| `core/registry.py` | 摘除 `selfcode` ModuleSpec（模块数 33 → 32） |
| `cognitive/danger.py` | 自升级提案不再转工程任务，只记审计事件 `self_improvement_proposal` |
| `llm/commands.py` | `!anima selfcode` 只回一句「已下线」 |
| `cli.py` | 不再加载已安装运行时工具 |
| `core/autonomy.py` | `evaluate_tools` 保留但恒空 |

`anima/cognitive/selfcode.py` 本体**保留未删**——它是可回滚点，且无注册后不会被调用。

### 10.3 Skill 替代评估（结论：已有实现，无需新建）

调研 Memoh 的 skill 机制（`internal/skills/`）后对比，**现有 `anima/skills/` 已经覆盖且更严格**：

| 能力 | Memoh | 现有 `anima/skills/` |
|---|---|---|
| 声明式格式 | `SKILL.md`（YAML frontmatter） | `skill.yaml`（pydantic `SkillManifest` 强校验） |
| 执行类型 | 纯指令（模型自己照做） | `prompt` / `http_get` / `tool` 三种 |
| 权限上界 | 无显式模型 | **机械保证**：`max(技能等级, 目标工具等级)` |
| 危险工具组合 | 无限制 | `danger` / `capability_only` / `agent_` 前缀一律拒绝 |
| SSRF 防护 | — | `http_get` 只接受固定地址，不提供请求头/方法/重定向 |

Memoh 的 `skill-creator` 值得借鉴的是**工作流约束**：用 `init_skill.py` 脚手架 +
`quick_validate.py` 校验器保证产物合法，不指望模型自觉。

**建议**（未实施，待 owner 决定）：
1. 给 `anima/skills/` 加一个 `skill-creator` 内置 skill，教模型按 workflow 产出 `skill.yaml`
2. 加一个校验命令（复用 `SkillManifest` 的 pydantic 校验即可）
3. 把上述接进 `owner_agent` 的规划提示，让「做个 XX 能力」落到 Skill 而非代码

**这条路的优势**：不执行代码 ⇒ 无沙箱联网矛盾；声明式 ⇒ 可审计；已有权限上界保证。

## 9. 未决

- `hourly_budget` / `min_gap_sec` 的初值需要影子期数据校准