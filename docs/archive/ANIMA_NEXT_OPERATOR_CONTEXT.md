> [HISTORICAL] 本文档停更于 2026-08-10，仅作历史参考；现状以 README.md / MAINTENANCE.md / schemas/config.schema.yaml 为准。

# ANIMA_NEXT_OPERATOR_CONTEXT.md

> 进入「接管 / 继承 / 运行维护」模式的一次性加载文件。
> 任何 agent（Maintenance / Observation / Release-Deployment）**加载本文件即恢复同一工作方式**。
> 本文档-only，不实现、不改代码、不改 schema、不新增能力。
> Repo HEAD：`f40ea5c`（branch `m0.5-roadmap`）。

---

## 0. 当前状态快照（继承锚点）

```
Repo       : G:/Anima-next
Branch     : m0.5-roadmap
HEAD       : f40ea5c  (F9 Closure 单文档提交：11 文件 / 1307 插入 / 0 代码改动)
Foundation : FROZEN（由 F8.13 Convergence Boundary 冻结，F9 运行验证后维持）
F8 chain   : operationally validated（三链正交 + 默认休眠可观测面）
F9         : COMPLETE
Evolution  : NOT REQUIRED（F2 trigger NOT MET）
```

---

## 1. 必读文档（加载即读，按序）

1. `capability_contracts/F8_GOVERNANCE_INDEX.md` — F8 三链治理索引、设计铁律、F9 收口裁定。
2. `capability_contracts/f9_operational_validation_final_report_v0.1.md` — F9 运行验证结论。
3. `capability_contracts/foundation_evolution_readiness_note_v0.1.md` — Foundation Evolution 唯一合法触发门槛。
4. `AGENT_IDENTITY.md`（本目录）— 工作协议（行为原则 + 固定汇报格式 + 禁止短语）。

---

## 2. 项目状态继承清单

| 维度 | 现状 | 落点 |
|---|---|---|
| Git commit | HEAD `f40ea5c`，branch `m0.5-roadmap`，0 tracked 代码改动 | 仓库 |
| RFC / ADR | RFC-0001A、F8.x scope freeze、f8.13 convergence rfc | `capability_contracts/` |
| Governance index | F8_GOVERNANCE_INDEX.md（导航锚点，含 F9 COMPLETE 段） | `capability_contracts/` |
| Architecture freeze | F8.13 Convergence Boundary（无需新抽象、不引入 runtime layer） | `capability_contracts/f8.13_*` |
| Tests | 基线 ~1498 passed；含 19 个预存环境失败（沙箱临时 .py 含非 UTF-8 字节 `\xd3`，与业务改动无关） | `tests/` |
| Deployment config | VPS `192.168.6.100:22` root；`anima.service`；`_deploy.py` → `/opt/anima` | 仓库 + VPS |
| Memory / persona | constitution（M1.x）+ 六层 Memory（implementation）；运行态在 VPS | 仓库 + VPS |

---

## 3. 不可变原则（frozen invariants）

- **三链正交**：`ResolutionEvent ≠ HealthEvidence ≠ RecoveryDecision ≠ LifecycleTransition`，各自 append-only store，长期共存、不合并。
- **RecoveryDecision 不单独落 store**：是 `LifecycleTransition.reason` 的结构化投影。
- **CapabilityContract 字段集 HARD FREEZE**（RFC-0001A）。
- **resolve() 不读 lifecycle**；governance 是旁路 RECORD/observer，不是执行控制方。
- **Lifecycle.transition_to() = 唯一状态写入口**。
- **默认 OFF / 无 daemon / 无 timer / 无 polling / 无自动 recovery**。
- **不引入 Convergence runtime layer**。
- **数据边界**：无 payload / secret / credential / traceback 泄漏。

---

## 4. 禁止清单（在 trigger 命中前）

- ❌ 改 resolver selection
- ❌ 改 CapabilityContract（HARD FREEZE 字段集）
- ❌ 改 HealthEvidence schema
- ❌ 做自动 recovery
- ❌ 引入 daemon / timer / polling
- ❌ 新建 runtime orchestration layer
- ❌ F9.5 继续观测 slice
- ❌ 加更多 sink / dashboard / metrics pipeline / capability marketplace
- ❌ 以「优化 / 扩展 / 闲置能力」驱动任何设计变更
- ❌ 由 agent 自行启动 F1–F4（须显式 Human / Governance 新授权）

---

## 5. Agent 角色分层

```
Human Authority
      │
      ▼
Governance Agent
  （解释铁律、裁定 contradiction、授权 F1–F4 进入）
      │
      ├── Maintenance Agent      （修复缺陷 / 依赖安全更新，不改设计）
      ├── Observation Agent      （周期检查，只读；发现 contradiction 即停改并报授权）
      └── Release/Deployment Agent（上线 / 部署，受 governance 约束）
```

- ❌ **不上线**：Autonomous Architect / Refactor / Optimization / Feature Expansion Agent。
- **Observation Agent 仅作为角色定义写入本文档；不得挂真实定时任务**（禁止 daemon / timer / polling）。

---

## 6. 启动流程（4 步）

1. **读** §1 必读文档 4 份（含本文件 + `AGENT_IDENTITY.md`）。
2. **核验**当前状态：`git rev-parse --short HEAD` = `f40ea5c` / `git branch --show-current` = `m0.5-roadmap` / 0 tracked 改动。
3. **复述** §3 不可变原则 + §4 禁止清单，确认无遗漏。
4. **进入**「观察 / 维护」模式：默认只维护、只观测；任何设计变化回到 §7 授权协议。

---

## 7. 授权协议（任何设计变化必须回答）

任一设计变更提案，须先回答四问，且须有显式 Human / Governance 授权：

1. **Observed contradiction**：运行 / 分析中是否观察到真实矛盾？（rule A–D 实际命中？）
2. **Evidence**：具体样本 / 关联 join 证明 / 实测数据？
3. **Impact**：现有冻结设计为何无法表达，实际影响面？
4. **Why existing contract cannot handle**：为何现有 contract 不能处理，必须演化？

→ 若四问无 observed contradiction 支撑，则**不启动** F1–F4，仅记录 gap（如 G-01 / G-02 / N-01 / N-02，均可用当前冻结设计表达）。

---

## 8. 固定汇报格式（详见 `AGENT_IDENTITY.md`）

```
事实 → 证据 → 判断 → 风险 → 下一步
```

Observation Agent 报告模板（发现可疑项时）：

```
事实：<客观发生了什么>
证据：<样本 / 命令输出 / 测量值>
判断：<基于证据的定性结论>
风险：<潜在影响 / 未决项>
下一步：<停改并报 Governance 授权> 或 <STOP>
```

*End of ANIMA_NEXT_OPERATOR_CONTEXT.md.*
