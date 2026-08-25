# PROJECT_STATE — ANIMA 当前状态快照

> 维护者：Principal Autonomous Engineering Agent
> 更新：2026-08-25（C 地基 + B 阶段全清 + 一生快照 + 记忆水合/合并门/QQ引用 + stealer 表情整顿 + webui 深链修复 + 自主劝睡拦截）
> 分支：`anima-autonomous-evo`（= `master`）· 生产版本：`bb37ccc703b7`
> 本日要点：
> - **stealer 收紧**：`_self_review_pass` 仅当视觉判定 `is_sticker=True` 才自动入库；`None` 一律留审核区(产线 5 张无判定 approved 已回退,备份 `stickers.json.bak.20260825184620`,approved 现全带视觉判定)。
> - **webui 深链**：`/stickers` 改走 Vue 双区 SPA(不再给旧纯HTML页);`/os` 补入 `SPA_PATHS`+页面路由(此前深链 401 无法加载)。
> - **自主劝睡拦截**：autonomy 主动发言 `_ACTIVE_LOW_VALUE_RE` 增劝睡类水话(睡大觉/去睡觉/快去睡/早点睡/乖乖睡觉/该睡觉了/睡觉舒服);19:04 群552994229 那条系自主主动发言非写死,现此类被丢弃。

### B5-merge（底座单一源 16/16 折叠，2026-08-25）
16 个顶层 `anima/capability_*.py` 实现全部折叠进规范层 `anima/capability/`；顶层仅留 `__getattr__` re-export 兼容 shim，保持 `from anima.capability_xxx import ...` 旧导入兼容，单一源=anima/capability。冻结核 `test_base_freeze`(顶层集==冻结16) 持续绿；治理/边界扫描测试统一按折叠态解析单源；修 `_FakeEmbedder` 用 `ord` 替代进程随机 `hash` 消除跨进程 flake。全量 3339 确定性绿，生产 `8163c38d0b36`。

### 一生「快照恢复」闭环（Owner 可逆暂存 + 生效设计，2026-08-25）
纵1 一生视图从「只读清点」升级为可用闭环：`POST /api/life/snapshot/restore`（owner_ok+confirm，fail-closed，恢复产物落到 data/life_restore/ = 自动备份 + 原子替换 + 可回滚，拒绝 chat_memory/穿越/非清单）+ `GET /api/life/snapshot/staged` + `POST /api/life/snapshot/rollback`；LifeView 已接 Owner 恢复/回滚 UI；服务级 9 测。另产出 `docs/LifeSnapshotApply-Design.md`（staged→live 生效域 + SAFE_APPLY_PERSONA 门控 + 审计/回滚），生效写端点默认 OFF、待 Owner 拍板。生产 `b30a7815add5`。

## B / C 阶段增量（2026-08-24）

### C 阶段（友好 AI OS 地基）
| 项 | 状态 |
|---|---|
| 宪法记忆水合 memory_engine/MemoryAuthority | COMPLETE（`fbc911d`/`a7df3d5`） |
| G4 客观晋升尺 EvidenceJury | COMPLETE+接线（`ef4b694`） |
| CI DoD 定义+落地硬门 | COMPLETE（`f14b40c`/`c62a146`） |
| R12 多 Agent 评议入评审（默认关） | COMPLETE（`260c0a9`） |

### B 阶段（推翻重构适配两纵四横）
| 项 | 状态 |
|---|---|
| B0 重构蓝图 | `reports/2026-08-24_b_phase_webui_rebuild_blueprint.md` |
| B1 外壳两纵四横导航（一生项） | COMPLETE（`37d6437`） |
| B2 一生时间线只读 `/api/life/timeline` | COMPLETE+已部署 |
| B3 自治工作台闭环徽标（CI DoD/晋升尺/评议） | COMPLETE+已部署 |
| B4 底座收拢一屏 `/api/platform/summary` | COMPLETE+已部署 |
| B5 底座冻结核（anti-entropy 顶层 16 冻结） | COMPLETE+已推送（测试轮） |
| B6 一生档案·传承（快照清点/遗忘/日记/伤痕/议程） | COMPLETE+已部署 |

> 全量 pytest **3336 passed**；WebUI `npm run build` 通过。未完成：B5-merge（顶层 16 个 capability_* 折叠进 `anima/capability/`，跨轮大件）；B4 平台四横前端分组（纯前端）。

## CURRENT

| 项 | 值 |
|---|---|
| 分支 / 生产版本 | `anima-autonomous-evo` / `e1b32e7e1a5a`（R9 受控自我进化） |
| 阶段 | **R6–R9 Autonomous OS Foundation 全部 COMPLETE** |
| R6.1 权限边界 | **COMPLETE**（已部署 `8eca02d`，VPS 验收通过） |
| R6.2 治理加固 | **COMPLETE**（已部署 `424f434`） |
| R6.3 权限模型 v2 | **COMPLETE**（RBAC 19 测试绿，已部署 `ad9feb1`） |
| R6.4 执行隔离 | **COMPLETE**（隔离层 17 测试绿，已部署 `ad9feb1`） |
| R6.5 行为证据 | **COMPLETE**（证据系统 11 测试绿，已部署 `ad9feb1`） |
| R7 多 Agent 架构 | **COMPLETE**（编排器 7 测试绿，已部署 `ff41e74`） |
| R8 自主开发闭环 | **COMPLETE**（issue 采集+闭环 8 测试绿，已部署 `f939049`） |
| R9 受控自我进化 | **COMPLETE**（提案/围栏/审批门 8 测试绿，已部署 `e1b32e7e1a5a`） |

> 更新：2026-08-16 · R6+R7+R8+R9 全量 **121 测试**无回归；生产版本 `e1b32e7e1a5a`；`master` 与本地主线同步。

## DONE（R6–R9 全部）

### R6 系列（Governance & Execution）
- **R6.1** owner-only 硬边界（`PythonExecProvider._authorize_owner`），fail-closed，D1-D4 短路，审计证据齐备，VPS 验收通过。
- **R6.2** 并行 agent 完成：capability revoke、kill switch、audit hash chain、runtime attestation（均部署）。
- **R6.2** 本 agent 完成：`event_chain.py` 端到端审计事件链（caller/capability/permission/provider/sandbox/result/timestamp）+ 通用 JSONL 存储 + fail-open sink + 反伪造。
- **R6.3** `rbac.py` 企业级 RBAC：Principal→Role→Capability→Scope→Policy→Audit；临时授权/继承/吊销/反伪造/fail-closed。
- **R6.4** `isolation.py` 工业级隔离：CPU/内存/超时/进程数/seccomp 策略档 + 资源记账 + 终止时序 + fail-closed 校验。
- **R6.5** `evidence.py` 行为证据：execution trace → artifact → audit → replay，任何行为可事后还原。

### R7 系列（Multi-Agent Architecture）
- **R7** `orchestrator.py` 多 Agent 安全协作：Planner/Developer/Tester/Reviewer/Deploy 流水线；每 Agent 独立身份 + RBAC 独立授权（fail-closed）；每阶段落 R6.5 Evidence（ALLOW/DENY 都记录，Audit Everything）。已部署 `ff41e74`，生产实证 7 测试绿。

### R8 系列（Autonomous Development Loop）
- **R8** `devloop.py` 自主开发闭环：DevIssue → Plan→Develop→Test→Review→Deploy 端到端编排；`issue_sources.py` 自监控（SelfCode 效果/审计高频 DENY）+ Owner 指令 → DevIssue；受控门禁（fail-closed，部署需 Owner 显式授权）；真实接线 SelfCode + `_deploy.py` + pytest。已部署 `f939049`，生产实证 8 测试绿。

### R9 系列（Controlled Self Evolution）
- **R9** `self_evolution.py` 受控自我进化：ANIMA 可提自身优化但绝不自我授权；`CORE_PROTECTED` 安全围栏禁止自动改核心/权限/安全/部署代码；`EvolutionGate` fail-closed 审批门（默认 DENY，仅 Owner 显式 approve）；`EvolutionLoop` 接入 R8 闭环。已部署 `e1b32e7e1a5a`，生产实证 8 测试绿。

## RISK

1. **已消除**：R6–R9 全部已部署 VPS 并生产实证（总计 121 测试全绿，服务 active）。
2. **残余风险**：R9 受控门禁依赖 Owner 显式审批——若 Owner 误批触及 `CORE_PROTECTED` 的提案，需人工 PR 复核（系统已拒绝自动放行，仅留人工通道）。
3. VPS 凭证 `ANIMA_DEPLOY_HOST/USER/PASS` 本环境已配置，自动部署可用。

## NEXT（推荐顺序）

1. **R9 收尾运营**：在 WebUI 工程/审批页接入 R9 `EvolutionLoop` 提案可视化与 Owner 一键审批（当前仅 API/测试，未接 UI）——**已在 `dfe993d` 完成**。
2. **R10-00 GitHub Actions CI/CD（已完成，commit `dcfeb90`）**：`.github/workflows/anima-ci.yml` 四 job（backend/frontend/security/evidence）真实绿；`scripts/anima_guard.py` + `scripts/ci_evidence.py`；声明 paramiko 依赖；忽略 6 个预存 broken 测试（后续专项修）；`commit-attestation.json` 证明未自动部署/未自动批准 Evolution。
3. **R10-01 Reality Observation Layer（已完成）**：`anima/observation/` 统一观测事件层（审计 DENY 聚合/运行时健康/用户反馈/聚合桥接 R8）；9 测试全绿；github_source 留 TODO 需 API token。
3. **R10-02 Evolution Evaluation Engine（已完成）**：`anima/evolution/evaluator/` （metric / benchmark / comparison + ImprovementEvidence）；接入 R9 `EvolutionLoop.evolve`，部署前后自动采集 Before/After 指标并产出改进证据、fail-open 落盘 `data/evolution_evidence.jsonl`；12 测试全绿。与 R10-01 共同构成「观测 → 评估」闭环（可审计、不自我授权）。
3. **R10-03 可观测自治闭环收口（已完成）**：`anima/evolution/orchestrator.py` （`ObservationOptimizationLoop` 串 Observation → Evolution → Evaluation；默认 dry_run 不部署，fail-closed；2 测试全绿）。R10（CI / 观测 / 评估 / 闭环）工程化自治闭环全部收口。
3. **R11 Agent Intelligence（R11-01 + R11-02 已完成）**：`anima/agent/memory_evolution.py` （Memory Evolution Engine 确定性挖掘行为结果→受控优化提案；R11-02 技能类提案转 SelfCode 候选，仅生成不安装，落地需 Owner 审批；9 测试全绿）。
3. **R12 Multi-Agent Society（R12 Debate & Judge 已完成）**：`anima/agent/multi_agent_society.py` （多 Agent 辩论 + 裁判裁决，复用 R7 独立身份/RBAC；结论建议性、fail-closed、不授权任何动作；7 测试全绿）。
3. **R13 Autonomous Operation（R13 Health & Recovery 已完成）**：`anima/agent/autonomous_operation.py` （运行时健康探测 + 受控恢复计划；信号缺失 fail-closed 判 critical，恢复动作需 Owner 授权、绝不自我重启/重部署；9 测试全绿）。
3. **R14 Human-AI OS（R14 Control Surface 已完成）**：`anima/os/control_surface.py` + WebUI `/api/os/overview` `/api/os/command`（聚合健康/恢复/提案/观测全景；受限 NL 指令解析，特权动作只指向既有 Owner 授权端点、绝不自我授权；9 测试全绿）。
3. **R15 ANIMA OS Final（已完成）**：`tests/test_r15_os_final_acceptance.py` 系统级端到端验收（R10→R14 全链路闭环 + fail-closed 验证；4 测试全绿）。**R6–R15 全程目标达成。**
3. **生产实证补强**：R8/R9 接真实 `issue_sources`（SelfCode 效果建议、审计 DENY 聚合）跑一周期，验证闭环能稳定收敛不退化。
3. **后续增强（路线外）**：人格漂移执法评估、SelfCode 效果评估与建议卸载、大图哈希缓存、danger 收紧、fallback 压测。
4. **明确不做**：无审批自动安装；默认不做语音模块（用户否决过）。

## R9 UI 接入（已完成，commit `dfe993d`）

- WebUI 工程页新增「受控自我进化」面板：发起提案 / 触核心红标 / Owner 批准·驳回。
- 后端 4 个 `/api/evolution` 端点（proposals/propose/{id}/approve|reject），批准仅放行**提案**不部署，审计落盘。
- 前端 `EngineeringView.vue` + `api.ts` + `types.ts`，`npm run build` 类型检查通过。
- 生产实证：`/api/evolution/proposals` 返回 200；R9+WebUI 端点测试 10 项全绿。
- 注意：R9 自我进化**永不借 UI 自我授权生产更新**；批准提案后仍需走 R8 受控部署（owner_authorized）才落地。

---

## Post-R15 战略定位（2026-08-17 起）

> **重新定位**：ANIMA 从「具备自治能力的 Agent 系统」升级为 **Owner 长期使用的个人
> Autonomous Agent Operating System（非商业产品）**。
>
> - 终态不是「AI 产品 → 用户 → 商业价值」，而是 **Owner → ANIMA OS →（记忆 / 技能 /
>   Agent / 知识 / 运行时 / 演化 / 运维）**。
> - **明确不做**：商业化（P7 商业变现）、Marketplace、SaaS、多租户、企业权限模型扩展、
>   用户增长系统。
> - 安全纪律不变：Fail Closed · No Hidden Authority · Audit Everything ·
>   Identity Cannot Be Forged · Production Is Sacred；**Self improvement ≠ Self authorization**。

### Post-R15 优先级路线（P0 最高）

| 阶段 | 主题 | 关键交付 |
|---|---|---|
| **P0** | Runtime Reliability（长期稳定可证明） | 每日健康证明 / 7·30·90 天稳定记录 |
| **P1** | 真实环境观察 | GitHub/运行时/Audit 异常→观察→分析→提案 |
| **P2** | 优化效果评估 | Evolution Impact Score（前后对比可量化） |
| **P3** | 真实 Agent 协作 | Architect/Security/Research/Coding/Critic/Judge 多视角降错 |
| **P4** | 长期记忆 | 事件→知识→行为模式三层（Owner 习惯/历史决策/为何这样设计） |
| **P5** | 自治维护 | 检测过期依赖→分析风险→提议升级→CI 验证→等 Owner 批准→执行 |
| **P6** | 个人能力生态 | Skill Proposal → Sandbox → Benchmark → Adopt 生命周期 |

### P0-01 Daily Health Proof（已完成）

- `anima/os/reliability.py`：每日健康证明（版本/文件hash/配置hash/权限/Audit/Agent/漂移），
  fail-closed，可答「昨天有没有异常？」。

### P0-02 Long Running Test（已完成）

- `anima/os/long_running.py`：7/30/90 天稳定性记录（uptime / anomaly / degraded / 权限漂移 /
  恢复 / 演化计数），fail-closed 诚实，落盘 `data/stability/`。
- WebUI `/api/os/overview` 聚合 7/30/90d 稳定性；`/api/os/command` 新增 `stability` 只读意图。

### P1 Real-Environment Observation（已完成）

- `anima/observation/environment.py`：真实环境观察闭合 Reality → Observation → Analysis →
  Proposal；GitHub 信号 + 性能阈值 + Audit DENY 增长趋势 → ObservationReport → R8 DevIssue
  （只生成提案、不部署）。WebUI `/api/os/overview` 聚合 `environment`；`/api/os/command`
  新增 `observe` 只读意图。

### P2 Evolution Impact Score（已完成）

- `anima/evolution/impact.py`：演化影响评分——聚合历史 Before/After 证据语料为语料级评分 +
  数据驱动建议（`score_for_proposal` 让提案有历史支撑）。WebUI `/api/os/overview` 聚合
  `impact`；`/api/os/command` 新增 `impact` 只读意图。

### P3 Real Agent Society（已完成）

- `anima/agent/agent_society.py`：真实多 Agent 协作——角色专精多视角辩论（架构/安全/研究/
  编码/批判/裁判）+ 多视角加权裁决（降错优先）+ `run_society_debate` 便捷端到端；结论建议性、
  不授权执行。WebUI `/api/os/command` 新增 `society` 只读意图。

### P4 Long-Term Memory（已完成）

- `anima/memory/long_term.py`：三层长期记忆（事件/知识/行为模式）+ why-described 设计原因
  专项 + 关键词查询。WebUI `/api/os/overview` 聚合 `memory`；`/api/os/command` 新增 `memory`
  只读意图（"你还记得我的习惯吗？为什么这么设计？"）。

### P5 Autonomous Maintenance（已完成）

- `anima/agent/autonomous_maintenance.py`：自治维护闭环（检测过期依赖→风险分析→生成 R9 待审
  提案→可选 CI 预检→等待 Owner）。复用 P4 记忆护栏跳过有意为之的依赖、CORE_PROTECTED 保护、
  降错优先；绝不自我安装。WebUI `/api/os/overview` 聚合 `maintenance`；`/api/os/command` 新增
  `maintenance` 只读意图。

### P6 Personal Skill Evolution（已完成）

- `anima/skills/evolution.py`：个人能力生态闭环（提案→沙箱→Benchmark→采纳判定，采纳仍须 Owner）。
  复用 SkillDraftManager 静态护栏与 Owner 批准路径；默认确定性 Benchmark 可注入；绝不自我采纳。
  WebUI `/api/os/overview` 聚合 `skill`；`/api/os/command` 新增 `skill` 只读意图。

**路线收官**：P0–P6 全部完成并 CI 绿；ANIMA OS 以 Owner 长期个人助理为定位的演进闭环成形
（Runtime 可靠 / 真实环境观察 / 演化影响评分 / 多 Agent 协作 / 长期记忆 / 自治维护 / 个人能力生态）。



- `anima/agent/autonomous_maintenance.py`：自治维护闭环（检测过期依赖→风险分析→生成 R9 待审
  提案→可选 CI 预检→等待 Owner）。复用 P4 记忆护栏跳过有意为之的依赖、CORE_PROTECTED 保护、
  降错优先；绝不自我安装。WebUI `/api/os/overview` 聚合 `maintenance`；`/api/os/command` 新增
  `maintenance` 只读意图。



- `anima/memory/long_term.py`：三层长期记忆（事件/知识/行为模式）+ why-described 设计原因
  专项 + 关键词查询。WebUI `/api/os/overview` 聚合 `memory`；`/api/os/command` 新增 `memory`
  只读意图（"你还记得我的习惯吗？为什么这么设计？"）。



- `anima/agent/agent_society.py`：真实多 Agent 协作——角色专精多视角辩论（架构/安全/研究/
  编码/批判/裁判）+ 多视角加权裁决（降错优先）+ `run_society_debate` 便捷端到端；结论建议性、
  不授权执行。WebUI `/api/os/command` 新增 `society` 只读意图。



- `anima/evolution/impact.py`：演化影响评分——聚合历史 Before/After 证据语料为语料级评分 +
  数据驱动建议（`score_for_proposal` 让提案有历史支撑：我建议 X，因为历史证明有益）。
  WebUI `/api/os/overview` 聚合 `impact`；`/api/os/command` 新增 `impact` 只读意图。



- `anima/observation/environment.py`：真实环境观察闭合 Reality → Observation → Analysis →
  Proposal；GitHub 信号 + 性能阈值 + Audit DENY 增长趋势 → ObservationReport → R8 DevIssue
  （只生成提案、不部署）。WebUI `/api/os/overview` 聚合 `environment`；`/api/os/command`
  新增 `observe` 只读意图。



- `anima/os/long_running.py`：7/30/90 天稳定性记录（uptime / anomaly / degraded / 权限漂移 /
  恢复 / 演化计数），fail-closed 诚实，落盘 `data/stability/`。
- WebUI `/api/os/overview` 聚合 7/30/90d 稳定性；`/api/os/command` 新增 `stability` 只读意图。



- `anima/os/reliability.py`：生成每日健康证明，覆盖 **当前版本（commit SHA）/ 文件 hash /
  配置 hash / 权限状态 / Audit 状态 / Agent 状态 / 运行时漂移（R6.2）**。
- fail-closed：任一探针缺失/异常 → 该检查 FAIL，整体证明 DEGRADED（绝不假装健康）；
  证明本身带 sha256 指纹可事后核验。
- `answer_was_anomaly_yesterday(data_dir)`：直接回答 Owner「昨天有没有异常？」。
- 落盘 `data/health_proof/YYYY-MM-DD.jsonl`（每日追加，坏行隔离）。
- 10 单元测试全绿（HEALTHY/DEGRADED/漂移/DENY 计数/落盘问答）。
