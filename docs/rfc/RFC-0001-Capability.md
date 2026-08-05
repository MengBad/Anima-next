# RFC-0001: Capability Layer

- **Status:** Challenge — **APPROVED** (Infrastructure Gate; anchor `TBD-anchor`)
- **Date:** 2026-07-30
- **Authors:** LIMBot V2 / Senior Developer
- **Related:** RFC-0002 Task Contract · RFC-0003 Workflow DSL · RFC-0004 Agent Protocol · RFC-0005 Memory Protocol

---

## 1. Background / Problem

当前架构草案里，Agent 的调用链仍是：

```
Think → ToolManager → Playwright / Python / NapCat ...
```

这导致 **Agent 在推理时就已经"知道"具体工具的存在**。后果：

1. 换工具（Playwright → CDP / 远程浏览器，NapCat → 其他 QQ 协议）必须改 Agent 的 Prompt 与逻辑；
2. 同一能力有多种实现时（Search: Tavily / SearXNG / Bing），Agent 无法透明切换或降级；
3. 权限、超时、重试等策略散落在每个工具里，无法统一治理。

**核心问题：Agent 到底根据什么决定"自己能做什么"？** 答案应当是"能力（Capability）"，而不是"工具（Tool）"。

---

## 2. Goals

- G1 Agent **永远不直接引用任何 Provider**（Playwright / NapCat / Redis / Postgres …）。
- G2 Agent 只声明 `need: <capability>`，由 **Capability Resolver** 解析到具体 Provider。
- G3 Capability 按**能力域**组织，与实现解耦，词汇稳定。
- G4 Provider 可替换、可降级（一个 Capability 对应有序 Provider 列表）。
- G5 权限 / 超时 / 重试在 Capability Contract 上统一定义，Provider 只负责执行。
- G6 本层契约可直接生成实现（Resolver + ToolManager 接口由此 RFC 推导）。

---

## 3. Non-goals

- 不定义 Workflow DSL 文法（见 RFC-0003）。
- 不定义 Task 的数据结构（见 RFC-0002）。
- 不定义 Memory 读写协议（见 RFC-0005）。
- 不规定具体 LLM 推理方式（Reasoning Engine 见后续 RFC）。
- 不规定 Provider 内部实现（Playwright 怎么装、沙箱怎么起，由 Provider 自己负责）。

---

## 4. Core Concepts

| 概念 | 角色 | 稳定性 |
|------|------|--------|
| **Capability** | 稳定的"能力"抽象，如 `browser.page.extract` | 稳定层 |
| **Provider** | 具体实现，如 `playwright` / `remote_browser` | 可替换层 |
| **Capability Contract** | 该能力的输入输出 / 权限 / 超时 / 重试定义 | 稳定层（带版本） |
| **Capability Resolver** | 把 `need` 解析为可用 Provider 并执行/降级 | 运行时核心 |
| **Registry** | `capability → [providers]` 的路由表（配置驱动） | 配置层 |

调用链：

```
Agent(think)
   │  need: { type: browser.page.extract, args: {...} }
   ▼
Capability Resolver
   │  ① 查 Contract  ② 查 Registry 选 Provider  ③ 权限/超时/重试治理
   ▼
ToolManager (执行边界: 沙箱 / 权限)
   │
   ▼
Provider (playwright / remote_browser / ...)
```

**Agent 在任何环节都看不到 Provider 名。**

---

## 5. Data Model / Schema

### 5.1 Capability Contract（每个能力一份，带版本）

```yaml
# capabilities/browser.page.extract.yaml
id: browser.page.extract
version: 1.0.0          # semver；Resolver 按 major 匹配
domain: browser
description: 打开网页并抽取正文与标题，可选截图
stability: stable        # experimental | beta | stable | deprecated
                        #   Planner 默认只用 stable，experimental 需显式开启；
                        #   Plugin / 自生长能力继承此字段
input:
  url: { type: string, required: true }
  selector: { type: string, required: false }
output:
  title: string
  content: string
  screenshots: { type: list[image], required: false }
permission:            # 该能力要求 Provider 必须具备的权限域
  network: true
  filesystem: false
  exec: false
  secrets: false
timeout: 20s
retry: 2
```

> 注：`stability` 是**成熟度 / 生命周期**字段（属 Contract 层，随版本演进），不属运行时属性。`cost / latency / deterministic / side_effect / idempotent / cacheable / streaming` 等运行时属性统一定义在 **RFC-0001A Capability Metadata**，供 Planner 与 Resolver Scorer。Planner 默认只用 `stable`；`experimental` 能力除非显式请求否则不进入候选（防止未稳定能力进入生产路径）。

### 5.2 Provider Manifest（每个实现一份）

```yaml
# providers/playwright.yaml
id: playwright
implements:
  - { capability: browser.page.extract, version: 1 }
  - { capability: browser.page.open,    version: 1 }
  - { capability: browser.page.click,    version: 1 }
granted_scopes:       # 该 Provider 实际被授予的权限
  network: true
  filesystem: false
  exec: false
  secrets: false
health:                # 健康探测，用于降级决策
  check: http://localhost:3001/health
  timeout: 3s
sandbox: true          # 是否走沙箱执行
config:                # Provider 私有配置（不暴露给 Agent）
  headless: true
```

### 5.3 Capability Registry（路由表，配置驱动）

```yaml
# registry.yaml
routes:
  browser.page.extract:
    providers: [playwright, remote_browser]   # 顺序即优先级/降级序
  browser.page.open:
    providers: [playwright]
  search.web:
    providers: [tavily, searxng, bing]         # 多 Provider 自动降级
  python.exec:
    providers: [sandbox_python, local_python]
  qq.send:
    providers: [napcat]
```

### 5.4 Agent 的 `need` 表达（稳定词汇，进 Prompt 的只有 capability 列表）

```yaml
need:
  type: browser.page.extract
  args:
    url: "https://example.com"
    selector: "article"
# Agent 不写 provider；也不写 playwright
```

给 LLM 的"能力清单"由 Registry 推导（仅 capability id + description），**不含任何 Provider 名**。

---

## 6. Resolver Algorithm

核心变化（采纳评审建议）：选择策略从"固定顺序降级"升级为 **Candidate → Score → Pick**，Scorer 可插拔——Resolver 主流程不变，只换 Scorer 即可调整选型策略（如"成本优先"）。

```
function resolve(need):
    contract = load_contract(need.type)          # 按 major 版本匹配
    if contract is None: raise CapabilityNotFound(need.type)

    # ① Candidate：健康 + 权限达标才进入候选集（不参与排序）
    candidates = [
        p for p in registry[need.type]
        if provider_healthy(p) and satisfies(p.granted_scopes, contract.permission)
    ]
    if not candidates:
        raise NoProvider(need.type) if registry_empty else PermissionDenied(need.type)

    # ② Score：可插拔，见 §6.1
    ranked = scorer.rank(candidates, context=need.context)

    # ③ Pick：按分值从高到低执行，失败降级到下一个
    last_err = None
    for provider in ranked:
        for attempt in 1..(contract.retry + 1):
            try:
                result = tool_manager.invoke(
                    provider, contract, need.args, timeout = contract.timeout
                )
                return CapabilityResult(ok=True, data=result, provider = provider.id)
            except (TimeoutError, TransientError) as e:
                last_err = e; continue            # 瞬时错误：重试
            except ProviderError as e:
                last_err = e; break               # 逻辑错误：不重试，跳下一候选
    raise CapabilityUnavailable(need.type, cause = last_err)
```

要点：
- **Candidate 门槛**：Health + Permission 只决定"能否进候选集"，不参与排序。
- **Score 决定顺序**：同健康同权限下，谁先上由 Scorer 算分。
- **降级保留**：Pick 阶段仍按顺序失败降级（与候选集内排名一致）。
- **Agent 不可见**：返回结果里 `provider` 字段仅供日志/可观测，不回灌 Agent 决策。

### 6.1 Scorer（可插拔）

默认 Scorer 综合四维度加权（权重在配置中，可整体替换；维度数据见 RFC-0001A）：

| 维度 | 权重 | 数据来源 |
|------|------|----------|
| Success Rate（近期成功率） | 40% | Provider 健康统计 |
| Health（实时健康） | 30% | `provider.health` 探测 |
| Latency（延迟） | 20% | Capability Metadata `latency` |
| Cost（成本） | 10% | Capability Metadata `cost` |

```
score = 0.4*success_rate + 0.3*health + 0.2*latency_score + 0.1*cost_score
```

- **只换 Scorer**：要改选型策略，替换 Scorer 实现即可，Resolver 主流程不动。
- **维度可扩展**：未来加"地域/合规"等维度，扩 Scorer 不改 Resolver。

---

## 7. Permission Model

权限域（最小集合，可扩展）：

| scope | 含义 |
|-------|------|
| `network` | 允许出网 |
| `filesystem` | 允许读写本地文件 |
| `exec` | 允许执行进程/代码 |
| `secrets` | 允许读凭据（API key 等） |
| `user_interaction` | 允许向用户发消息/等待输入 |

规则：Provider 的 `granted_scopes` 必须是 Contract `permission` 的**超集**，否则该 Provider 对该能力不可用。

---

## 8. Error Semantics

| 错误 | 触发 | Agent 可见 |
|------|------|-----------|
| `CapabilityNotFound` | capability id 不存在 | 是（建议改写 need） |
| `NoProvider` | 该能力无任何 Provider 注册 | 是 |
| `PermissionDenied` | 所有 Provider 权限均不满足 | 是（说明能力受限） |
| `CapabilityUnavailable` | 全部 Provider 超时/失败 | 是（可提示降级或换思路） |
| `ContractViolation` | Provider 返回不符合 output schema | 否（内部，计入 Provider 健康） |

所有错误带 `diagnostics`（尝试过的 Provider、各步原因），仅进日志，不污染 Agent 上下文。

---

## 9. Versioning & Evolution

- **Capability Contract** 用 semver。`major` 变更 = 破坏性（输入输出结构变），Resolver 只匹配同 `major`。
- **新增能力**：加 Contract + 在 Registry 注册 Provider，Agent 能力清单自动出现，**无需改 Agent 代码**。
- **下线 Provider**：从 Registry 移除即可；能力仍可用（走其他 Provider）或降级为 `NoProvider`。
- **废弃能力**：Contract `stability: deprecated`，Agent 清单逐步移除；新代码不应再 `need` 该能力。
- **破坏性升级**：发新 major 版 Contract，新旧并存，Provider 各自声明实现的版本。

---

## 10. Examples

### 10.1 browser.page.extract（多 Provider 降级）
见 §5.1 / §5.2 / §5.3。`playwright` 不健康时自动切 `remote_browser`。

### 10.2 search.web（三路降级）
```yaml
search.web:
  providers: [tavily, searxng, bing]
```
Tavily 配额耗尽 → SearXNG → Bing，Agent 无感。

### 10.3 python.exec（沙箱优先）
```yaml
python.exec:
  providers: [sandbox_python, local_python]
```
默认走 `sandbox_python`（隔离）；沙箱不可用时降级 `local_python`（需 `exec` 权限且 `sandbox:false`）。

### 10.4 qq.send（通信域，单 Provider）
```yaml
qq.send:
  providers: [napcat]
```
换 QQ 协议只需在 Registry 把 `napcat` 换成新 Provider，**Agent Prompt 零改动**。

---

## 11. Integration Points

- **Agent（RFC-0004）**：只产出 `need`；接收 `CapabilityResult`。
- **ToolManager（原方案六/十二）**：本层调用它执行 Provider，沙箱/权限由它落地。
- **Workflow DSL（RFC-0003）**：step 写 `use: browser.page.extract`，Resolver 在运行时解析，DSL 不出现 Provider 名。
- **Scheduler / Reflection**：可基于 `CapabilityUnavailable` 统计驱动 Reflection（如"search.web 近 1h Tavily 失败率高 → 调整 Registry 权重"），呼应 Reflection Engine。

---

## 12. Known Limitations

1. Resolver 的"选哪个 Provider"仅支持**有序降级 + 健康探测**，不做负载/质量智能路由（后续可加权重/打分，不在本 RFC）。
2. Capability 词汇需**人工策展**，不会自动涌现；滥发 capability 会膨胀 Agent 上下文。
3. LLM 仍需要在 Prompt 中拿到 capability 清单——清单过长需裁剪（建议按任务域动态注入）。
4. 跨 Capability 的事务一致性（如"发消息 + 写记忆"要么都成要么都败）不在本层，归 Task/Workflow 管。

---

## 13. Open Questions

- Q1：Provider 健康探测失败是否立即摘流，还是按错误率滑动窗口？（建议后者，待 RFC-0002 定 SLO）
- Q2：`secrets` 作用域如何从配置中心注入 Provider，而不落 Registry？（建议 secrets-manager，不在本 RFC）
- Q3：Capability 调用是否计成本/预算，供 Reasoning Engine 做取舍？（建议计，字段待 RFC-0004）

---

## 14. Evolution / Dependent RFCs

依赖顺序（先稳契约，再长实现；评审后于 RFC-0001 与 RFC-0002 间插入 RFC-0001A）：

```
RFC-0001  Capability Layer        ← 本 RFC（先做，决定其余形状）
   └─ RFC-0001A Capability Metadata   ← 必须早于 RFC-0002：Task 依赖 Need，Need 依赖 Metadata
        └─ RFC-0002 Task Contract       Task 即 DAG，steps 引用 capability id
             └─ RFC-0003 Workflow DSL     step.use 引用 capability id
                  └─ RFC-0004 Agent Protocol   Agent 只发 need / 收 result
                       └─ RFC-0005 Memory Protocol  memory.read/write 也是 capability
```

RFC-0001A 稳定后，RFC-0002 才能落定（Task 的 Need 需携带 Metadata 供 Planner 推理）。本 RFC 通过后即可开始实现 Resolver + ToolManager 骨架；Scorer 默认实现随 RFC-0001A 的 metadata 一并落地。

---

## 15. Acceptance Criteria（本 RFC 通过标准）

- [ ] Resolver 能从 `need` 解析并执行，Agent 侧无 Provider 名；
- [ ] 单 Provider 超时/不健康时自动降级到下一个；
- [ ] 权限不足时该 Provider 被跳过并记日志；
- [ ] 新增/替换 Provider 不需改 Agent 代码，仅改 Registry；
- [ ] 错误带 diagnostics 且不污染 Agent 上下文；
- [ ] 至少 3 个能力（browser / search / python）有可运行 Provider 验证端到端。

---

## 16. Phase C Challenge Record (Infrastructure Gate)

> **Challenge Result: APPROVED (Infrastructure Gate).** All four boundary checks PASS; no Ontology extension, no new Authority, design unchanged. Anchor pending (see `anchor: TBD-anchor`).

### 16.1 Check Results

| Check | Result | Evidence |
|-------|--------|----------|
| Q1 Capability Boundary | PASS | §4 (Capability = stable capability abstraction, not Provider wrapper / Tool / Agent resource / Goal state / Life state), §5.1 (Contract = semver'd input/output/permission/timeout/retry), §5.4 (`need` carries only capability type); call chain `Need → Capability Contract → Resolver → Provider` (§4 lines 61–72); Goal never appears in the chain. RFC-0100 §1.18 (Capability = declared need-type; Life names Capabilities, never Providers). |
| Q2 Resolver Authority | PASS | §6 `resolve(need)` only loads contract → filters candidates (health+permission) → scores → picks → invokes via ToolManager, returns `CapabilityResult`. §6.1 Scorer reads only success/health/latency/cost metadata. §8 errors are Capability-level; `provider` field "仅供日志/可观测，不回灌 Agent 决策" (§6 line 203). Resolver mutates no Goal / Identity / Memory / Need. |
| Q3 Provider Isolation | PASS | §5.2 Provider Manifest (`implements` / `granted_scopes` / `health` / `sandbox` / private `config`); replaceable + sandboxed + contract-bound. §7 `granted_scopes` must be a superset of Contract `permission`. §4 line 74 "Agent 在任何环节都看不到 Provider 名". RFC-0100 §1.19 (Provider "not part of the Life… swapping it MUST NOT change Identity, Need, or Memory") + P1/P2 bidirectional isolation. Provider reads no Identity/Goal, writes no Memory, affects no Drive, is a black-box execution-boundary implementation, not a hidden Life actor. |
| Q4 Metadata Boundary | PASS | §5.1 note: `cost/latency/deterministic/side_effect/idempotent/cacheable/streaming` live in RFC-0001A "供 Planner 与 Resolver Scorer" — runtime properties for routing/scoring only. §6.1 Scorer consumes `latency`/`cost` for ranking. RFC-0001A defines metadata strictly for resolution / routing / scoring / compatibility. No path metadata → Identity → Preference → Learning; `stability` (maturity) is explicitly separated as a Contract-layer field, not Life knowledge. |

### 16.2 Foundation Gate

> **Foundation Stable? → YES.** RFC-0001 consumes the Foundation (RFC-0100 §1.18 / §1.19 / §2 / P1 / P2); it introduces **no new Ontology noun** and **no new Life Authority** (its Authority model — Capability ownership §5.1/§5.3/§9, Resolver authority §6/§6.1/§8, Provider boundary §5.2/§7 — is intentionally placed *outside* the Life O1 matrix, consistent with P1/P2 isolation). No Foundation patch required at the document level. The Infrastructure Track (0001 → …) remains separate from the CLOSED Life Validation Track (0101 → 0111).

### 16.3 Anchor

> **anchor:** `TBD-anchor`
>
> Two-stage commit: (A) `docs(rfc): anchor RFC-0001 Capability Layer`; (B) `docs(rfc): stamp RFC-0001 anchor hash`. Document hash filled in stamp commit.
