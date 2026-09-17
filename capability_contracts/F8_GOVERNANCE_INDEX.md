# F8 Capability Governance — Series Index

> F8 系列治理能力建设索引。本文件为导航与一致性锚点，不实现、不改代码。
> 配套：正式 RFC `f8.13_convergence_rfc_v0.1.md`（收敛边界裁定）、只读勘察 ADR `f8.13_convergence_scope_freeze_v0.1.md`。

## 设计铁律（全系列贯穿）

```
ResolutionEvent
    ≠
HealthEvidence
    ≠
RecoveryDecision
    ≠
LifecycleTransition
```

- 三条链**职责正交、无重复存储语义**，各自 append-only store，长期共存、不合并。
- `RecoveryDecision` 不单独持久化（是 `LifecycleTransition.reason` 的结构化投影）。
- `CapabilityContract` 字段集 **HARD FREEZE**（RFC-0001A）。
- `resolve()` **不读 lifecycle**；governance 是旁路 RECORD/observer，不是执行控制方。
- **不引入** Convergence runtime layer / daemon / timer / polling。

## 三链模型

```
Resolution 链 : resolve() → ResolutionEvent  ──► (F8.7 AuditStore, opt-in)
Health 链     : governed execution → NormalizedExecutionOutcome(F8.11) → HealthEvidence ──► HealthEvidenceStore
Recovery 链   : HealthEvidence → HealthPolicyEvaluator → RecoveryDecision → HealthObserver → TransitionStore
```

## F8.x 文档映射

> ⚠️ **2026-09-18 核对订正（审查 P1-5）**：本表此前列出 F8.4–F8.12 的**具体文件名**，
> 读起来像"这些文档就在 `capability_contracts/` 下"，实际**它们不在本仓库**。
> 已用两条独立方法核对：
> 1. `capability_contracts/` 目录枚举 —— 只有 `f8.13_convergence_rfc_v0.1.md` 与 F9.x / phase* 系列；
> 2. `git log --all --name-only -- '*f8.*'` —— 历史中**只出现过 `f8.13`**（提交 `afea135`）。
>
> ⇒ 结论：F8.1–F8.12 的独立 scope-freeze 文档**既不在工作树、也不在本仓库的 git 历史里**
> （可能产生于当时的独立工作区）。本表保留编号与状态（它们记录了治理进程），
> 但**文件名一栏改为诚实标注**，不再给出不存在的路径。
> 如需追回：本机 git 历史已两次损坏，`reports/2026-09-15_git_incident_and_p3_delivery_2026-09-15.md`
> 记载了当时的恢复范围；若仍找不到，应视为**已丢失**，而不是继续当成可引用依据。

| 编号 | 主题 | 文档在库？ | 状态 |
|---|---|---|---|
| F8.1–F8.2 | capability governance 早期切片（概念前驱） | ❌ 无独立文档（本就是概念引用） | 概念引用 |
| F8.3 | Resolution 审计（Selection Audit） | ❌ 不在库（原引用自 `f8.4`/`f8.13`） | 已闭环 |
| F8.4 | Scope Freeze Proposal | ❌ **不在库** | 已闭环 |
| F8.5 | Scope Freeze Proposal | ❌ **不在库** | 已闭环 |
| F8.6 | Scope Freeze Proposal | ❌ **不在库** | 已闭环 |
| F8.7 | AuditStore（持久 sink，opt-in） | ❌ **不在库** | 已闭环 |
| F8.8 | Lifecycle / Recovery Observer（QUARANTINED 方向） | ❌ **不在库** | 已闭环 |
| F8.9.1 | sub-slice | ❌ **不在库** | 已闭环 |
| F8.9.2 | sub-slice | ❌ **不在库** | 已闭环 |
| F8.9.3 | sub-slice（evidence 接入点） | ❌ **不在库** | 已闭环 |
| F8.10 | Health Signal Boundary | ❌ **不在库** | 已闭环 |
| F8.10.1 | Evidence Emission（注入点） | ❌ **不在库** | 已闭环 |
| F8.11 | Execution Outcome Normalization（三态） | ❌ **不在库** | 已闭环 |
| F8.12 | Recovery（QUARANTINED→ACTIVE 决策层） | ❌ **不在库** | 已闭环（实现 `3667441`） |
| **F8.13** | **Convergence Boundary（收口）** | ✅ `f8.13_convergence_rfc_v0.1.md` + 本索引 | **✅ 收口** |

## 收口裁定（F8.13）

- Convergence = 仅文档级收敛，**不需要新抽象、不需要实现**。
- 三条链按设计正交共存；`RecoveryDecision` 已是 Lifecycle command projection。
- evaluator / observer / audit 边界已由 R1/R2/R3 + 唯一 `transition_to` 写入点冻结。
- `CapabilityContract` HARD FREEZE；`resolve()` 不读 lifecycle；governance 继续作为旁路记录与决策系统。
- 不引入 Convergence runtime layer。

## F9 — Operational Validation（运行验证层，COMPLETE）

> F9 不建设、不演化设计；只验证 F8 冻结态在真实运行下是否成立。
> 全部 slice 纪律：只读 / 默认 OFF / 无代码演化 / 无 Foundation Evolution / 不 commit code（除本收口文档提交）。

| 编号 | 主题 | 文档 | 状态 |
|---|---|---|---|
| F9.1 | 只读验证（生产面 0 样本，INCONCLUSIVE BY CONSTRUCTION） | `f9.1_operational_observation_report_v0.1.md` + `f9.1_operational_validation_scope_freeze_v0.1.md` + `f9.1_contradiction_backlog_v0.1.md` | 已闭环 |
| F9.2 | Operational Activation（激活可观测接缝，默认 OFF，四零 diff，单提交 `61e0ee1`） | `f9.2_operational_activation_scope_freeze_v0.1.md` | 已闭环 |
| F9.3 | Observation Window（短窗 5 executions，链路存在） | `f9.3_observation_report_v0.1.md` + `f9.3_scope_decision_v0.1.md` | 已闭环 |
| F9.4 | Longitudinal Observation（长窗 20 executions，稳定性） | `f9.4_observation_report_v0.1.md` + `f9.4_scope_decision_v0.1.md` | 已闭环 |
| **F9 收口** | **Operational Validation Final Report** | **`f9_operational_validation_final_report_v0.1.md`** | **✅ COMPLETE** |

### 收口裁定（F9）

- F8 架构经 F9 全 slice **运行验证成立**：三链正交、1:1 关联、默认休眠、数据边界、无自动恢复。
- 累计真实运行样本：Resolution 25 / Health 25 / Recovery 0；**0 observed contradiction**；**0 frozen invariant violation**。
- Foundation Evolution trigger **NOT MET** → Foundation **FROZEN**，不进 F1–F4。
- 重启条件见 `foundation_evolution_readiness_note_v0.1.md`（仅 observed contradiction / invariant violation / scaling pressure / security boundary failure 才合法）。

## Onboarding Entry（运维接管入口）

> 需要「接管 / 继承 / 运行维护」本仓库时，从仓库根的
> [`ANIMA_NEXT_OPERATOR_CONTEXT.md`](../ANIMA_NEXT_OPERATOR_CONTEXT.md) 一次性加载：
> 项目状态继承 + 不可变原则 + 禁止清单 + Agent 角色分层 + 启动流程 + 授权协议。
> 配套工作协议见仓库根 [`AGENT_IDENTITY.md`](../AGENT_IDENTITY.md)
> （行为原则 + 固定汇报格式 `事实/证据/判断/风险/下一步` + 禁止短语）。
> 本索引不重复这些约定，只作导航锚点。

## 引用一致性

- f8.13 显式引用 `F8.1 / F8.3 / F8.7 / F8.8 / F8.10 / F8.10.1 / F8.11 / F8.12`，与本索引编号一致。
- 铁律表述（R1/R2/R3、`RecoveryDecision` 不落 store、`resolve()` 不读 lifecycle、HARD FREEZE、无 daemon）在 f8.12 与 f8.13 间无冲突。
