# PROJECT_STATE — ANIMA 当前状态快照

> 维护者：Principal Autonomous Engineering Agent
> 更新：2026-08-15（R6 系列推进中）
> 分支：`anima-autonomous-evo` · 生产版本：`424f4347bd70`

## CURRENT

| 项 | 值 |
|---|---|
| 分支 / 生产版本 | `anima-autonomous-evo` / `ad9feb1`（R6.3-05 permission model v2） |
| 阶段 | **R6 Governance & Execution**（R6.1/R6.2 已部署，R6.3/R6.4/R6.5 已提交待部署） |
| R6.1 权限边界 | **COMPLETE**（owner-only 硬边界，已部署 `8eca02d`，VPS 验收通过） |
| R6.2 治理加固 | **COMPLETE**（revoke/kill-switch/audit-chain/attestation 已部署；event_chain 在 `32d40de`） |
| R6.3 权限模型 v2 | **COMPLETE**（RBAC 引擎 19 测试绿，已提交 `ad9feb1`） |
| R6.4 执行隔离 | **COMPLETE**（隔离策略层 17 测试绿，已提交 `ad9feb1`） |
| R6.5 行为证据 | **COMPLETE**（证据系统 11 测试绿，已提交 `ad9feb1`） |

> 更新：2026-08-15 · R6.3/4/5 已提交 `ad9feb1`（94 测试全绿）。待 owner 授权部署 + 生产实证。

## DONE（R6 系列）

- **R6.1** owner-only 硬边界（`PythonExecProvider._authorize_owner`），fail-closed，D1-D4 短路，审计证据齐备，VPS 验收通过。
- **R6.2** 并行 agent 完成：capability revoke、kill switch、audit hash chain、runtime attestation（均部署）。
- **R6.2** 本 agent 完成：`event_chain.py` 端到端审计事件链（caller/capability/permission/provider/sandbox/result/timestamp）+ 通用 JSONL 存储 + fail-open sink + 反伪造。
- **R6.3** `rbac.py` 企业级 RBAC：Principal→Role→Capability→Scope→Policy→Audit；临时授权/继承/吊销/反伪造/fail-closed。
- **R6.4** `isolation.py` 工业级隔离：CPU/内存/超时/进程数/seccomp 策略档 + 资源记账 + 终止时序 + fail-closed 校验。
- **R6.5** `evidence.py` 行为证据：execution trace → artifact → audit → replay，任何行为可事后还原。

## RISK

1. **R6.3/R6.4/R6.5 已提交未部署**：代码已进 git 历史（`ad9feb1`），但未上生产。
2. **生产实证**：R6.1 已验收，但 R6.3-R6.5 的 VPS 生产实证（bwrap 真实隔离、审计落盘）待 owner 授权部署后执行。
3. VPS 凭证 `ANIMA_DEPLOY_HOST/USER/PASS` 本环境缺失，自动部署受限。

## NEXT（推荐顺序）

1. **等 owner 授权后部署 R6.3/R6.4/R6.5** 到 VPS + 生产实证（审计落盘）。
2. **R6.5 收尾**：VPS 实证确认 execution trace 落盘可 replay。
3. **R7 Multi-Agent Architecture**：Planner/Developer/Tester/Reviewer/Deploy Agent，复用 R6.3 RBAC 授予/继承/吊销。
4. **R8 Autonomous Development Loop**（发现问题→Issue→规划→改码→测试→Review→部署→验证）。
5. **R9 Controlled Self Evolution**（自我优化，但 Self improvement ≠ Self authorization，全程 owner 审批）。