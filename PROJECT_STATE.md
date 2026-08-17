# PROJECT_STATE — ANIMA 当前状态快照

> 维护者：Principal Autonomous Engineering Agent
> 更新：2026-08-16（R6–R9 全部完成并部署）
> 分支：`anima-autonomous-evo`（= `master`）· 生产版本：`e1b32e7e1a5a`

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
