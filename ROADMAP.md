# ROADMAP — ANIMA 长期路线图

> 目标：从 AI Runtime 演进为 安全、自治理、可扩展、可审计、支持多 Agent 协作、
> 支持受控自我优化的 **Autonomous Agent Operating System**。

## 已达成（DONE）

- **R5 Sandbox Security**：COMPLETE（fail-open 修复、隔离器探测、D1-D4 门）。
- **R6.1 Owner Authorization Boundary**：COMPLETE（owner-only 硬边界，已部署）。
- **R6.2 Governance Hardening**：COMPLETE（revoke / kill switch / audit hash chain / runtime attestation / event_chain，已部署+代码就绪）。

## 进行中（IN PROGRESS）

- **R6.3 Permission Model v2**：代码就绪（RBAC 引擎 19 测试绿），待提交/部署。
- **R6.4 Execution Isolation**：代码就绪（隔离策略层 17 测试绿），待提交/部署。
- **R6.5 Execution Evidence**：代码就绪（证据系统 11 测试绿），待提交/部署。

## 下一阶段（NEXT）

### Phase R6.5 —— Execution Evidence（收尾）
任何 Agent 行为可事后还原：execution trace → artifact → audit → replay。代码已就绪，
待 owner 授权部署 + VPS 生产实证（审计落盘）。

### Phase R7 —— Multi Agent Architecture
多个 Agent 安全协作：Planner → Developer → Tester → Reviewer → Deploy。
每个 Agent 拥有：独立身份、独立权限、独立能力范围。可复用 R6.3 RBAC 授予/继承/吊销。

### Phase R8 —— Autonomous Development Loop
AI 软件工程闭环：发现问题 → Issue 生成 → 任务规划 → 代码修改 → 测试 → Review → 部署 → 验证。

### Phase R9 —— Controlled Self Evolution
ANIMA 可优化自身：检测不足 → 提出方案 → 创建 Patch → Sandbox 测试 → 生成 Evidence → Owner 审批 → 生产更新。
原则：**Self improvement ≠ Self authorization**。

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