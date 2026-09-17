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

## 0. 统计（2026-09-18，Phase A–D 执行后）

| 状态 | 条数 | 备注 |
|---|---|---|
| `FIXED`（本轮修） | 9 | D-01/02/03/04/09/23/24/25/26 |
| `VERIFIED-NON-ISSUE`（实测证伪） | 2 | D-12（数据轮转）、D-22（context 预算=0） |
| `OPEN` | 12 | 含 D-13（生命链闭环）、D-17（16 个空目录受 OS 阻塞） |
| `NEEDS-OWNER` | 5 | 均需显式授权或决策（含 D-14 口径已更正） |
| `SEALED` | 1 | D-29 |
| **合计** | **29** | |

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
- **未闭合项**：D-13（Reflection → Memory 无 producer）—— 闭合前生命链"接上但不活"。

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

## D-05 · F8 索引引用了仓库中不存在的子文档 —— `OPEN`

- **来源**：审查 P1-5 / §2.6。
- **证据**：`capability_contracts/F8_GOVERNANCE_INDEX.md:34-50` 列 F8.1–F8.12 子文档，
  仓库中不存在。
- **验收口径**：索引逐项标注「在库 / 不在库」，不在库者写明原因（未产出 / 已合并 / 已删除）。
- **状态**：本轮按"诚实表述"处理（见 A5）。

## D-06 · `config/default.yaml` 已过期（缺 7 个段）—— `OPEN`

- **来源**：本轮新发现（对比生产键集时暴露）。
- **证据**：代码中存在但 `config/default.yaml` 无对应段的 7 个：
  `debt_tracking`(config.py:669) · `memory_compression`(:645) · `owner_browser`(:630) ·
  `proactive`(:597) · `proxy_mgmt`(:825) · `react`(:683) · `task_agent`(:615)。
- **后果**：照 `default.yaml` 抄一份新配置会**静默丢掉**这 7 段（值走代码默认，不报错）。
- **验收口径**：7 段补齐到 `default.yaml`，且 `AnimaConfig` 全字段与 `default.yaml` 键集
  一致（可加一条测试钉住）。

## D-07 · 生产 config.yaml 含 6 处明文密钥，其中 2 处无 env 通道 —— `NEEDS-OWNER`

- **来源**：本轮新发现。
- **证据**：`llm.api_key`(48) · `vector.embedding_api_key`(51) · `vector.rerank_api_key`(51) ·
  `webui.token`(40) · `tavily.api_key`(58) · `x_browser.password`(13)。
- **分类**：
  - 已有 env 注入通道（可直接外置，`config.py:1019-1032`）：`llm.api_key` /
    `llm.endpoint` / `vector.embedding_api_key` / `vector.rerank_api_key` / `tavily.api_key`。
  - 🔴 **无 env 通道**（需代码改动）：`webui.token` · `x_browser.password`。
- **验收口径**：`deploy/check_config_drift.py` 第 4 节「明文密钥 = 0 处」。
- **卡点**：外置 = 改生产凭据注入方式 + 轮换，**需 owner 决定**（涉及停机与凭据轮换）。

## D-08 · `ConfigDriftMonitor` 结构上抓不到重启期变更 —— `OPEN`

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

## D-10 · 「空 allowlist = 全拒」是个错误心智模型 —— `OPEN`

- **来源**：D-09 衍生。
- **证据**：`anima/tools/__init__.py:977-979` 的 pilot 门带 `capability_only` 前置条件；
  `:194-195` 明写 python.exec 不经该门。⇒ 空 allowlist 只拒 **pilot-only** 能力，
  不拒 mixed-mode / non-capability_only 能力（含 python.exec）。
- **验收口径**：在 `MAINTENANCE.md` / 治理文档中统一表述为
  「空 allowlist = 拒绝全部 **pilot-only** 路由」，并给出 python.exec 的真实门清单。
  （本轮已写入 MAINTENANCE 的"易误读点"，但治理文档侧尚未逐处核。**

## D-11 · 12 个生产废弃模块 + 潜在 ImportError 地雷 —— `NEEDS-OWNER`

- **来源**：审查 P2-3。
- **证据**：生产独有 12 个模块（`pipeline/stages.py` 2204 行死代码、`image_request.py`、
  8×`capability/runtime_*.py`、2×`webui/*_page.py`）；
  `anima/capability/runtime_evidence_integration.py:53` 引用已归档模块。
- **验收口径**：先修 `:53` 的引用，再删除/封存 12 个模块；删除前后各跑一次全量回归。
- **卡点**：删生产文件属独立授权（审查明确列为"独立授权任务"）。

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

## D-13 · Reflection → Memory 闭环无生产 producer —— `OPEN`

- **来源**：审查 P1-2。
- **证据**：`MemoryAuthority.ingest` 全库无生产调用者（仅 `invariants.py:28` 导入）；
  `constitution/invariants.py:17` 自称 "Run in CI / at boot" 但无 boot/CI 调用点。
- **验收口径**：二选一 —— 接上真实 producer，或在文档标注"未闭合"并把
  `invariants` 的 CI/boot 接线点删掉或实现。**不允许继续自称 Run in CI。**

## D-14 · F5 / M0.x 执行基建：**模块已接线 / 产物为零** —— `NEEDS-OWNER`（口径已更正）

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

## D-15 · 两个 `capability` 同名不同物 —— `OPEN`

- **来源**：审查 P1-4（`f9.1_contradiction_backlog:104` 的 N-01 升级为既成事实）。
- **证据**：抽查 journal 近 30 天 1063 行含 `capability` 的日志，**全部**来自
  `anima.tools.capability`（自生长能力层），治理关键字命中 **0**。
- **验收口径**：至少做到文档层命名消歧（治理面 `capability_layer.*` vs 自生长
  `capabilities.*` vs 组件 `anima/capability/`），并在排查手册加"看到 capability
  先确认是哪一层"。

## D-16 · RFC-0002/0003 已 APPROVED 但未实现 —— `NEEDS-OWNER`

- **来源**：审查 P1-7。
- **证据**：`docs/governance/RFC-0002-FINAL-SCOPE-CONFIRMATION.md` /
  `RFC-0003-FINAL-SCOPE-CONFIRMATION.md` 均为 APPROVED；代码未见 Task DAG / Workflow DSL。
- **验收口径**：二选一 —— 实现，或明确回退为 `Deferred`。当前状态是"批准了但没人做"，
  这在治理语义上等价于永久挂起，应当写明。

## D-17 · 仓库游离物（62 项）—— `OPEN`（46/62 已回收；16 个受 OS 权限阻塞）

- **来源**：审查 P2-1。
- **实测清单（2026-09-18，`glob` 全量）**：**62 项 / 共 1389.6 MB**，且**全部 `.gitignore` 已覆盖**
  （`git ls-files --error-unmatch` 逐项确认 **0 项被跟踪**）。⇒ 这不是"工作树脏"（`git status` 本来就是干净的），
  而是**文件系统层杂乱**。原报告"72 项"的计数偏高。
- **已回收 46 项 → `.workbuddy/tmp/trash-repo-clutter/2026-09-18/`**（`_MANIFEST.json` 留档，**未删除**，可原样取回）：

| 类别 | 项数 | 体积 |
|---|---|---|
| `.pytest-*` 临时目录 | 46（含 `tmp_*`/`MagicMock`/日志等） | —— |
| `tmp_astrbot_test/` | 1 | **1354 MB**（单笔最大） |
| `.pytest-r10/` | 1 | 20.5 MB |
| `.git.broken-20260915/`（git 事故留证） | 1 | 10.9 MB |
| 根目录 `_*.log` / `_deploy_out.txt` | 17 | ~0.2 MB |

- **❗ 未能回收 16 个**：`.pytest-tmp*` 系列**空目录**（0 字节）。任何**打开/改名/删除**操作都返回
  `PermissionError [WinError 5]`（`os.listdir` / `os.rename` / `os.rmdir` / `shutil.move` 全部失败），
  但 `os.lstat` 可读：`mode=0o40777`、`attrs=0x10(DIRECTORY)`、**不是重解析点（reparse=False）**。
  属 OS 层 ACL/句柄问题，**未强删**（遵循既有 `.git` 事故纪律：`.git` 内删除同样报 `WinError 5`，严禁强删）。
  影响面：**零**（空目录 + 已被 `.gitignore:16,57` 覆盖 + 无功能引用）。
- **验收口径（部分达成）**：46/62 已移出工作树；剩余 16 个需在**普通终端/管理员**下处理，属环境权限问题。
- **附带结论**：`tmp_astrbot_test` 一项即占 **97%** 的体积，是 2026-07 遗留的 astrbot 试验环境。

## D-18 · `_TOOL_FAILURE_PREFIXES` 之外仍有一条待决语义 —— `NEEDS-OWNER`

- **来源**：2026-09-18 工具失败判定完整性审计（上一轮）。
- **证据**：`X 搜索异常` 前缀当前被判**成功**。该轮 47 处 fail-open **全部**是
  "文本明确自称失败"，唯独此条措辞算不算失败属**产品判断**。
- **验收口径**：owner 决定后一行改动即可。

## D-19 · `react.py` 触顶兜底话术在通用路径上不适用 —— `OPEN`

- **来源**：2026-09-18 占位串逃逸修复（上一轮）。
- **证据**：`anima/agent/react.py` 的 `_empty_content_fallback()` 里
  「换个具体视频链接（带 BV）再丢一次」出现在**通用**兜底路径 —— 用户问"天气/几点"
  时会得到"发个 BV 链接"的建议。
- **验收口径**：改为与场景无关的通用说法（属产品措辞，未擅自改）。

## D-20 · X 搜索额度 10/日 与"额度用尽=静默失败" —— `OPEN`

- **来源**：既有运维发现。
- **证据**：`search_daily_limit: 10`；额度用尽表现为**毫秒级、零日志**的静默失败，
  且 `_daily` **只在内存 ⇒ 重启即重置**。
- **验收口径**：额度计数落盘（跨重启连续）＋ 用尽时写一条显式审计/日志。

## D-21 · 生产 `config.yaml` 文件权限 —— `OPEN`（待核）

- **来源**：`reports/2026-08-27_vision_gap_plan.md` D2（当时报 644）。
- **验收口径**：实测权限；若仍为组/其他可读，`chmod 600` 并在 `_deploy.py`
  启动自检加权限断言。

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

## D-27 · 系统提示词预算长期顶格（`capabilities` 段从不进 prompt）—— `OPEN`（归因已闭环）

- **来源**：既有体检基线（09-18 计 38/38，tokens 均值 ~9534/10000）。
- **✅ 归因闭环（2026-09-18 生产只读实测）**：生产 `context` 的**精确取值**为
  `input_token_budget 12000` / **`system_token_budget 10000`** / `image_token_estimate 768` /
  `tool_result_token_budget 1800`（`config.yaml:297-301`）。
  那个 **10000** 正是体检里 "~9534/**10000**" 的分母 ⇒ 顶格是**阈值偏小 / 段本身偏大**，
  **与"0 语义"无关**（D-22 已被三重证伪：掩码规则、`ge=1024`、生产实读）。
- **验收口径**：二选一 —— 抬 `system_token_budget`，或明确裁掉 `capabilities` 段
  （现在它 100% 被丢弃，等于白占构造开销）。**未确认前不改任何逻辑。**

## D-28 · GitHub ↔ 生产无自动一致性门 —— `OPEN`

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
