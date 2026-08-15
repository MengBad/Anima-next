# ACCEPTANCE_REPORT — R6 系列代码层验收

> 验收范围：R6.1-R6.5（本 agent 负责的 R6.2 event_chain / R6.3 / R6.4 / R6.5 + R6.1 验证）
> 日期：2026-08-15 · 分支：`anima-autonomous-evo`

## 结论

**代码层验收通过**：R6.1 已部署并 VPS 验收；R6.2 event_chain、R6.3 RBAC、R6.4
Execution Isolation、R6.5 Execution Evidence 代码就绪、测试全绿。因与并行 agent
的 git 索引竞态，提交/部署待其收尾 + owner 授权后执行。

## 测试证据（pytest 全绿）

| 阶段 | 测试 | 结果 |
|---|---|---|
| R6.1 | `test_r6_1_python_exec_authorization.py` | 24 passed（owner ALLOW / 非 owner DENY / 伪造 DENY） |
| R6.2 | `test_r6_2_event_chain.py` | 16 passed（端到端审计事件链 + 反伪造 + fail-open） |
| R6.2-04 | `test_r6_2_04_runtime_attestation.py`（并行 agent） | 7 passed |
| R6.3 | `test_r6_3_rbac.py` | 19 passed（RBAC/scope/临时/继承/吊销/反伪造/审计） |
| R6.4 | `test_r6_4_isolation.py` | 17 passed（CPU/内存/超时/进程/seccomp/记账/终止） |
| R6.5 | `test_r6_5_evidence.py` | 11 passed（trace/artifact/audit/replay/反伪造/fail-open） |
| **合计** | | **94 passed** |

## 完成标准对照

### R6.3 Permission Model v2（任何能力调用回答五问）
- 谁？ → Principal（R6.1 trusted call context）
- 为什么？ → PolicyDecision.reason / role
- 允许什么？ → capability + scope
- 有效多久？ → effective_until（temporary permission）
- 留下什么证据？ → audit event（to_event_fields → event_chain）

### R6.4 Execution Isolation（任何执行行为可限制/可终止/可追踪）
- 可限制 → CPU/内存/超时/进程/seccomp 上限
- 可终止 → TERM→KILL 两段终止时序
- 可追踪 → ResourceAccount 记账 + isolation_to_event

### R6.5 Execution Evidence（任何行为可事后还原）
- trace → artifact → audit → replay 闭环
- 反伪造：不存在的 execution_id 还原 → None
- 参数只留 sha256 指纹，不存原始敏感参数

## 生产实证（待 owner 授权）

1. R6.3-R6.5 部署到 VPS（bwrap 真实隔离验证 + 审计落盘实证）。
2. R6.2 event_chain 一次良性受治理调用，确认审计落盘。
3. R7 起进入 Multi-Agent 架构。

## 安全纪律保持

- 全部 R6.3/4/5 为加性新增独立模块，未修改共享核心。
- fail-closed / 反伪造 / audit-everything / fail-open 审计全部测试钉死。
- 代码备份于 `_r6_backup_arch_20260815_224317/`（防并行 agent stash 误伤）。