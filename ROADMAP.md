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

## 进行中（IN PROGRESS）

- 无（R6–R9 主线全部完成并部署生产）。

## 下一阶段（NEXT）

### Phase 收尾运营（R9 UI 接入）
在 WebUI 工程/审批页接入 R9 `EvolutionLoop` 提案可视化与 Owner 一键审批（当前仅 API/测试，未接 UI）。

### Phase 生产实证补强
R8/R9 接真实 `issue_sources`（SelfCode 效果建议、审计 DENY 聚合）跑一周期，验证闭环能稳定收敛不退化。

### Phase 路线外增强
人格漂移执法评估、SelfCode 效果评估与建议卸载、大图哈希缓存、danger 收紧、fallback 压测。

### 明确不做
- 无审批自动安装。
- 默认不做语音模块（用户否决过）。

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