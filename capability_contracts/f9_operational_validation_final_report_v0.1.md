# F9 — Operational Validation Final Report (v0.1)

> 文档-only 收口。性质：不改代码 / 不改 schema / 不改 runtime / 不新增能力 / 不 commit code。
> 本文件收敛 F9.1–F9.4 的全部观察结果，形成单一治理结论。
> Repo HEAD：`61e0ee1`（branch `m0.5-roadmap`），F9 期间 0 tracked 代码改动。

---

## 0. Executive verdict

```
F8 architecture   : VALIDATED OPERATIONALLY
F9                : COMPLETE
Foundation        : FROZEN
Evolution trigger : NOT REQUIRED (no F2 contradiction)
```

F8 治理能力链（Resolution / Health / Recovery 三链正交 + 默认休眠可观测面）经 F9 全 slice
验证：**从「结构性未运行」走到「真实运行可观测且零矛盾」**，且其全部冻结不变量在短窗与
长窗下均保持成立。无 observed contradiction，无 frozen invariant violation，无需进入 F1–F4。

---

## 1. Observation Windows（F9.1 → F9.4）

| Slice | 模式 | 窗口 | Resolution | Health | Recovery | 结论 |
|---|---|---|---|---|---|---|
| **F9.1** | 只读勘察（生产面） | 0 s（治理链从未运行） | 0 | 0 | 0 | INCONCLUSIVE BY CONSTRUCTION（非矛盾，是裁定态） |
| **F9.2** | 激活接缝（默认 OFF） | — | 构造可用 | 构造可用 | 构造+dormant observer | 四零 diff，单提交 `61e0ee1` |
| **F9.3** | 短窗观察 | 5 executions | 5 | 5 | 0 | 链路存在，1:1 关联，0 矛盾 |
| **F9.4** | 长窗观察 | 10 纪元 / 13.666 s / 20 executions | 20 | 20 | 0 | 稳定性，0 漂移，0 矛盾 |

> F9.1 的「0 样本」根因是**设计裁定态**（sink 从未被授权激活），不是矛盾。F9.2 把可观测面
> 从休眠转为可用（默认仍全 OFF），F9.3/F9.4 才第一次在真实 governed execution 路径下采集到
> 非零样本。

---

## 2. Evidence Volume

- **F9.1**：生产 0 / 测试 177 passed（仅单元测试表面，非运行证据）。
- **F9.3**：真实 5 governed executions → 5 ResolutionEvent + 5 HealthEvidence + 0 LifecycleTransition。
- **F9.4**：真实 20 governed executions（10 纪元，1.5s 间隔，实测 13.666s 跨度）→
  20 ResolutionEvent + 20 HealthEvidence + 0 LifecycleTransition。
- **累积真实运行样本**：Resolution 25 / Health 25 / Recovery 0。

Recovery 链 0 样本是设计态（dormant seam 在观察期间从不调用 observer），非缺陷。

---

## 3. Contradiction Analysis

逐 slice 执行 rules A–D（selected↔health.success / 每条 health 必有 audit / 数据边界 /
三链正交性）：

| Slice | Rule A | Rule B | Rule C (data boundary) | Rule D (orthogonality) | contradictions |
|---|---|---|---|---|---|
| F9.1 | n/a（0 样本） | n/a | n/a | 设计裁定态 | 无矛盾（仅 G-01/G-02/N-01/N-02 gap，均非 F2 contradiction） |
| F9.3 | clean | clean | 0 leaks | clean | `[]` |
| F9.4 | clean | clean | 0 leaks | clean | `[]` |

- **F8.11 关键非矛盾**：failure 读同 `execution_key` 既 `success=true`（transport facet）
  又 `normalized_outcome=failure`（interpretation facet）—— 两种正交 facet，非 contradiction。
- **F9.1 的 G-01/G-02/N-01/N-02** 均可用当前冻结设计表达，无需设计变更，不满足 F2 触发条件。

**累计 Observed contradiction：NONE。**

---

## 4. Frozen Invariant Validation

| 冻结假设（F8.13 / F8 铁律） | F9.1 | F9.3 | F9.4 |
|---|---|---|---|
| 三链正交（`ResolutionEvent ≠ HealthEvidence ≠ LifecycleTransition`） | 设计裁定态 | VALIDATED | VALIDATED |
| `execution_key ↔ ResolutionEvent ↔ HealthEvidence` 1:1 可关联 | 未运行 | VALIDATED（5/5） | VALIDATED（20/20，跨完整窗口） |
| `RecoveryDecision` 不单独落 store / 不伪装 state mutation | 投影未调用 | VALIDATED（0 transition） | VALIDATED（0 transition） |
| 默认 OFF / 无 daemon / 无自动恢复 | 成立（含 G-01 缺口） | 成立 | 成立 |
| 数据边界（无 payload / secret / credential / traceback 泄漏） | 未运行 | VALIDATED | VALIDATED |
| 时间维度稳定性（无 drift / UNKNOWN 异常增长） | 未运行 | — | VALIDATED（单调、UNKNOWN=0） |

**全部冻结假设在可测量范围内成立；无 challenged 项。**

---

## 5. Foundation Trigger Decision

```
F2 触发条件：
  Observed contradiction = TRUE        → NO
  Frozen invariant broken = TRUE       → NO
  （scaling pressure / security boundary failure 在 F9 观察范围内未出现）

⇒ Foundation Evolution trigger = NOT MET
⇒ 不进入 F1–F4
⇒ Foundation remains FROZEN
```

F9 验证闭环到此完成：
```
F8 设计冻结 → F9.1 只读验证(无矛盾/无样本) → F9.2 激活接缝(默认OFF)
           → F9.3 短窗观察(链路存在) → F9.4 长窗观察(稳定性)
           → F9 COMPLETE → Foundation FROZEN → STOP
```

---

## 6. 纪律合规声明（F9 全程）

- 未修改任何代码 / schema / policy / sink 接线（F9.2 单提交 `61e0ee1` 为四零 diff）。
- F9.3 / F9.4 为只读观测，harness 落系统 temp、跑后 purge，不进仓库。
- 未引入自动恢复 / dashboard / metrics pipeline / capability marketplace / runtime orchestration。
- 未提 Foundation Evolution。
- 本报告 + F9.3/F9.4 报告为 untracked 文档；F8/F9 索引更新与 Foundation Evolution Readiness
  Note 一并随本收口单提交。

---

## 7. 后续入口（不启动）

下一阶段为**封存成果，不是继续开发**。唯一合法进入 F1–F4 的路径：

```
ONLY:
  - observed contradiction
  - invariant violation
  - scaling pressure
  - security boundary failure
NOT:
  - desire for optimization
  - desire for feature expansion
  - unused capability
```

详见 `foundation_evolution_readiness_note_v0.1.md`。

*End of F9 Operational Validation Final Report v0.1.*
