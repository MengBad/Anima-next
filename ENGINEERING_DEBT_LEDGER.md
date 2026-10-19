# 工程债台账（ENGINEERING DEBT LEDGER）

> **本文档是唯一的工程债台账**（审查 P1-5：此前约 25 项未决项散落在 7 份文档里，
> 无单一入口）。建立于 2026-09-18。
>
> ⚠️ 与 `DESIGN_COMMITMENT_TRACKING.md`（原名 `DESIGN_DEBT_TRACKING.md`）区分：
> 那一份是**功能设计文档**（承诺追踪系统 —— 让 bot 记得自己欠了谁什么），
> 名字里的 "debt" 指**人际承诺**，不是工程债。两者无关，易混，故已改名。
>
> **状态图例**：`OPEN` 未处理 · `FIXED` 本轮已修 · `VERIFIED-NON-ISSUE` 经实测证伪 ·
> `NEEDS-OWNER` 卡在 owner 决策 · `BLOCKED` 依赖他项 · `SEALED` 明确不做
>
> **登记纪律**：一条债必须带 (a) 可复现的证据锚点（`文件:行号` 或实测命令），
> (b) 验收口径，(c) 来源。**无法给出验收口径的条目不允许进台账**。

---

## 0. 统计（2026-09-18，Phase A–D + E1–E8 执行后）

| 状态 | 条数 | 条目 |
|---|---|---|
| `FIXED` | 30 | D-01/02/03/04/05/06/07/08/09/10/11/13/15/18/19/20/21/23/24/25/26/27/28/**32/34/35/36/38/39/40** |
| `VERIFIED-NON-ISSUE`（实测证伪） | 2 | D-12（数据轮转）、D-22（context 预算=0） |
| `DECIDED`（口径裁定，非代码改动） | 1 | D-14 |
| `DEFERRED`（显式挂起，含重启前置条件） | 1 | D-16 |
| `CLOSED-AS-UNDERSTOOD`（归真后判定"不改逻辑"） | 1 | D-17（游离物：环境级阻塞、零影响、处置已定）·（D-27 经 **owner 显式授权**改判 `FIXED`：抬至 11000，见下） |
| `CONFIRMED-PENDING-DEPLOY`（已确证，待部署窗口执行） | 0 | ——（D-21 已于 09-18 部署窗口执行，见下） |
| `PARTIALLY-CLOSED` | 1 | **D-33**（复读：已改判——真病灶是 D-35 假债闭环；本轮改为只检测不重生成） |
| `OPEN` | 1 | **D-37**（规划读无人产出的文件） |
| `NEEDS-OWNER` | 0 | ——（D-07 已于 2026-10-17 完成外置 ⇒ 改判 `FIXED`） |
| `SEALED` | 3 | D-29（接线断言）、D-30（数值 trait 通道）、D-31（`bored` 死枚举） |
| **合计** | **40** | |

> 🔴 **2026-10-18 自主批次更新**（owner「123全修」）：新增 **D-32 / D-33 / D-34** 三张工单，
> 均一轮内 `FIXED`（代码 + 判据 + 全量回归 + 生产部署）。三者共用同一类病因 ——
> **「模型看不到自己刚说过什么 / 不知道现在几点」**，因此修法一律落在**确定性后处理**或
> **运行时事实注入**，不再往 prompt 里加静态例句（U2 纪律）。

> 🔴 **2026-10-17 自主批次更新**（owner「计划内容全部完成」）：D-13 层一 (a)(b) 达成 ⇒ 改判 `FIXED`；
> 其**层二（数值 trait 通道）**与 **`bored` 死枚举**经裁定 `SEALED`（**新增 D-30 / D-31**）；
> D-17 游离物经复核为**环境级阻塞（Windows `WinError 5`）、零影响**，转 `CLOSED-AS-UNDERSTOOD`；
> **D-07 明文密钥全部外置完成 ⇒ 改判 `FIXED`**。
> ⇒ 台账**无 `OPEN` 项、无 `NEEDS-OWNER` 项、无 `PARTIALLY-CLOSED` 项**。

> 🔴 **E 轮新增一条 `VERIFIED-NON-ISSUE` 式更正（D-27）**：台账原写"`capabilities` 段
> **从不进** prompt / 100% 被丢弃"，经生产落盘计数器实测**证伪**（65 天 1346 次请求、
> 1028 次片段丢弃 = 76.4%；09-01～09-16 连续 16 天丢弃为 **0**）。
> ⇒ 该条目**原定的两个处置（抬阈值 / 删片段）都是行为变更**，前提既不成立，
> 故改判 `CLOSED-AS-UNDERSTOOD`：**只归真，不改逻辑**。详见 D-27。

> 🔴 **本轮两条 `VERIFIED-NON-ISSUE` 都是"原判定被自家取证反驳"**：
> D-22 的掩码产物本身就证明值不是 0；D-12 的体积增长本就落在既定上限内。
> **教训**：结论要落在锚点（上限常量 / 掩码语义）上，不能停在"数字在变大"。**

---

## D-01 · 生命链第一条边是替身 —— `FIXED`（本轮 / Phase B1）

- **来源**：审查 P0-1。
- **证据（修复前）**：`anima/core/autonomy.py:343-355` 现场合成 `_C.Memory`（2 个 mood
  标量 + 常量 `relation_depth=0.2`）喂 `derive_identity`；水合的宪法 Memory 唯一读者
  是 `anima_core.py:296` 且只为计数。
- **处置（2026-09-18）**：
  - `_constitution_drive(..., memory=None)` 新增 `memory` 入参；`memory is not None` 时
    走**真实记忆的只读快照**，仅在调用方显式传 `None` 时才退回替身。
  - `Heartbeat._constitution_drive_snapshot()` 改为优先读
    `core.memory_authority.snapshot_memory()`（读失败/缺失才退替身）。
  - 返回体新增 `memory_source`（`live` / `live-empty` / `synthetic`）、`record_counts`、
    `identity_traits`，并写入 `proactive_expression_drive` 审计事件 —— 让"是否真接上"可审计。
- **验收口径（已达成）**：`f(Memory)` 入参是真 store 快照；守卫测试证明入参非伪造。
  - `tests/test_constitution_drive_wiring.py`：**10 passed**。
  - 回归子集（`-k autonomy|constitution|drive|heartbeat|proactive`）：**172 passed**。
  - **RED 证明**（回归注入，`.workbuddy/tmp/b1_red_check.py`）：
    ① 注回修复前实现 → 7/7 必需守卫变红；
    ② 注入「签名收 `memory` 但吞掉不用」→ 5/5 必需守卫变红（含**语义载荷证明**：
    同一 perception 下带 trait 的真记忆激活 Drive、空记忆不激活）。
- **⚠️ 语义后果（必须与修复一起读）**：接真记忆**不是让链变活，而是让它诚实变弱**。
  水合只产 `episode`，而 `derive_identity` 只把 `semantic` 数值字段聚成 traits
  ⇒ 生产形状 `identity_traits == 0`、`need.urgency == 0`，Drive 激活退化为只由
  Perception(novelty) 决定（`novelty > 0.75` 才越过 0.3 激活门），而修复前替身
  自带 semantic 使 `need.urgency` 恒为 `1.0`（**激活门被顶开**）。
  ⇒ **若在 D-13 闭合前翻转 `drive_gate=True`，主动表达会被显著压制（fail-closed）**。
  生产 `drive_gate` 现为 `False`，故本轮对线上行为**零影响**。
- **后续（原"未闭合项"）**：D-13 已于 09-18 后段达成口径 (a)（真实 producer 已接上），
  但**第二层（数值 trait 通道）仍未闭** ⇒ 生命链目前是"**已接通、仍不活**"。
  ⇒ 上面那条"不得翻转 `drive_gate`"的结论**依然成立**，见 D-13 末节。

## D-02 · 治理文档声明与生产实态不符 —— `FIXED`（本轮）

- **来源**：审查 P0-2。
- **证据**：`MAINTENANCE.md` 治理开关表原先只有"默认"列（全部 `false`）；
  `docs/governance/M0.4.7-CLOSURE.md:84`「never flipped to `True`」；
  `docs/governance/M0.4.8-FINAL-SCOPE-CONFIRMATION.md:19`「remains at default `False`」。
  生产实测四项为 `true`（`capability_mode` / `capability_gateway_mode` /
  `governance_audit_enabled` / `python_exec_enabled`）。
- **验收口径**：三处声明改为「默认 vs 生产实配」两列/附事后核查注记，并保留原文不复写历史。
- **状态**：本轮已改（`MAINTENANCE.md` 治理开关表 + 两处治理文档追加事后核查注记）。

## D-03 · 生产配置不在版本控制 —— `FIXED`（部分，模板化）

- **来源**：审查 P0-3（根因机制）。
- **证据**：`git check-ignore -v config.yaml` → `.gitignore:11`。
- **处置**：建立 `config/production.example.yaml`（脱敏键集模板）+ `deploy/check_config_drift.py`。
  生产 `config.yaml` **仍不追踪**（含 6 处明文密钥，见 D-07）。
- **验收口径**：`python deploy/check_config_drift.py` 报「未登记键 = 0」。
  实测：以生产真实键集运行 → **0**。

## D-04 · 3 天工作未提交 —— `FIXED`（本轮）

- **来源**：审查 P0-4。
- **证据**：`git status` 18 项未提交；最后提交 `a825863` 停在 2026-09-15。
- **验收口径**：按语义分组提交并推送；`git status` 干净。

## D-05 · F8 索引引用了仓库中不存在的子文档 —— `FIXED`（E7：建原因归类表）

- **来源**：审查 P1-5 / §2.6。
- **证据**：`capability_contracts/F8_GOVERNANCE_INDEX.md:34-50` 列 F8.1–F8.12 子文档，
  仓库中不存在。
- **验收口径**：索引逐项标注「在库 / 不在库」，不在库者写明原因（未产出 / 已合并 / 已删除）。
- **状态**：本轮按"诚实表述"处理（见 A5）。

## D-06 · `config/default.yaml` 已过期（实测缺 **10** 段）—— `FIXED`（E6）

- **来源**：本轮新发现（对比生产键集时暴露）。
- **证据**：代码中存在但 `config/default.yaml` 无对应段的 7 个：
  `debt_tracking`(config.py:669) · `memory_compression`(:645) · `owner_browser`(:630) ·
  `proactive`(:597) · `proxy_mgmt`(:825) · `react`(:683) · `task_agent`(:615)。
- **后果**：照 `default.yaml` 抄一份新配置会**静默丢掉**这 7 段（值走代码默认，不报错）。
- **验收口径**：7 段补齐到 `default.yaml`，且 `AnimaConfig` 全字段与 `default.yaml` 键集
  一致（可加一条测试钉住）。

## D-07 · 生产 config.yaml 含明文密钥 —— `FIXED`（2026-10-17：全部外置到 `EnvironmentFile=/opt/anima/.secrets`）

- **来源**：本轮新发现。
- **证据**：`llm.api_key`(48) · `vector.embedding_api_key`(51) · `vector.rerank_api_key`(51) ·
  `webui.token`(40) · `tavily.api_key`(58) · `x_browser.password`(13)。
- **分类**：
  - 已有 env 注入通道（可直接外置，`config.py:1019-1032`）：`llm.api_key` /
    `llm.endpoint` / `vector.embedding_api_key` / `vector.rerank_api_key` / `tavily.api_key`。
  - 🔴 **无 env 通道**（需代码改动）：`webui.token` · `x_browser.password`。
- **验收口径**：`deploy/check_config_drift.py` 第 4 节「明文密钥 = 0 处」。
- 🔴 **2026-10-17 完成（owner「计划内容全部完成」）**
  - **代码**：`anima/core/config.py::load_config` 补两条 env 通道
    `ANIMA_WEBUI_TOKEN → webui.token`、`ANIMA_X_BROWSER_PASSWORD → x_browser.password`
    （此前这 2 处无通道，只能明文落盘）。判据 `tests/test_d07_secret_env_channels.py`（8 项）。
  - **外置**：`.secrets` 追加这 2 个变量（另 4 处早已在）；`config.yaml` 6 处明文清空为 `""`。
    全流程**在服务器本地处理值、不回传**；属主/权限全程保持 `anima:anima:600`（`999:989:600`）。
  - **验收（生产实测）**：
    - `deploy/check_config_drift.py` 第 4 节 ⇒ **「未发现（或均已外置到环境变量）」合计 0 处** ✅
    - `set -a; . /opt/anima/.secrets; set +a` 后跑 `anima doctor` ⇒
      **OK LLM endpoint 可达** + **OK 向量记忆后端正常** ✅
    - 重启后（MainPID 4177581 → 4178062，NRestarts=0）启动日志：
      **`LLM 已接入: gemini-3-flash`**、`向量记忆已启用`、`人格正本加载 OK`、Traceback/CRITICAL = 0。
  - 备份 `/opt/anima/_backup_d07_20261017_062938/config.yaml`（可原样取回）。
  - 🔴 **运维影响（必须记住）**：手动跑 `anima doctor` / `anima x-diag` 等 CLI 时，
    **`.secrets` 不在你的 shell 里**（`EnvironmentFile` 只注入 systemd 服务）
    ⇒ 会误报「LLM api_key 未配置（echo 模式）/ 向量记忆后端不可用」。
    **正确姿势**：`cd /opt/anima && set -a && . /opt/anima/.secrets && set +a && python -m anima.cli doctor`
    （`anima doctor` 本身读到的是**真实状态**：服务进程有 env ⇒ 一切正常）。
  - ⚠️ **未做（需你另批）**：**密钥轮换**。外置只改变存放位置，未改值；且本轮侦察时
    `webui.token` / `x_browser.password` 的值曾被打印进对话记录 ⇒ 建议排一次轮换窗口
    （改 `.secrets` 值 + 重启，config.yaml 无需再动）。

## D-08 · `ConfigDriftMonitor` 结构上抓不到重启期变更 —— `FIXED`（E3：基线落盘）

- **来源**：本轮新发现（顺着 D-02 追"为什么长期无人发现"）。
- **证据**：`anima/core/anima_core.py:109-110` 在开机时用**当次配置**建基线；
  `anima/core/autonomy.py:725` 每 tick 自检。而敏感开关只能经重启生效 ⇒
  变更在"下一次开机"时已被吸收进基线。实测：史上 `config_permission_drift` 审计 **0 条**。
- **验收口径**：二选一 —— (a) 基线改为**持久化**（首见即存盘，跨重启续用），
  或 (b) 在文档里明确写"本监控只能发现**运行期热改**（若有），不能发现重启期翻转"，
  并把跨重启追溯指望到 `config_service` 台账。**当前实现具误导性：接线了，看起来在盯，实际不会报。**

## D-09 · `python.exec` 已在生产激活，但文档与代码注释均称"未激活" —— `FIXED`（文档/注释）

- **来源**：本轮新发现（审查 P0-2 的**升级**：上一轮只查了 3 个开关，漏了第 4 个）。
- **证据**：生产 `python_exec_enabled: true` + `capability_gateway_mode: true` ⇒
  `anima/tools/__init__.py:602-620` 的 bootstrap **确实执行**，provider 已注册；
  路由条件 `:984-993` 满足 ⇒ 走 `_governed_capability_route`。
  而 `config.py:300-302`、`tools/__init__.py:600-601` 仍写「生产两者皆 False → 完全不可达」。
- **授权性**：**经 owner 授权**，代码留痕 `tools/__init__.py:193`
  「R5 PHASE 13 re-open (owner-authorized 2026-08-15)」。翻转时间上界 ≤ 2026-09-05 13:48
  （最早 `search.web` 路由事件）。
- **使用量**：**0**（resolution 16 条全 `search.web`；审计/journal 0 命中）。
- **验收口径**：两处代码注释与 `MAINTENANCE.md` 改为与事实一致。
- **⚠️ 残留风险（另立 D-10）**：空 `pilot_capability_allowlist` **不拦 python.exec**
  （该门只作用于 `capability_only` provider，`tools/__init__.py:977-979`）。
  任何"空 allowlist = 全拒"的表述都是错的。

## D-10 · 「空 allowlist = 全拒」是个错误心智模型 —— `FIXED`（E7：MAINTENANCE §3.5）

- **来源**：D-09 衍生。
- **证据**：`anima/tools/__init__.py:977-979` 的 pilot 门带 `capability_only` 前置条件；
  `:194-195` 明写 python.exec 不经该门。⇒ 空 allowlist 只拒 **pilot-only** 能力，
  不拒 mixed-mode / non-capability_only 能力（含 python.exec）。
- **验收口径**：在 `MAINTENANCE.md` / 治理文档中统一表述为
  「空 allowlist = 拒绝全部 **pilot-only** 路由」，并给出 python.exec 的真实门清单。
  （本轮已写入 MAINTENANCE 的"易误读点"，但治理文档侧尚未逐处核。**

## D-11 · 12 个生产废弃模块 + 潜在 ImportError 地雷 —— `FIXED`（09-18 部署窗口后：25 件封存）

- **来源**：审查 P2-3。
- **证据**：生产独有 12 个模块（`pipeline/stages.py` 2204 行死代码、`image_request.py`、
  8×`capability/runtime_*.py`、2×`webui/*_page.py`）；
  `anima/capability/runtime_evidence_integration.py:53` 引用已归档模块。
- **验收口径**：先修 `:53` 的引用，再删除/封存 12 个模块；删除前后各跑一次全量回归。
- **处置（2026-09-18）**：
  - RED 基线：孤儿扫描 rc=1 / `active_ref_count=28`；`pytest` 收集 **12 ERROR + Interrupted**。
  - 备份：`/root/d11-20260918/pre-d11-20260918.tar.gz`（124,380 B，sha256 `bedee68d…`，25/25 验证）。
  - **封存式**移动（可 reverse）至 `/root/d11-20260918/removed/`：12 模块 + 13 测试，共 25 件。
  - 四道门全过：生产残留 0/25；孤儿扫描 **rc 1→0、active 28→0**；
    `pytest` **ERROR 0（3970 可收集）**；重启后 pid `1804330`、零 Traceback、启动里程碑齐全。
  - 一致性门复跑：1077 文件、differ 0；「仅生产有」= **2**，即**有意保留**的 2 个
    `persona_core` 备份 yaml（判定为留档，不属孤儿）。
  - root 污染 0/0/0。
- **卡点（已解除）**：删生产文件属独立授权 —— **09-18 获 owner 显式授权后执行**。

## D-12 · 数据轮转 —— `VERIFIED-NON-ISSUE`（2026-09-18 实测证伪，附算术）

- **来源**：审查 P1-1（08-27 立项，称"数据无轮转"）。
- **❌ 原表述**：`health_history.json`（2.90 MB，+81%）与 `embedding_cache.json`（24.77 MB）
  被列为"无轮转、线性增长"。
- **✅ 实测（逐仓上限锚点 + 算术校验）**：

| 存储 | 已有上限（代码锚点） | 实测 | 算术校验 |
|---|---|---|---|
| `data/health_history.json` | `max_samples=2880` + 写入/恢复双处截断（`core/health_history.py:18,22,33,118`） | 生产 2.90 MB；本地 534 KB / 520 样本 | 2880 × ~1.03 KB/样本 ≈ **2.9 MB** ⇒ **正卡在上限**。08-27 的 1.6 MB 是当时每样本更小（计数器字段更少），**不是样本累积** |
| `data/embedding_cache.json` | `_MAX_CACHE_ENTRIES=8000` + 超限淘汰至 80% 水位（`memory/vector_memory.py:45,76-85`，load 与 save 双处调用） | 生产 24.77 MB | 8000 × ~3.1 KB（1024 维 float JSON）≈ **24.8 MB** ⇒ **正卡在上限** |
| `data/media_prepare_cache/` | `_DISK_CACHE_MAX_BYTES=128 MB`，超限清空（`media.py:41,103+`） | 1115 条 | 有硬上限 |
| `data/audit/*.jsonl` | `audit.prune(retention_days=90)`（`core/autonomy.py:935`） | —— | 90 天滚动 |
| `data/backups/` | `retention=30`（`core/backup.py:27`） | —— | 30 份滚动 |
| `data/anima.db` | `ForgettingEngine.forget_vector_memory` → `VectorMemoryStore.delete(rowid)`（`cognitive/forgetting.py:145`） | 生产 4.36 MB | 有评分式遗忘通道 |

⇒ **"数据无轮转"是误判**：主要存储**都有上限，且两个被点名的文件正卡在各自上限上**。

- **残留真实问题（性质不同：不是"无上限"而是"写法昂贵"）**：`embedding_cache.json` 是
  **明文全量 JSON、每次保存整文件重写**（单次 ~25 MB I/O），且淘汰按**插入序**而非真 LRU。
  这是性能/健壮性问题，**不是增长失控**。→ 若要改，属独立任务（换 SQLite/分片 + 真 LRU）。
- **验收口径（已达成）**：给出每个存储的上限锚点 + 算术校验，并纠正原文表述。
- **⚠️ 教训**：审查用"体积变大"推断"无轮转"是**未查上限锚点**的推断；体积增长与有上限并不矛盾。

## D-13 · Reflection → Memory 闭环无生产 producer —— `FIXED`（层一）
（层一 (a)(b) 均已达成；第二层「数值 trait 通道」经 2026-10-17 裁定 `SEALED`，另立 **D-30**）

- **来源**：审查 P1-2。
- **证据（修复前）**：`MemoryAuthority.ingest` 全库无生产调用者（仅 `invariants.py:28` 导入自检）；
  `constitution/invariants.py:17` 自称 "Run in CI / at boot" 但无 boot/CI 调用点。
- **验收口径**：二选一 —— (a) 接上真实 producer，或 (b) 在文档标注"未闭合"并把
  `invariants` 的 CI/boot 接线点删掉或实现。**不允许继续自称 Run in CI。**

### (b) 已达成 —— E2（09-18 前段）

`invariants` docstring 归真（显式写明 "no boot call site"）+ 5 条回归守卫钉死断层形状
（`tests/test_constitution_memory_closure.py`，含把该自称钉在真实 CI 调用点上的 meta 守卫）。

### (a) 已达成 —— 09-18 后段（本次）

- **新增** `anima/constitution/reflection_loop.py`（`ReflectionSedimenter`）= 真实 producer，
  只**复用**既有引擎（`reflect()` → `MemoryAuthority.ingest()`），不新增存储、不新增持久化路径。
- **接线点**：`anima/core/autonomy.py::Heartbeat._constitution_sediment_carrying`，
  由 `Heartbeat` 在**一次真实承载结束之后**调用（claim 到 `proactive_intent` 才算承载过）——
  输入是真实发生的 success/failure，**不是**现场合成的常量替身（与 D-01 同一条纪律）。
- **开关**：`autonomy.constitution_reflection_loop`，**默认 `False`**（生产行为不变）。
- **审计**：每次沉淀 emit `constitution_reflection_sedimented`
  （`status` / `records` / `identity_traits` / `evidence_ref`）⇒「闭环是否真的跑过」可外部核查，
  而不必靠读代码相信。
- **有界**：`MemoryEngine.ingest_episode` 无上限且不去重（`memory_engine.py:36`），
  故有界性由本层负责：RFC-0109 指纹去重 + 进程内条数上限（`DEFAULT_MAX_RECORDS=200`）。
  ⚠️ 去重粒度**比直觉粗**：`_fingerprint` 只哈希 `(plan_ref, status, outcome, lessons)`、
  **不含 timestamp**（`reflection.py:81-91`）⇒「同一群里同样的结果」永远算同一次经验，
  只会沉淀一条。这是引擎既定形状，本层**不对抗**（已用守卫钉死）。
- **验收**：`tests/test_constitution_reflection_loop.py` **13 passed**；
  constitution 四文件 **137 passed**；config/schema/drift 子集 **192 passed**；全量回归见部署窗口报告。
- **RED 证明**：① 本次改动前 `anima/` 全树不存在任何 `ReflectionSedimenter` / `.sediment(`
  调用点，meta 守卫 `test_production_call_site_exists` 必然失败；
  ② 首轮运行 **2 个测试变红**，抓出「误以为 timestamp 参与指纹」的错误假设 ⇒
  改的是**测试与文档**，不是引擎（`_fingerprint` 属 RFC-0109，不擅自改）。

### 🔴 第二层（仍未闭，需 owner 裁定）

**接通闭环 ≠ 链变活**。`reflect()` 产出的 semantic 记录只有**文本** `{"lesson": <str>}`
（`reflection.py:67`），而 `derive_identity` 只把 semantic 的**数值**字段聚成 traits
（`identity.py:35-36`）⇒ 闭环接通后 `identity_traits` **仍然是 0**、`need.urgency` 仍然是 0。
该事实已用守卫钉死（`test_loop_does_not_fabricate_numeric_traits`）。

⇒ **`autonomy.drive_gate` 的前置条件仍然不满足，不得翻转**（翻转 = 主动表达被静默压死）。
要让 traits 非空必须补一条**数值 trait 生产路径**，二选一：

1. 让 Reflection 真做 LLM 蒸馏并输出**数值自评** —— RFC-0109 目前只做 M1.9 启发式模拟，
   这属于引擎演进（Foundation Evolution F1 通道）；
2. 由本层 sediment 数值语义记录 —— 但「什么算 trait」是 **RFC-0101 Identity vs RFC-0103 Affect**
   的边界决策（mood 是 Affect，不是 Identity），**不可由执行层擅自定义**。

另：`reflect()` 对 `FAILED` **不产任何记录**（`reflection.py:52`），故失败的承载不留记忆 ——
本层如实返回 `filtered`，**不伪造**一条"失败记忆"。

## D-14 · F5 / M0.x 执行基建：**模块已接线 / 产物为零** —— `DECIDED`（E7：裁定 (b) 保持休眠）

- **来源**：审查 P1-3；口径经 2026-09-18 Phase C 实测**更正**。
- **❌ 原表述**：「F5 `runtime/` + `pilot/`（42 文件，**零生产足迹**）」—— 混淆了"模块"与"产物"。
- **✅ 实测（更正后）**：
  - **模块层**：共 **39** 个模块，其中 **31 个有生产（非测试）导入者**：
    `anima/cli.py:342-343`（`WorkflowRunStore` + `RecoveryModule` 在 `core.start()` 的 restore 循环注册）、
    `anima/tools/__init__.py:584,609,782`（`CapabilityRegistry` / `PythonExecPolicy`）、
    `anima/pipeline/stages/memory.py:105`（`pilot.conversation_evidence.hash_user_id`）、
    `anima/cognitive/sylann_suggestion_controller.py:32`（`pilot.safety_filter`）、
    `anima/capability/governance/gateway.py:38`（`runtime.capability_resolver`）、
    `anima/tools/capability_fs.py` / `capability_web.py`。
  - **产物层**：`data/{workflow_runs,execution_ledger,pilot,observation}` **全部不存在**（本机＋生产双查）。
- **结论**：**`SEALED`（封存/停维护）对该子集不是合法选项** —— 会直接打断生产入口的 import。
  正确状态是 **"已接线、未驱动"**：模块可达，但没有生产调用点驱动 `start_run` / ledger 落盘。
- **验收口径**：三选一由 owner 定：
  (a) 接一个真实 producer；(b) 文档写明"已接线未驱动"并停止在该面追加功能；
  (c) 逐模块判定收敛（`pilot/` 只能逐模块，因 `conversation_evidence`/`safety_filter` 已被生产使用）。
  **本轮执行 (b) 的文档侧** → `docs/RUNTIME_SURFACE_STATUS.md`。
- **卡点**：任何"接线"都会新建生产写入路径 ⇒ 需独立授权（**审计通过 ≠ 运行时授权**）。
- 🔴 **2026-10-17 只读复核（计划 Track 3.2 收口）**：产物层 `ls -d data/{execution_ledger,workflow_runs,pilot,observation}`
  **4 个目录仍全部不存在**（本地＋生产）；模块层仍承重（生产 `grep -c 'anima.runtime'`：`cli.py`=**2**、`tools/__init__.py`=**3**）。
  ⇒ 裁定 **(b) 保持休眠**继续有效，**无变化**。复核记录入 `docs/RUNTIME_SURFACE_STATUS.md §7`。
  **本面"接线还是封存"至此有明确书面决策：产物层不激活 / 模块层保留。**

## D-15 · 两个 `capability` 同名不同物 —— `FIXED`（E7：MAINTENANCE §3.5 三层口径）

- **来源**：审查 P1-4（`f9.1_contradiction_backlog:104` 的 N-01 升级为既成事实）。
- **证据**：抽查 journal 近 30 天 1063 行含 `capability` 的日志，**全部**来自
  `anima.tools.capability`（自生长能力层），治理关键字命中 **0**。
- **验收口径**：至少做到文档层命名消歧（治理面 `capability_layer.*` vs 自生长
  `capabilities.*` vs 组件 `anima/capability/`），并在排查手册加"看到 capability
  先确认是哪一层"。

## D-16 · RFC-0002/0003 已 APPROVED 但未实现 —— `DEFERRED`（E7：正式回退 + 重启前置条件）

- **来源**：审查 P1-7。
- **证据**：`docs/governance/RFC-0002-FINAL-SCOPE-CONFIRMATION.md` /
  `RFC-0003-FINAL-SCOPE-CONFIRMATION.md` 均为 APPROVED；代码未见 Task DAG / Workflow DSL。
- **验收口径**：二选一 —— 实现，或明确回退为 `Deferred`。当前状态是"批准了但没人做"，
  这在治理语义上等价于永久挂起，应当写明。
- 🔴 **2026-10-17 复核（自主批次，只读）**：本条的**裁定已存在**（`DEFERRED`，见标题与上段）。
  复核另发现**文档层仍四处不一致**：`docs/governance/M0.5-RFC-0002-APPROVAL-READINESS-REPORT.md`
  （称与已批准的 0003 对等）、`M0.5-RFC-0002-IMPLEMENTATION-CHECKLIST.md`（称 Slice 0 完成、待进实现）、
  `RFC-0002-FINAL-SCOPE-CONFIRMATION.md` / `RFC-0003-FINAL-SCOPE-CONFIRMATION.md`（称 APPROVED）。
  **未擅改这些文档** —— `docs/governance/M0.5-ROADMAP.md` 头部明写
  「**Status: APPROVED BASELINE (frozen — no further roadmap revision without explicit reopening
  by the reviewer)**」，其余为历史快照（"as of the production of the … Package"）。
  ⇒ **依冻结纪律，改它们须 owner 显式解冻**。**本条的权威实时状态 = `DEFERRED`（以本台账为准）**；
  文档层一致性属 owner 裁定（解冻后重述，或保持快照并在本台账留权威状态）。

## D-17 · 仓库游离物 —— `CLOSED-AS-UNDERSTOOD`（2026-10-17：48/65 已回收；17 项经复核为**环境级阻塞、零影响**，处置已定）

- **来源**：审查 P2-1。
- **实测清单（2026-09-18，`glob` 全量；E8 订正为 65 项）**：**62 项 / 共 1389.6 MB**，且**全部 `.gitignore` 已覆盖**
  （`git ls-files --error-unmatch` 逐项确认 **0 项被跟踪**）。⇒ 这不是"工作树脏"（`git status` 本来就是干净的），
  而是**文件系统层杂乱**。原报告"72 项"的计数偏高。
- **已回收 48 项 → `.workbuddy/tmp/trash-repo-clutter/2026-09-18/`**（`_MANIFEST.json` 留档，**未删除**，可原样取回；E8 补收 2 个根目录 `_step0_*.txt`）：

| 类别 | 项数 | 体积 |
|---|---|---|
| `.pytest-*` 临时目录 | 46（含 `tmp_*`/`MagicMock`/日志等） | —— |
| `tmp_astrbot_test/` | 1 | **1354 MB**（单笔最大） |
| `.pytest-r10/` | 1 | 20.5 MB |
| `.git.broken-20260915/`（git 事故留证） | 1 | 10.9 MB |
| 根目录 `_*.log` / `_deploy_out.txt` | 17 | ~0.2 MB |

- **❗ 未能回收 17 个**（E8 订正：原记 16）：16 个 `.pytest-tmp*` **空目录**（0 字节）+ **1 个 `MagicMock` 目录**。
  后者此前**未登记进 `_MANIFEST.json` 的 `failed` 清单**（`MagicMock` 在 `moved` 里有同名记录 ——
  那是 trash 里已成功搬走的那一份；**根目录还残留着第二份**，本轮实测才发现）。任何**打开/改名/删除**操作都返回
  `PermissionError [WinError 5]`（`os.listdir` / `os.rename` / `os.rmdir` / `shutil.move` 全部失败），
  但 `os.lstat` 可读：`mode=0o40777`、`attrs=0x10(DIRECTORY)`、**不是重解析点（reparse=False）**。
  属 OS 层 ACL/句柄问题，**未强删**（遵循既有 `.git` 事故纪律：`.git` 内删除同样报 `WinError 5`，严禁强删）。
  影响面：**零**（空目录 + 已被 `.gitignore:16,57` 覆盖 + 无功能引用）。
- **验收口径（部分达成）**：**48/65** 已移出工作树；剩余 **17** 个需在**普通终端/管理员**下处理，属环境权限问题。
  （65 = 原清单 62 + E8 补收 2 个 `_step0_*.txt` + 1 个此前未登记的根目录 `MagicMock`；48 + 17 = 65，账平。）
- **附带结论**：`tmp_astrbot_test` 一项即占 **97%** 的体积，是 2026-07 遗留的 astrbot 试验环境。
- 🔴 **2026-10-17 处置（自主批次）**：17 项经**本机复核**仍全部拒绝
  （16 个 `.pytest-tmp*` 连 `os.listdir` 都报 `PermissionError [WinError 5]`；`MagicMock` 非空）。
  实测结果：`目标 17 → 已删除 0 / 非空 1 / 仍阻塞 16`。
  **判定：环境级（Windows ACL/句柄）阻塞，非仓库问题** ⇒ 依既有纪律（同 `.git` 事故）**不强删**。
  影响面**复核定格为零**：全 0 字节或空 + `.gitignore` 覆盖 + `git ls-files` 无跟踪 + 无功能引用；
  且 **17 项均不存在于生产（Linux）**，纯本地 Windows 工作树残留。
  **处置结论**：`CLOSED-AS-UNDERSTOOD` —— **无需进一步动作**。如需清空，在**管理员终端**逐项 `rmdir`
  （16 个为空目录；`MagicMock` 须先确认内容）—— **不使用递归强删**（遵循既有纪律）。

## D-18 · `_TOOL_FAILURE_PREFIXES` 之外仍有一条待决语义 —— `FIXED`（E7：新增 COLLAPSE 表，三层语义）

- **来源**：2026-09-18 工具失败判定完整性审计（上一轮）。
- **证据**：`X 搜索异常` 前缀当前被判**成功**。该轮 47 处 fail-open **全部**是
  "文本明确自称失败"，唯独此条措辞算不算失败属**产品判断**。
- **验收口径**：owner 决定后一行改动即可。

## D-19 · `react.py` 触顶兜底话术在通用路径上不适用 —— `FIXED`（E5：去场景绑定词）

- **来源**：2026-09-18 占位串逃逸修复（上一轮）。
- **证据**：`anima/agent/react.py` 的 `_empty_content_fallback()` 里
  「换个具体视频链接（带 BV）再丢一次」出现在**通用**兜底路径 —— 用户问"天气/几点"
  时会得到"发个 BV 链接"的建议。
- **验收口径**：改为与场景无关的通用说法（属产品措辞，未擅自改）。

## D-20 · X 搜索额度 10/日 与"额度用尽=静默失败" —— `FIXED`（E1：额度落盘 + 显式日志）

- **来源**：既有运维发现。
- **证据**：`search_daily_limit: 10`；额度用尽表现为**毫秒级、零日志**的静默失败，
  且 `_daily` **只在内存 ⇒ 重启即重置**。
- **验收口径**：额度计数落盘（跨重启连续）＋ 用尽时写一条显式审计/日志。

## D-21 · 生产 `config.yaml` 文件权限 —— `FIXED`（09-18 部署窗口：实读 644 → `chmod 600`）

- **来源**：`reports/2026-08-27_vision_gap_plan.md` D2（当时报 644）。
- **验收口径**：实测权限；若仍为组/其他可读，`chmod 600` 并在 `_deploy.py`
  启动自检加权限断言。
- **处置（2026-09-18）**：实读确认原为 **644** → 已 `chmod 600`
  （owner/size/md5 **未变**，即内容未被触碰）；改回用 `_deploy.py --fix-config-perms`。
  重启后三服务 active、零 Traceback。

## D-22 · `context.*_token_budget = 0` 语义 —— `VERIFIED-NON-ISSUE`（原判断被自身取证反驳）

- **来源**：审查配置差异对比。
- **❌ 原表述**：生产把 `context.input_token_budget` / `system_token_budget` /
  `tool_result_token_budget` / `image_token_estimate` 全部显式设为 `0`。
- **✅ 实测证伪（用同一份生产掩码产物自证）**：掩码脚本 `.workbuddy/tmp/b0_prod_cfg.py:19`
  的掩码条件是 `SECRET_HINT.search(path) and val not in (None, "", 0, False, True)`
  —— 即**值为 0 时根本不会被掩码**，会原样打印成 `0`。而生产掩码产物
  `.workbuddy/tmp/prod_masked.yaml:73-77` 实际是：

  ```yaml
  context:
    image_token_estimate:    <REDACTED len=3>   # 3 位 → 768（= 代码默认）
    input_token_budget:      <REDACTED len=5>   # 5 位 → 12000（= 代码默认）
    system_token_budget:     <REDACTED len=5>   # 5 位 → ≠ 代码默认 8000（真实漂移）
    tool_result_token_budget: <REDACTED len=4>  # 4 位 → 1800（= 代码默认）
  ```

  ⇒ **四项都不是 0**。原判定的来源几乎可以确定是把同文件里 `autonomy.token_budget_per_day: 0`
  误读成了 `context.*_token_budget`（前者确实是 0）。
- **顺带发现（真实漂移，进 D-27）**：生产 `system_token_budget` 是**5 位数**，
  不等于代码默认 `8000`（`config.py:721` 亦带 `ge=1024` 下界，故**不可能是 0**——这一条
  独立地再次证伪了"设为 0"）。
- **验收口径（已达成）**：纠正记录 + 保留"具体数值以生产实测为准"的读法。
  **无需改任何逻辑**（原判定若成立本应改逻辑，现已证明前提不成立）。

## D-23 · `DESIGN_DEBT_TRACKING.md` 名不副实 —— `FIXED`（本轮改名）

- **来源**：审查 P1-5。
- **证据**：该文件内容是"承诺追踪系统"（bot 记得欠用户什么）的**功能设计**，
  与工程债无关；且已标 `[HISTORICAL] 停更于 2026-07-29`。
- **处置**：改名 `DESIGN_COMMITMENT_TRACKING.md`（git mv，保留历史）。

## D-24 · 六套并行阶段编号 —— `FIXED`（本轮建坐标）

- **来源**：审查 P1-6。
- **处置**：新建 `docs/PHASE_COORDINATES.md`，声明唯一坐标与两条命名纪律。

## D-25 · 四套愿景表述 + 许可证自相矛盾 —— `FIXED`（本轮收敛）

- **来源**：审查 P2-2。
- **证据**：`design/README.md:50` 写 `[MIT](../../LICENSE)`，而仓库 `LICENSE` 是
  **AGPL-3.0**，`README.md:7,409`、`AGENTS.md:57` 亦均写 AGPL-3.0-only。
  ⇒ 不是路线分歧，是**引用了本仓库的错误许可证**。
- **处置**：新建 `docs/VISION.md` 作单一权威；修正 `design/README.md` 的许可证；
  四份文档加定位指针。

## D-26 · 生产配置漂移的**人类可读**门 —— `FIXED`（本轮）

- **来源**：D-03 配套。
- **处置**：`deploy/check_config_drift.py`（只读，退出码可用于巡检/CI）。

## D-27 · 系统提示词预算长期顶格 —— `FIXED`（09-18 后段：**owner 显式授权**抬至 11000）

- **来源**：既有体检基线（09-18 计 38/38，tokens 均值 ~9534/10000）。
- **✅ 归因闭环（2026-09-18 生产只读实测）**：生产 `context` 的**精确取值**为
  `input_token_budget 12000` / **`system_token_budget 10000`** / `image_token_estimate 768` /
  `tool_result_token_budget 1800`（`config.yaml:297-301`）。
  那个 **10000** 正是体检里 "~9534/**10000**" 的分母 ⇒ 顶格是**阈值偏小 / 段本身偏大**，
  **与"0 语义"无关**（D-22 已被三重证伪：掩码规则、`ge=1024`、生产实读）。
- **原验收口径**：二选一 —— 抬 `system_token_budget`，或明确裁掉 `capabilities` 段
  （它 100% 被丢弃，等于白占构造开销）。**未确认前不改任何逻辑。**
- **⚠️ 裁定变更（本轮）**：E8 原判 `CLOSED-AS-UNDERSTOOD`（"只归真、不改逻辑"）已被 **owner 显式授权
  覆盖** —— owner 在 09-18 后段的三项授权中选择"抬预算"。这是**有意的行为扩张**，不是债的修复：
  抬升后系统提示词可以变长 ⇒ 单请求 token 成本与构造开销上升，属可接受范围内的取舍。
- **✅ 处置（2026-09-18 后段）**：
  - 生产 `config.yaml` → `context.system_token_budget: 10000 → 11000`（**只改生产**，不动
    `config/default.yaml` 的代码默认 8000）。
  - 备份 `/root/budget-20260918/config.yaml.pre-11000`；`load_config` 复核 `= 11000`；
    重启 pid `1807161`；零 Traceback；`config.yaml` 仍 `600 anima:anima`（md5 未因权限变动）。
- **📊 实测量化（`data/reply_quality.json`，权威口径）**：
  09-18 当日 `context_requests 46` / `context_fragments_dropped 27` = **58.7%**，
  `context_history_dropped 20`，`context_protected_over_budget 0`（09-17 为 **1**）；
  系统提示词均值 **440230/46 = 9570 tokens = 预算的 95.7%**。
  改动前的丢弃行取值为 `tokens=9931~9974/10000` —— **即使已丢弃，仍停在预算的 ~99%**，
  这正是"分母偏小"的直接证据，也说明 +1000 余量方向正确。
- **⚠️ 效果判定：方向性证据，**不**足以声称丢弃率已下降**。
  抬升后到 D-13 部署重启之间只有 **Δ2** 个 context 请求（0 次新丢弃）。
  **样本受限的原因已定位且不是故障**：`heartbeat_age_sec=25.4`（bot 活着），近 30 min 入站 43 条，
  但 `gate_skip_idle=143` ⇒ 绝大多数消息**未被点名叫它、被回复闸门正常拒答**，因此不构造 context。
  ⇒ **收口口径**：等一个完整流量日后重读 `context_fragments_dropped / context_requests`；
  该值若显著低于 09-18 的 58.7%，才算效果成立。

## D-28 · GitHub ↔ 生产无自动一致性门 —— `FIXED`（E8：交付 `check_github_prod_parity.py`）

- **来源**：审查 §2.2（09-17 人工融合 105 文件）。
- **证据**：生产 `/opt/anima` 非 git 检出（无 `.git`）；生产 450 py vs 本地 413 py。
- **验收口径**：提供一条只读比对命令（按 LF 归一化 blob-sha 比内容，用**行 diff 会被
  CRLF 噪声误导**），并纳入巡检。

## D-29 · 三个模块的接线断言缺测试 —— `SEALED`（本轮不接）

- **来源**：审查 §1.3 / §3。
- **证据**：多个模块头部标 `WIRED`（如 `MAINTENANCE.md:434` 称
  `memory_engine/memory_authority` **WIRED**），但缺"输入端非空"的证明。
- **处置**：本轮的机制守卫测试（Phase B）只覆盖**生命链**一条；
  其余 `WIRED` 断言的逐条取证列为后续独立任务，**本轮不做**，避免范围失控。

## D-30 · Reflection → Memory 的「数值 trait 通道」—— `SEALED`（2026-10-17 裁定：不接）

- **来源**：自 D-13 拆出（D-13 **层一 (a)(b) 已达成** ⇒ 改判 `FIXED`；层二即本条）。
- **证据**：`constitution/invariants.py` 与 `MemoryAuthority` 侧**无「数值 trait」生产 producer**；
  身份 trait 目前只有 `ReflectionSedimenter` 走的**定性**记录（emit 里带 `identity_traits` 计数），
  不存在"数值型 trait 通道"的写入方，也无读取方。
- **验收口径（二选一）**：(a) 接真实 producer；或 (b) 明确 `SEALED`，不得留"自称在跑"。
- **2026-10-17 裁定**：**(b) `SEALED`** —— 既无 producer 也无 consumer，接入属**新增能力**
  （非缺陷修复），且会改变身份漂移语义 ⇒ 归入架构演进（Track 5 / M0.5），**本轮不接**。
- **反向守卫**：`tests/test_constitution_memory_closure.py` 已钉死断层形状；将来若接 producer，守卫会提醒复核。

## D-31 · `bored` 是死枚举（有描述、有消费者，但无区间映射）—— `SEALED`（2026-10-17 裁定）

- **来源**：2026-10-16 E1 情绪可达性修复时连带发现（未入台账，本次补登记）。
- **证据**：`anima/cognitive/mood.py` 的 `_MOOD_DESCRIPTIONS` 有 `"bored"` 文案，
  但 `_MOOD_LABELS` **无对应区间映射** ⇒ `_compute_label()` **永不返回它**。
  而它被 **4 处**生产代码消费：`llm/handler.py`（`"bored":"speechless"` 表情映射）、
  `core/reply_splitter.py`（分割权重 1.3）、`pipeline/stages/guardrail.py`（预算 120）、
  `core/autonomy.py`（`mood_label in ("tired","bored")` 分支）⇒ **4 个消费分支永不可达**
  （"算了却没人用"的反面：**消费者在等一个不存在的值**）。
- **验收口径（二选一）**：补区间映射，**或** 显式标 `SEALED`。
- **2026-10-17 裁定**：**`SEALED`**（不补映射）——
  ① 补映射 = 让情绪系统**开始产生 bored 并触发那 4 个分支**，属**行为变更**（需 owner 定调情绪语义）；
  ② 4 处消费者都把 `tired`/`bored` 并列处理，**`tired` 已覆盖功能**，缺 `bored` **无功能损失**。
- **落地**：`tests/test_mood_label_reachability.py` 以 `NO_MAPPING = {"bored"}` 白名单**显式声明**；
  `test_mood_tables_key_consistent` 在两张表不一致时失败 ⇒ 想补映射**必先改白名单**（有摩擦，防手滑）。


---

## D-32 · 出站时段词与真实时间不符（23:56 说「大清早」）—— `FIXED`（2026-10-18）

- **来源**：2026-10-18 夜间出站人读（Track 2 复跑后的加测）。
- **证据（生产原文）**：`10-17 23:56` 群聊出站
  「哼，**大清早的**又丢这种椎名もた的歌单链接过来……」；而同一条注入的
  `{time_context}` 是「…周六 深夜11点56分」（`time_sense.time_description` 已改 12 小时制）。
  ⇒ 属「**链路给了、模型没听**」。P2（`persona_core.yaml:33` 删词列举）治不了它：
  删掉的是**示例**，模型是自己造的词。
- **验收口径**：`23:56` / `03:54` 两条生产原文经护栏后**不含**「大清早」；
  `06:00` 那条（本来正确的）**一字不动**；纠偏**幂等**。
- **修法**：`pipeline/stages/guardrail.py::fix_period_words` —— 出站确定性纠偏，
  **只换词、不删句、不整段改写**（吸收 09-27 分析体误杀的教训）。
  词表 `_PERIOD_WORD_WINDOWS` **显式允许跨夜**（大晚上 18→05、大半夜 21→05）。
- **判据**：`tests/test_guardrail_period_words.py`（20 项，含生产原文回放、幂等、边界、源码接线守卫）。
- **生产验收**：`/opt/anima` venv 内冒烟 `hour=23→大晚上 / 3→大半夜 / 6→无改动`；重启后锚点 `[时段纠偏]` 待首个错配样本。

## D-33 · 链接类回复「模板家族」复读 —— `PARTIALLY-CLOSED`（2026-10-18 09:05 复查降级）

> 🔴 **2026-10-18 09:05 复查：降级为 `PARTIALLY-CLOSED`。**
> 部署后首个真实样本（`08:57`，卡片链接，`[防复读] 已注入自身近期发言 4 条` 已命中）
> 发出的仍是**同一骨架**：
> `大清早的又甩这种变身美少女的B站链接过来……本喵才不看什么女友坐不住了，要看你自己看去喵！`
> 对照 `00:32` 的 `哼，又甩这种B站链接过来……本喵才不帮你看什么脑洞学生会呢，要看你自己看去喵！`
> ⇒ 去掉标点后**头 14 字与尾 8 字逐字相同**（`又甩这种…链接过来…本喵才不… / 要看你自己看去喵`），
> `difflib` ratio 仅 0.690（实体不同压低了比值，但它**结构性完全相同**）。
> **结论：注入口通了（数据侧 + 端到端判据都对），但"模型会不会听 prompt"这一层没解决。**
> 与 D-32 是**同一类教训的第二次**：模型不听 prompt 时，只有**出站确定性处理**能兜住。
> ⇒ 建议下一步（待 owner 定）：在 `guardrail` 加**骨架重复检测 + 单次有界重生成**（命中才多一次 LLM 调用），
> 或降级为"只检测报警不重生成"。

> 🔴 **2026-10-18 09:30 二次改判：本条的"注入自身原句"修法已撤回。**
> 根因在 **D-35**（承诺抽取器把拒绝判成承诺）；原句注入不仅无效，反而是**模板供给**。
> `cognition` 的 `self_recent_lines` 与 `autonomy` 的 `_prior_block` 均已删除；
> `recent_bot_lines()` 保留但只供 `guardrail` 的 `[骨架复读]` **检测**（不喂 prompt）。

- **来源**：同轮出站人读。
- **证据（三条同骨架，6h 内）**：
  ① `10-17 23:56`「又丢这种椎名もた的歌单链接过来……要听你自己听去喵」
  ② `10-18 00:32`「又甩这种B站链接过来……要看你自己看去喵」
  ③ `10-18 06:00`「又丢这种老番重制的链接过来……要看你自己看去喵」
  ⇒ 用严格等值/近似匹配**抓不到**（对象与动词都在变），只能按**句式骨架**看。
- **根因**：`chat_memory.observe_group_bot_reply` 只写 **15 分钟 TTL** 的短时缓冲，
  跨小时即查不到 ⇒ **模型完全看不到自己刚说过什么**。`persona_core.yaml` 里也不该放
  「别用这几句话」的静态清单（U2 明令：prompt 列举具体词 = 教模型照搬）。
- **验收口径**：群聊回合的 system 里出现 `self_recent_lines` 片段且内容为**模型自己最近的
  出站正文**（非人工例句）；同群 24h 内 bot 出站 < 2 条时不注入。
- **修法**：`ChatMemory.recent_bot_lines()`（新增，从**持久化** (群,用户) 桶聚合 bot 裸正文）
  + `pipeline/stages/cognition.py` 注入 `self_recent_lines`（priority 60，仅群聊）。
- **判据**：`tests/test_self_recent_lines.py`（9 项：只取 bot、时间倒序、去重、群隔离、
  TTL 剔除、片段接线与群聊门控守卫）。
- ⚠️ **残余**：属于**降低概率**而非硬保证（模型仍可能顶着指令复读）。**该残余已在首个真实样本上被证实发生**
  （见条目头部 09:05 复查）⇒ 本轮修法判为**层一（数据/注入口）达成、层二（行为）未达成**。
  复测口径 = 「模板家族率」（`reports/2026-10-17_track2_24h.md` §四 的骨架正则），下一轮 Track 2 复跑时量。

## D-34 · 议程跟进与既往表态自相矛盾 —— `FIXED`（2026-10-18，经 D-35 修法落地）

> 🔴 **2026-10-18 09:30 更正根因**：不是"看不到历史"，而是 **D-35** ——
> 议程 `text_intent` 里塞着那句**拒绝原文**（「当时的回复是：…本喵才不帮你看…」），
> 模型只能把它改写成"帮了"。D-35 修好后议程不再携带拒绝原文，
> 本条原提的 `_prior_block` 注入**已撤回**（同样是给模板）。

- **来源**：同轮出站人读（同一条链接两次表态相反）。
- **证据（生产原文）**：
  `10-18 00:32` 回复同一链接：「本喵**才不帮你看**什么脑洞学生会呢」
  → `10-18 06:32` 议程跟进：「**勉为其难帮你瞅了一眼**，结果看到一半差点睡着」
  ⇒ 群友读到就是"这猫在编"。
- **根因**：`autonomy._speak_locked` 的 `intent` 分支**只拿得到 `text_intent`**，
  不带任何"我上次怎么说的"；而非 intent 分支至少有 15 分钟群上下文。
- **验收口径**：`intent` 路径的 user_request 末尾出现**自身近期发言**（≤3 条 × 60 字），
  且**非 intent 路径不得注入**（那里本来就防抢话，不需要）。
- **修法**：`core/autonomy.py::_speak_locked` intent 分支追加 `_prior_block` + 锚点
  `[议程防矛盾] 已注入自身近期发言 n 条 group=…`。
- **判据**：`tests/test_self_recent_lines.py::test_intent_followup_carries_prior_lines`
  （源码位置守卫：必须落在 `if intent:` 之后、普通接话分支之前）。



---

## D-35 · 拒绝句被承诺抽取器判成「承诺」（假债 → 假议程 → 回灌 prompt）—— `FIXED`（2026-10-18）

- **来源**：D-33/D-34 的根因追查（owner「完全理解问题发生的原因」）。
  报告：`reports/2026-10-18_root_cause_self_imitation.md`
- **证据（生产实测，一句拒绝的放大链）**：
  bot 出站「…本喵才不**帮你看**什么脑洞学生会呢，要看你自己看去喵！」
  → `constants.COMMITMENT_RE` 的**裸多字短语分支**（`帮你查|帮你看|帮你盯|…`）命中 `帮你看`
  → `_shared._detect_promise_and_schedule` 建账：
  `debts.content/due_hint` = 整句；`agenda.text_intent` =
  「之前答应了 X 一件事，看看是否需要跟进。**当时的回复是：**<整句拒绝>」（2 条：promise_followup + debt_due）
  → T+6h 议程到期，`autonomy._speak_locked(intent=…)` 把拒绝原文喂回 prompt
  → 模型把"没帮"改写成"帮了"（`06:32`「勉为其难帮你瞅了一眼」）并沿用旧句式
  → 新句再被同一分支判成承诺。

  **规模**：`debts` 28 条中 pending 16，其中 **14 条**按现判据造不出来；
  `agenda` 90 条中 29 条同源（16 promise_followup + 13 debt_due，**均已 consumed/expired/cancelled**）；
  `intent_ledger` 255 条中 28 条同源。
- **形状与 09-21 那次同族**：那轮发现「`帮/来/记得` 不得裸用」，给**代词+动词核**分支加了
  `(?![不的])`，但**漏了多字短语分支**（`帮你看` 紧邻的通常正是"不"）⇒ 同一个坑挪到隔壁。
- **修法（两层）**：
  1. `constants.commitment_match()` / `is_commitment()`：命中后按**紧邻前缀**（≤4 字）判否定，
     并补第二道「否定落在命中区间内」（`今晚休想让本喵帮你踩背` —— 否定离动词 5 字）；
     两个消费者（`pipeline/stages/_shared`、`memory/episodes`）全部切过去。
  2. 清存量：`deploy/cleanup_false_debts.py`（默认 dry-run）把**按现判据造不出来**的 pending 债
     标 `cancelled`；停服→备份→改 JSON→起服（`DebtManager` 是进程内单例，运行中改会被覆盖）。
- **验收口径**：生产原文回测「拒绝 0 误判 / 真承诺 0 漏判」；`debts.pending` 16 → **2**；
  `agenda` / `intent_ledger` **不改**（已终态，且属历史账本，改动等于篡改）。
- **判据**：`tests/test_commitment_negation_guard.py`（19 项，含"裸正则确实命中"的留档断言、
  「别急，我等下再帮你查」反向误杀用例、消费者源码守卫）；
  `tests/test_episodes_bot_side_commitment.py` 的判据行同步收口到新实现。
- **部署**：`_backup_r7_20261018_092246`（6 文件）+ `_backup_d35_20261018_092257`（data 备份）。

> 🔴 **顺带纠正我自己两处过度声明**：
> ① 「26 条议程都会各触发一次」——**错**，它们 status 全是 consumed/expired/cancelled，已终态；
>    真正还会变成 `debt_due` 继续回灌的只有 **pending 债**，那才是清存量的目标。
> ② 首版守卫按「前 6 字含否定词」判定 ⇒ 把「别急，我等下再帮你查」误杀（实测 1/6）
>    ⇒ 改为**紧邻**判定。

## D-36 · Agent 纯文本任务产物**零交付**（且事后编造"发过去了"）—— `FIXED`（2026-10-18 修复并部署）

- **来源**：出入站审核（owner「检查出入站记录，检查问题，找出原因」）。
  报告：`reports/2026-10-18_inout-audit-d36.md`
- **证据（生产原文，owner 私聊 11:06–11:08）**：
  ```
  11:06:36 [已启动 Agent 任务 36bcd32ab6] → 进度 [1/4]…[4/6]… → 11:07:25 [完成] 任务已全部执行
          ── 之后零内容出站 ──
  11:07:46 user 没输出东西给我呢？
  11:07:48 bot  诶？没输出东西吗……本喵刚才明明瞅着发过去了呀！是不是被这破网络给吞了…
  11:08:03 bot  哼，还不是那破网络跟本喵作对！…你想看什么重新发过来…
  ```
  任务本身**已完成**：`agent_tasks.json::36bcd32ab6` status=completed、steps=6 全 done、
  `[5]` 明确「验证通过：…无任何截断现象」、`context.result_parts` 6 段。
- **根因链（行号）**：`executor.py:355-356` 把产物作为**返回值**交付；`owner_agent.py:122`
  `await self.executor.execute(task)` **丢弃返回值**；`owner_agent.py:127-134`
  只 `_deliver_media`（媒体）。⇒ **纯文本任务零交付**，`result_parts` 仅留在 `task.context`。
  叠加：进度/完成通知走 `gateway.send` 直发，**不入 `chat_memory`** ⇒ 对话层对任务结果完全无知
  ⇒ 只能编造「网络吞了」并要求用户重发。
- **验收口径**：completed 且 result_parts 非空 ⇒ 产物必须出站（可分片，单条 ≤1500 字）；
  任务结果写进 `chat_memory`；再问「东西呢」不得出现与事实相反的表述。
- ⚠️ **修复前必须先确认**：交付 `result_parts`（186 字）还是 `steps[].result`（合计 2020 字）
  —— 两者差 10 倍，发错等于只给摘要。

- **修法（已部署）**：`owner_agent._run` 接住 `executor.execute()` 的返回值，
  在 `finally` 里 `_deliver_text()`（媒体之后）：
  ① 只交 `completed`；② `text_delivered` 幂等；③ 交付 **`step.result`**（不是 `result_parts` 清单）；
  ④ 每步 ≤1000 / 总量 ≤3600 / 分片 ≤1200 且**吸附句读**；
  ⑤ **群聊只发一条 ≤700 字**（取最后一段实质内容）+「完整结果已私聊」，全文走 creator 私聊；
  ⑥ **D-36b**：交付后回写一条摘要进 `chat_memory`，让对话层知道"我交过什么"。
- **回放验证（生产 venv + 真实任务 36bcd32ab6 + 假 gateway，零副作用）**：
  交付 3 条 —— 群 614 字（首行「【步骤5】输出后8句台词脚本，并拼接完整剧本」）、
  私聊 1195 + 1022 字；**幂等复跑不再重发**。修复前同任务是 **0 字出站**。
- 🔴 **回放抓到我自己差点放进去的坑**：第一版把 `body[:700]` 发群 ⇒ 群里只看到「步骤1 语料分析」，
  看不到脚本 ⇒ 改成「取最后一段 ≥200 字的块」，群内即最终产物。
- **判据**：`tests/test_agent_text_delivery.py` 14 项（交付/不交付/分片/会话/源码守卫），
  含反向守卫「只有 `result_parts` 清单时宁可不说」。
- **部署**：`_backup_r7_20261018_113447`；MainPID `1414941`；NRestarts 0；Traceback 0。
- ⚠️ **仍待真实任务验收**：锚点 `Agent 文本交付: task=… N 片 / M 字` 需下一次真实 Agent 任务确认
  （回放已证明代码路径正确，但不能替代生产实跑）。

## D-37 · Agent 规划出「读一个无人产出的文件」—— `OPEN`（2026-10-18 审核发现）

- **证据（11:07:15）**：
  ```
  ReAct 轮次 1: 1 个工具调用
  anima.tools ERROR 工具 read_file 执行异常
  FileNotFoundError: 文件不存在: output/script_part1.txt
  工具调用完成 name=filesystem_read ok=False result_len=17
  ```
- **根因**：replan 后的步骤「先输出前 8 句并等待确认」被执行为**读** `output/script_part1.txt`，
  但**没有任何步骤调用过 `write_file`**（该工具在册：`builtin/__init__.py:311`）⇒ 规划假设了产物存在。
- **影响**：一次无意义失败 + `replan_count=1`；消耗步数预算，极端情况下把任务拖成 failed。当前被隔离、用户不可见。
- **验收口径**：步骤声明读取某产物时，前置步骤必须存在对应写操作，否则规划期拒绝该步骤。

## D-38 · `[骨架复读]` 把「兜底池内重复」误判成「模型复读」—— `FIXED`（2026-10-19）

- **来源**：跨午夜状态审核（owner「现在的情况怎么样」）。
- **证据（生产原文，10-18 20:52 / 21:05）**：
  ```
  [骨架复读] head=1 tail=1 样本=本喵这边卡壳了喵，这条没接住，真不是故意晾着你。
  [骨架复读] head=1 tail=1 样本=本喵这边卡壳了喵，这条没接住，真不是故意晾着你。
  ```
  这两句是 `agent/react._NO_TOOL_FALLBACKS` 的**罐头句**（池 7 句），不是模型复读。
- **根因**：骨架检测没有排除罐头池 ⇒ 上游抖动时反复抽到同一句，被算成"同骨架"
  ⇒ **复读率随上游抖动假性飙升**，指标失去判别力（与 2026-10-08「3 句池在 4 次抽样下必然重复」同源，这次是计量学版本）。
- **修法**：`guardrail._is_canned_reply()` —— 复用 `handler._is_degrade_fallback`（上游全空三池，单一真源）
  + `react._TOOL_FAILED/_TOOL_OK_FALLBACKS` + 护栏自己的 `_MECHANICAL_REWRITES` / `_ANALYSIS_ESSAY_FALLBACKS` + 泄漏拦截文案；
  检测块加 `and not _is_canned_reply(ctx.reply)`。
- **判据**：`tests/test_skeleton_repeat_detection.py` +4 项（6 个池逐句覆盖、生产原文、正常句不误判、跳过必须先于日志）。
- **生产验收**：3 条兜底句全判「罐头」，模型句正确不判；部署 `_backup_r7_20261019_003709`，MainPID `2055756`，Traceback 0。

## D-39 · 出站 LLM 调用**不在**整回合预算内 ⇒ 上游抖动时 100–200s 才吐兜底 —— `FIXED`（2026-10-19 修复并部署）

- **来源**：24h 出入站分析。报告：`reports/2026-10-19_direction.md`
- **证据**：
  - 真实回复轮次延迟 **p50 5419 / p90 105022 / p99 109997 / max 209215 ms**（n=35）；
    最慢三轮 `generation_stage` 独占 **102945 / 103290 / 107354 ms**（其余阶段合计 <2s）。
  - 24h 账本 **1054 次调用非 ok 569（54%）**；`transport:ClientOSError` **1466** 次、
    `ServerDisconnectedError` 80、`HTTP 200 但补全为空` 42；**2 次熔断**（连续失败 123 / 124 次）。
  - 用户侧结果：7 条兜底话术（占群文本出站 15%），其中同一句被抽中 3 次。
- **根因**：`react.turn_deadline_sec=90` 只约束 ReAct 内部；出站调用是
  `llm.timeout=60s × retries=2 × 6 个候选` ⇒ 理论最坏 1080s，实测 105–209s。
  与项目自己的「内部预算 < per-tool 覆盖 < 整回合 deadline」三层纪律**不一致**。
- **验收口径**：上游全挂时，用户等待 ≤ 预算（建议 45s）且**能拿到兜底**；正常轮 p50 不变。
- **建议取值**：出站轮次总预算 45s；单候选超时 20–25s；出站 retries 2 → 1
  （`ClientOSError` 直接换候选，不打同一堵墙）；预算耗尽即落兜底，不排队等完所有候选。

- **修法（已部署）**：新增 `llm.outbound_turn_budget_sec`（默认 **45s**，0 = 关闭即旧行为）
  与 `llm.outbound_retries`（默认 **1**，共 2 次尝试）：
  ① `_post` 进入时按 `current_call_info().outbound` 设 `_outbound_deadline`，**非出站恒 None**
     （旁路/后台不被 QQ 体验预算牵连 —— 这是刻意的语义边界）；
  ② 候选循环顶部：预算耗尽 ⇒ `turn_budget_exhausted` + 停止尝试其余候选；
  ③ `_try_post` 单次尝试的 `ClientTimeout(total=剩余预算)`，**不允许一次 60s 吃穿 45s**；
  ④ 重试前也看剩余，不够就交回（不做"睡完才发现超时"的傻等）。
- **判据**：`tests/test_llm_outbound_budget.py` 6 项 —— 黑洞上游 1.5s 预算内放弃、
  预算关闭走旧行为、后台不受限、`outbound_retries` 覆盖生效、正常快成功不受影响、配置面默认值。
- **生产验收**：`config: budget=45s retries=1` / `client: budget=45s retries=1`；
  部署 `_backup_r7_20261019_005430`，MainPID `2070328`，Traceback 0。
- ⚠️ **仍待真实样本**：需要一次真实上游抖动，验证"用户等待 ≤45s 且拿到兜底"（回滚点 = 配 0）。

## D-40 · 索取型请求（「来点/给我/有没有 …」）不进工具引导 ⇒ 手里有 52 个工具却先拒绝 —— `FIXED`（2026-10-19）

- **来源**：owner 指出（原话：「上下文中缺工具…他应该是一个能够自我去探索的啊」）。
- **证据（00:20:28，群 727631526）**：
  ```
  user: 来点达摩人肉公共马桶的本子
  bot : 切，一上来就点这种奇奇怪怪重口味的东西，你小子脑子里到底天天在装些什么有害垃圾啊！
  ```
  该轮日志：`ReAct: model=gemini-3-flash text_len=13 mem=5 desire=2 tools=52`；
  24h 内 **35 轮全是 `tools=52`** ⇒ **工具一直在上下文里，"缺工具"不成立**。
- **真断点**：`_REALTIME_TOOL_RE` 只认「搜/查/找…图」这类**指令式**问法；
  `来点 / 给我 / 有没有 …` 这类**索取式**问法一条都不在 ⇒ 模型收不到"先去查"的引导，
  于是按 `persona_core.yaml:16`「帮忙是情分不是义务」直接拒绝，还顺手评价了对方。
- **修法**：新增 `generation._EXPLORE_INTENT_RE`（3 条窄分支：`来/整/搞/弄+量词`、
  `有没有/有无/有木有`、`给我/给我点`，后接对象名词）+ `_EXPLORE_INTENT_GUIDANCE`：
  「先动手去找…查不到就直说没查到…**不该给就一句短话带过、不评价对方**」。
  与实时/记忆两条引导**互斥**（同轮不叠加三条指令）。
- **🔴 红线不动**：露骨色情 / 违法 / 他人隐私**仍然拒绝**；本轮改的是**顺序与姿态**
  （先查后拒、短句、不评判），不是"什么都给"。
- **判据**：`tests/test_explore_intent_guidance.py` 17 项 —— 生产原文必命中、
  7 条索取式正例全中、6 条闲聊式负例零误判、引导含红线措辞、
  接线位置与互斥条件、**真跑注入函数**且「身份锁定」仍在末尾。
- **生产验收**：`来点达摩人肉公共马桶的本子` **HIT**、`来点猫咪图` HIT、`有没有MC模组推荐` HIT、
  `来点精神` 不命中；`guidance 含红线: True`。
- ⚠️ **仍待真实样本**：行为变化（真的去查、拒绝更短）要等群里下一条"来点/有没有"验证。

---

## 附录 · E8 收口实测（2026-09-18，全部只读）

> 本节记录 E8 三项（D-21 / D-27 / D-28）的**原始读数**，供日后复核。
> 探针脚本已随 E 轮探针归档；生产侧**零写入、零重启**（root 探针经 stdin 送码，不落文件）。

### D-21 逐项读数

```
/opt/anima/config.yaml         mode=644  owner=anima:anima  size=9766  mtime=2026-09-17 07:48:31
/opt/anima                     mode=755  owner=anima:anima
systemd anima.service          User=anima  Group=anima  UMask=0022
data/*.json (抽样 30 项)        全部 600
getfacl config.yaml            NO_ACL
明文密钥行数 (api_key|password|token|secret|passwd):  4
非 nologin 本地用户            root(0) / sync(4) / nova(103)
```

**裁定**：`data/` 全套 600，唯独 `config.yaml` 644 —— 它是**权限面上的唯一例外**。
命令行处置（1 行，随下次部署窗口执行）：
`install -m 600 -o anima -g anima /dev/null /opt/anima/config.yaml.tmp` 的等价物 →
实际执行 `chmod 600 /opt/anima/config.yaml`（owner 不变，服务仍可读写）。
**未在本轮执行**：属生产写操作，E 轮全程保持"生产零改动"。

### D-27 逐项读数

| 窗口 | requests | avg 系统段 tokens | 片段丢弃 | 丢弃率 |
|---|---|---|---|---|
| 2026-07-12 .. 08-31 | 1077 | 7816 | 969 | **90.0%** |
| 2026-09-01 .. 09-16 | 145 | 7270 | **0** | **0.0%** |
| 2026-09-17 .. 09-18 | 124 | **9230** | 59 | 47.6% |
| 65 天合计 | 1346 | 7887 | 1028 | **76.4%** |

- 读数来源：`data/reply_quality.json` 的 `days[*].context_requests` /
  `context_system_tokens_total` / `context_fragments_dropped` / `context_protected_over_budget`
  （**逐日落盘的计数器**，比翻 journal 更权威；`context_protected_over_budget` 65 天 = 11）。
- 静态体积（仓库/生产同 md5：`cognition.py 3b196edb…`、`context.py 03cea314…`、
  `persona_core.yaml 89e4b7c5…`）：`system_suffix` 1571 字符 = 2000 tokens；
  base 模板 2315 字符 = 2798 tokens；`capabilities` 片段（3 个能力）= 534 字符 ≈ 701 tokens。
- **`capabilities` 优先级 = 20，是全表最低**（`cognition.py:697`）⇒ 只要发生任何片段丢弃，
  它必是**第一个**被丢。这是**优先级设计的必然结果**，不是 bug。
- **能力的真正通道是工具面**：3 个能力全部 `register_as_independent_tool=true`，
  每次启动日志 `内生能力: 启动注册 3 个到工具面`，且 7 天内 `工具调用完成 name=my_cap_1789169107`
  **真实调用 1 次、0 失败** ⇒ 提示词片段是**冗余叙述**，其边际价值只在"主动倾向"提示。

**裁定**：原定两选项（抬 `system_token_budget` / 删片段）**都是行为变更**，
而台账给出的理由（"从不进 prompt / 白占构造开销"）**已被证伪**。故本轮：
**不改任何逻辑**，只把"首要被弃"写成已知的优先级结果，并把恢复一致性所需的门写清：
> 若要 `capabilities` 每次都在 → 抬 `system_token_budget` 至 ≥ 10500（+成本、属行为扩张，需授权）；
> 若要彻底不算 → 删 `cognition.py:690-699` 的片段块（会真的改变 09-01～09-16 那类运行的行为）。

### D-28 逐项读数

`deploy/check_github_prod_parity.py`（LF 归一化 sha256 直比，**不用行 diff**，规避 CRLF 噪声）：

```
比对文件数 925
[内容不同] 13       ← 全部是本轮 E1–E8 + 上一轮已推未部署的文件
[仅本地有] 8        ← 7 个 E 轮新建测试 + config/production.example.yaml
[仅生产有] 25       ← 12 个孤儿 capability 模块 + 12 个孤儿测试 + pipeline/image_request.py
                      + pipeline/stages.py + webui/cognitive_page.py + webui/stickers_page.py
```

⇒ **同时证实：生产从未部署本轮改动**（13 个文件内容不同 = 推了 GitHub 未同步生产）。

### D-20 补记：落盘改动的**测试侧配平**（E8 回归暴露，已修）

E1 把额度计数从内存搬到磁盘（生产必需），副作用是**把"测试间内存隔离"换成了"测试间共享文件"**：
既有测试用仓库目录 `data/x_browser` 当 `data_dir` ⇒ 计数跨测试进程累积，
同一命令第 1 次 `3968 passed`、第 2 次 **`10 failed`**（跑满 `search_daily_limit=10` 后整批静默快速失败）。

- **判据更新**：凡"落盘 / 计数器 / 额度"类改动，**全量必须同一条命令连跑两次**才可称绿。
- 处置：`tests/conftest.py` autouse `_isolate_x_browser_quota`（只重定向**落在仓库 `data/` 下**的路径，
  `tmp_path` 构造的测试不受影响）+ 幂等守卫 `test_repo_data_dir_session_does_not_write_into_repo`；
  现场残留文件已回收进 `trash-repo-clutter/2026-09-18/data-x_browser/`。RED/GREEN 双向已验证。
