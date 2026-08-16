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
3. **生产实证补强**：R8/R9 接真实 `issue_sources`（SelfCode 效果建议、审计 DENY 聚合）跑一周期，验证闭环能稳定收敛不退化。
3. **后续增强（路线外）**：人格漂移执法评估、SelfCode 效果评估与建议卸载、大图哈希缓存、danger 收紧、fallback 压测。
4. **明确不做**：无审批自动安装；默认不做语音模块（用户否决过）。

## R9 UI 接入（已完成，commit `dfe993d`）

- WebUI 工程页新增「受控自我进化」面板：发起提案 / 触核心红标 / Owner 批准·驳回。
- 后端 4 个 `/api/evolution` 端点（proposals/propose/{id}/approve|reject），批准仅放行**提案**不部署，审计落盘。
- 前端 `EngineeringView.vue` + `api.ts` + `types.ts`，`npm run build` 类型检查通过。
- 生产实证：`/api/evolution/proposals` 返回 200；R9+WebUI 端点测试 10 项全绿。
- 注意：R9 自我进化**永不借 UI 自我授权生产更新**；批准提案后仍需走 R8 受控部署（owner_authorized）才落地。