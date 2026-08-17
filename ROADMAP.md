# ROADMAP — ANIMA 长期路线图

> 目标：从 AI Runtime 演进为 安全、自治理、可扩展、可审计、支持多 Agent 协作、
> 支持受控自我优化的 **Autonomous Agent Operating System**。

## 已达成（DONE）

- **R5 Sandbox Security**：COMPLETE（fail-open 修复、隔离器探测、D1-D4 门）。
- **R6.1 Owner Authorization Boundary**：COMPLETE（owner-only 硬边界，已部署）。
- **R6.2 Governance Hardening**：COMPLETE（revoke / kill switch / audit hash chain / runtime attestation / event_chain，已部署）。
- **R6.3 Permission Model v2**：COMPLETE（RBAC 引擎 19 测试绿，已部署 `ad9feb1`）。
- **R6.4 Execution Isolation**：COMPLETE（隔离策略层 17 测试绿，已部署 `ad9feb1`）。
- **R6.5 Execution Evidence**：COMPLETE（证据系统 11 测试绿，已部署 `ad9feb1`）。
- **R7 Multi-Agent Architecture**：COMPLETE（编排器 7 测试绿，已部署 `ff41e74`）。
- **R8 Autonomous Development Loop**：COMPLETE（issue 采集+闭环 8 测试绿，已部署 `f939049`）。
- **R9 Controlled Self Evolution**：COMPLETE（提案/围栏/审批门 8 测试绿，已部署 `e1b32e7e1a5a`）。
- **R9-02 WebUI 受控自我进化通道**：COMPLETE（4 端点 + 工程页面板，已部署 `dfe993d`）。
- **R10-00 GitHub Actions CI/CD**：COMPLETE（`.github/workflows/anima-ci.yml` + `scripts/anima_guard.py` + `scripts/ci_evidence.py`；测试/前端/安全护栏/证据四 job；禁止自动部署/自动批准 Evolution）。
- **R10-01 Reality Observation Layer**：COMPLETE（`anima/observation/` 统一观测事件层：审计 DENY 聚合/运行时健康/用户反馈/聚合桥接 R8；9 测试绿）。
- **R10-02 Evolution Evaluation Engine**：COMPLETE（`anima/evolution/evaluator/`：MetricSample/collect_metrics + Benchmark 前后采集 + ImprovementEvidence 改进/退化判定；12 测试绿；接入 R9 `EvolutionLoop.evolve` 产出并落盘 `evolution_evidence.jsonl`）。

## 进行中（IN PROGRESS）

- **R10 工程化自治闭环**：R10-00 / R10-01 / R10-02 三项均已完成；并新增 `anima/evolution/orchestrator.py` 将 Observation → Evolution → Evaluation 串成单一可观测闭环（默认 dry_run 不部署，fail-closed；2 测试绿）。
- **R11 Agent Intelligence**：R11-01 Memory Evolution + R11-02 Skill Evolution（受控）均已完成。
- **R12 Multi-Agent Society**：R12 Debate & Judge 已完成。
- **R13 Autonomous Operation**：R13 Health & Recovery 已完成。
- **R14 Human-AI OS**：R14 Control Surface 已完成。
- **R15 ANIMA OS Final**：系统级端到端验收完成（R10→R14 全链路闭环 + fail-closed 验证；4 测试绿）。
- **R11 Agent Intelligence**：R11-01 Memory Evolution Engine + R11-02 Skill Evolution（受控）均已完成（`anima/agent/memory_evolution.py`，确定性挖掘行为结果→受控优化提案→桥接 R8 DevIssue / 转 SelfCode 候选；9 测试绿；候选仅生成、绝不自动安装，落地需 Owner 审批）。
- **R12 Multi-Agent Society**：R12 Debate & Judge 已完成（`anima/agent/multi_agent_society.py`，复用 R7 独立身份/RBAC；多 Agent 辩论 + 裁判裁决，结论建议性、fail-closed；7 测试绿）。
- **R13 Autonomous Operation**：R13 Health & Recovery 已完成（`anima/agent/autonomous_operation.py`，复用 R10-02 健康采集；故障检测 + 受控恢复计划，涉及运行时/渠道/配置的动作一律需 Owner 授权，绝不自我重启/重部署；9 测试绿）。
- **R14 Human-AI OS（Control Surface）**：R14 控制面聚合已完成（`anima/os/control_surface.py` + `/api/os/overview` `/api/os/command`；聚合健康/恢复/提案/观测全景，受限 NL 指令解析，特权动作仅指向既有 Owner 授权端点、绝不自我授权；9 测试绿）。
- **R15 ANIMA OS Final**：系统级端到端验收完成（`tests/test_r15_os_final_acceptance.py`，把 R10→R14 串成可验证闭环：观测→受控进化提案→改进证据→控制面聚合；全链路 fail-closed、绝不自我授权；4 测试绿）。**R6–R15 全程目标达成。**

## 下一阶段（NEXT）

### Phase R10-01 Reality Observation Layer — DONE
新增 `anima/observation/`：runtime_monitor / audit_analyzer / user_feedback / collector；统一 `Observation Event`。github_source（GitHub Issue 采集）留 TODO（需 API token）。

### Phase R10-02 Evolution Evaluation Engine
新增 `evolution/evaluator/`：metric / benchmark / comparison；Before/After 度量，产出 `Improvement Evidence`。

### Phase R11–R15（总路线）
R11 Agent Intelligence（Memory / Skill Evolution）→ R12 Multi-Agent Society（Multi Plan / Debate / Judge）→ R13 Autonomous Operation（Health / Recovery）→ R14 Human-AI OS（Dashboard / NL Management）→ R15 ANIMA OS Final。

### 明确不做
- 无审批自动安装。
- 默认不做语音模块（用户否决过）。
- CI 永不自动部署生产、永不自动批准 Evolution、永不自动改代码、永不自动跳过失败。


### Post-R15 — 个人 Autonomous Agent OS（非商业产品，2026-08-17 起）

重新定位：ANIMA 是 **Owner 长期使用的个人 Autonomous Agent Operating System**，
终态 Owner → ANIMA OS →（记忆/技能/Agent/知识/运行时/演化/运维）。
**明确不做**：商业化、Marketplace、SaaS、多租户、企业权限扩展、用户增长。

- **P0 Runtime Reliability**（进行中）
  - **P0-01 Daily Health Proof**：`anima/os/reliability.py` —— 每日健康证明（版本/文件hash/
    配置hash/权限/Audit/Agent/漂移），fail-closed，可答「昨天有没有异常？」。**DONE**。
  - **P0-02 Long Running Test**：7/30/90 天稳定性记录（uptime / anomaly / recovery /
    permission-drift / evolution 计数）。**DONE**。
- **P1 Real-Environment Observation**（规划→**DONE**）：GitHub commits/CI/issue + 运行时 CPU/mem/
  error/latency + Audit DENY 增长 → Reality → Observation → Analysis → Proposal（`anima/observation/environment.py`）。
- **P2 Evolution Impact Score**（规划→**DONE**）：前后对比（test-fail-rate / latency 等）量化优化收益，
  「我建议 X，因为历史数据证明有益」（`anima/evolution/impact.py`）。
- **P3 Real Agent Society**（规划→**DONE**）：Architect/Security/Research/Coding/Critic/Judge 多视角
  降错，升级 R12 `argue_fn` 为真实多 Agent 辩论（`anima/agent/agent_society.py`）。
- **P4 Long-Term Memory**（规划→**DONE**）：事件→知识→行为模式三层长记忆（Owner 习惯 / 历史决策 /
  为何这样设计）（`anima/memory/long_term.py`）。
- **P5 Autonomous Maintenance**（规划→**DONE**）：过期依赖检测→风险分析→升级提议→CI 验证→
  等 Owner 批准→执行（工程准备自动，执行需授权）（`anima/agent/autonomous_maintenance.py`）。
- **P6 Personal Skill Ecosystem**（规划）：Skill Proposal → Sandbox → Benchmark → Adopt 生命周期。

## 完成标准（最终判断）

1. 可以安全执行任务
2. 可以管理自己的权限
3. 可以证明自己的行为
4. 可以多个 Agent 协作
5. 可以自动完成软件工程流程
6. 可以提出自身优化
7. 可以在人工批准下升级自己

## 安全不可违反（贯穿所有阶段）

Fail Closed · No Hidden Authority · Identity Cannot Be Forged · Audit Everything · Production Is Sacred

## 开发纪律

- 小步提交，格式 `R版本-feature编号`（如 `R6.3-01-principal-model`），禁止大规模混合提交。
- 任何功能：Unit Test + Integration Test + Production Verification，测试失败 STOP。
- 部署：backup → deploy → restart → smoke → doctor → acceptance。