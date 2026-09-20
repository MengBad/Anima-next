# 运行时执行面现状：F5 `runtime/` · `pilot/` · 双轨归属

> 建于 2026-09-18（Phase C）。**本文是只读实测记录 + 待决清单，不是 RFC、不是授权书。**
> 它更正审查报告 §Phase C 的一处表述，并把"接线还是封存"的**决策**明确留在 owner 手上。

---

## 0. 本文更正的结论

审查报告 §6 Phase C 与工程债台账 D-14 曾写：

> 「F5 `runtime/` + `pilot/`（**42 文件，零生产足迹**）接线还是封存」

该表述**混淆了"代码"与"产物"**。2026-09-18 实测订正：

| 层 | 实测结论 |
|---|---|
| **模块层** | `runtime/` + `pilot/` 共 **39 个模块**；其中 **31 个有生产（非测试）导入者**，含 `anima/cli.py`、`anima/tools/__init__.py`、`anima/pipeline/stages/memory.py`、`anima/cognitive/sylann_suggestion_controller.py`、`anima/capability/governance/gateway.py` |
| **产物层** | `data/{workflow_runs,execution_ledger,pilot,observation}` **不存在**（本机 + 生产双查） |

⇒ "零足迹"**只在产物层成立**，在模块层不成立。
⇒ **`SEALED`（封存＝停止维护甚至删除）对这个子集不是合法选项**：动它会直接打断生产入口的 import。

---

## 1. 模块层取证（包外生产导入者 = 承重）

探针：对 `anima/**` 全量按 `^\s*(from|import)\s+<module>` 做静态可达性判定，再与 `tests/**` 分池。

| 生产导入方 | 被导入的 `runtime/` · `pilot/` 模块 | 位置 |
|---|---|---|
| `anima/cli.py`（生产入口） | `runtime.persistence.WorkflowRunStore`、`runtime.recovery_module.RecoveryModule` | `:342-343`，在 `core.start()` 的 restore 循环里 `register(RecoveryModule(...))` |
| `anima/tools/__init__.py`（工具注册表） | `runtime.capability_resolver.{CapabilityRegistry,CapabilityNeed}`、`runtime.providers.python_exec.PythonExecPolicy` | `:584`、`:609`、`:782` |
| `anima/capability/governance/gateway.py`（F8 治理网关） | `runtime.capability_resolver.*`、`runtime.providers.base.ProviderResult` | `:38`、`:43`、`:146`… |
| `anima/tools/capability_fs.py` · `capability_web.py` | `runtime.capability_resolver`、`runtime.providers.base` | 见各文件 import 段 |
| `anima/pipeline/stages/memory.py`（**语料管线**） | `pilot.conversation_evidence.hash_user_id` | `:105` |
| `anima/cognitive/sylann_suggestion_controller.py` | `pilot.safety_filter.{SafetyFilter,SafetyResult}` | `:32` |

其余 20 余个模块的导入者是**包内**（`runtime/*` ↔ `runtime/*`、`pilot/*` ↔ `pilot/*`）+ 测试。
即：这些模块构成一个**自洽但未被任何生产调用点驱动**的子系统 —— 这与"死代码"不是一回事。

---

## 2. 产物层取证（真正零足迹的部分）

| 产物目录 | 是否存在 | 含义 |
|---|---|---|
| `data/execution_ledger/` | ❌ | `ExecutionLedger` 从未被写入 |
| `data/workflow_runs/` | ❌ | 无任何 `WorkflowRun` 落盘 |
| `data/pilot/` | ❌ | 无 pilot 记录 |
| `data/observation/` | ❌ | 无观测产物 |

⇒ 模块被 import，但**没有任何生产调用点驱动 `start_run` / ledger 落盘**。
这也解释了为什么 `M0.x` / F5 的全部验证都只能停在"单测通过"。

---

## 3. 三个面的裁定（2026-09-18 收口 · 债 D-14）

| # | 面 | 模块状态 | 产物状态 | **裁定** |
|---|---|---|---|---|
| C-1 | `runtime/` 执行基建（含 `providers/`、`capability_resolver`） | **已接线** | 零 | **(b) 保持休眠** + 本文明示"已接线、未驱动" + **停止在该面追加功能**（见下方策略声明） |
| C-2 | `pilot/`（`canary*`/`entry_gate`/`message_adapter`/`identity_*`…） | **部分接线**（`conversation_evidence` 与 `safety_filter` 已被生产用） | 零 | **逐模块**：已承重的按承重维护；未驱动的并入 (b) |
| C-3 | RFC-0002 / RFC-0003（已 APPROVED，无实现） | 未实现 | 无 | **`Deferred`**（正式回退，见两份 FINAL-SCOPE 的 Status update） |

**为何不是 (a) 接 producer**：任何"接线"都会新建生产写入路径（`start_run` / ledger 落盘），
而当前**没有消费方**——`drive_gate` 在生产不存在（默认 `False`），宪法链对线上产出为 0。
先接 producer 等于制造第二批"零产物基建"，且会绕开独立授权窗口。⇒ 判 (a) 不成立，取 (b)。

### 3.1 策略声明（对该面的处置纪律）

自 2026-09-18 起，对 `runtime/` 与未承重的 `pilot/` 模块：

1. **不新增功能**。该面处于"已接线、未驱动"，新增代码只会扩大无人验证的表面积。
2. **允许的改动只有三类**：修 bug（尤其 ImportError 类，见 `deploy/check_orphan_imports.py`）、
   修文档、删死代码。任何"加能力"的改动需先有 C-1/C-2 的重新裁定。
3. **`SEALED`（封存/停维护）不是合法选项** —— 39 个模块中 31 个有生产导入者，
   封存会直接打断 `cli.py:342-343` / `tools/__init__.py:584,609,782` /
    `pipeline/stages/memory.py:105` / `sylann_suggestion_controller.py:32` /
   `governance/gateway.py:38` 的 import。
4. **改动前先跑一次产物面检查**（§6 命令②）：若 `data/{workflow_runs,execution_ledger,pilot,observation}`
   开始出现，说明有东西已在驱动该面 —— 那时本声明作废，必须重新裁定。

> 一句话：**模块可达、无人驱动 —— 保持这个状态，别往里加东西，也别封存它。**

---

## 4. 双轨映射（A 族 / B 族）

项目里长期并存两套平行实现，命名相近、活跃度相反。排查时**先确认读的是哪一族**：

| 概念 | **A 族** = `anima/constitution/`（RFC-0100 生命层，理论已冻结） | **B 族** = 运行时（`cognitive/` + `memory/` + `agent/` + `pipeline/`，真正驱动 bot） | 现状 |
|---|---|---|---|
| Memory | `MemoryEngine` / `MemoryAuthority`（水合 chat_memory → episode） | `memory/chat_memory.py`、`memory/crossgroup/*` | A 族**读 + 反思沉淀写**：D-13 (a) 已接生产 producer（开关默认关） |
| Identity | `Identity = derive_identity(Memory)`（纯函数、无 setter） | `config/persona_core.yaml` + `generation.py` 注入器链 | A 族**半悬空**：仅 Drive 桥调用，且链首曾是替身（D-01） |
| Affect | `Affect = derive_affect(Need, Perception)` | `cognitive/mood.py`（arousal/valence 标量） | 两套并存；A 族只被 Drive 桥读 |
| Drive | `Drive = derive_drive(Affect)` | **无对应物** | A 族独有；生产 `drive_gate=False` ⇒ 整链不执行 |
| Reflection | `ReflectionResult → MemoryAuthority.ingest` | `cognitive/rumination*` | A 族闭环**已接**（D-13 (a)）：`constitution/reflection_loop.py`，开关 `autonomy.constitution_reflection_loop` 默认关。⚠️ **数值 trait 通道仍缺**（D-13 第二层）⇒ `identity_traits` 恒 0 |
| Planning / Task | `Plan` / `Task` / `WorkflowRun`（`constitution/state.py`） | `agent/planner.py`、`agent/devloop.py` | A 族未驱动 |

**一句话**：A 族是"已经写好的理论层"，B 族是"实际在跑的工程层"；两者不是新旧替代关系，
`_constitution_drive` 是唯一的官方接口，且它默认关闭。

---

## 5. 命名 / 路径一致性（非破坏性处置）

| 项 | 曾经的表述 | 事实 | 处置 |
|---|---|---|---|
| F8 治理 sink 路径 | 文档写 `data/resolution_events.jsonl` | 真实为 `data/governance/resolution_events.jsonl`，目录由 `capability_layer.governance_data_dir` 决定 | **只改文档**（A3）。**不改真实路径** —— 改路径会让已积累的事件失联，属破坏性操作 |
| `health_history` | 有的地方写成 `data/governance/health_history.json` | 真实为 `data/health_history.json`（内存对象 `core.health_history` ↔ 落盘文件同名不同物） | 只做文档消歧，不动文件 |
| 两个 `capability` | —— | `anima/tools/capability.py` = **自生长能力**；`anima/capability/` = **F8 治理** | 命名保留（改名代价大于收益），排查手册加"先分清哪层"（D-15） |

**通则**：路径类不一致，能改文档就不改路径；路径 = 历史数据的地址。

---

## 6. 复现本文结论的命令

```bash
# ① 模块层：哪些 runtime/pilot 模块被 anima/ 引用（非 tests/）
python -c "……见 reports/2026-09-18_phase_c_d_runtime.md 附录探针……"
# ② 产物层
ls -d data/execution_ledger data/workflow_runs data/pilot data/observation   # 均应不存在
# ③ 承重 import 直证
grep -n 'anima.runtime' anima/cli.py anima/tools/__init__.py
grep -n 'anima.pilot' anima/pipeline/stages/memory.py anima/cognitive/sylann_suggestion_controller.py
```
