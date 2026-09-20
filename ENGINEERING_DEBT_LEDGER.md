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
| `FIXED` | 21 | D-01/02/03/04/05/06/08/09/10/11/15/18/19/20/21/23/24/25/26/27/28 |
| `VERIFIED-NON-ISSUE`（实测证伪） | 2 | D-12（数据轮转）、D-22（context 预算=0） |
| `DECIDED`（口径裁定，非代码改动） | 1 | D-14 |
| `DEFERRED`（显式挂起，含重启前置条件） | 1 | D-16 |
| `CLOSED-AS-UNDERSTOOD`（归真后判定"不改逻辑"） | 0 | ——（D-27 经 **owner 显式授权**改判 `FIXED`：抬至 11000，见下） |
| `CONFIRMED-PENDING-DEPLOY`（已确证，待部署窗口执行） | 0 | ——（D-21 已于 09-18 部署窗口执行，见下） |
| `PARTIALLY-CLOSED`（口径 (a) 达成 / (b) 仍需 owner 裁定） | 1 | D-13 |
| `OPEN` | 1 | D-17（17 项受 OS 阻塞） |
| `NEEDS-OWNER` | 1 | D-07（明文密钥外置） |
| `SEALED` | 1 | D-29 |
| **合计** | **29** | |

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

## D-13 · Reflection → Memory 闭环无生产 producer —— `PARTIALLY-CLOSED`
（09-18 后段：口径 (a) 已达成；第二层「数值 trait 通道」另需 owner 裁定）

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

## D-17 · 仓库游离物（64 项）—— `OPEN`（48/64 已回收；**17** 个受 OS 权限阻塞）

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
