# 工具自生长计划 v1（用 Anima 设计语言落细）

> 目标：让生产 bot（Anima-next / OpenClaw）具备**自主学习调用工具**与**自主生成工具（能力）**两类能力，
> 全部复用现有架构（单一 system 消息契约、权限分级、人格漂移预算、跨群注入模式），不引入 AstrBot 依赖。
>
> 设计哲学对齐：`anima/cognitive/learning.py`（反思+漂移预算）、`anima/crossgroup`（后台抽取+单 system 注入）、
> next-gen `CapabilitiesMixin`（能力自生长+语义去重+健康维护，已验证可防"103 能力 / 0 使用"膨胀）。

---

## 0. 范围与不在范围

| 在范围 | 不在范围（本期） |
|---|---|
| P0 工具调用反馈闭环（学会"何时调已有工具"） | P2 自动生成 `handler_code` 并沙箱执行（高风险，owner 才放） |
| P1 自生长能力系统（LLM 提示词方法，安全执行） | 改 ReAct 核心调度逻辑 |
| 与现有认知引擎 / 跨群系统同构集成 | 换 LLM / 换网关 |

**核心设计决定**：P1 的"生成工具" = bot 长出的**方法论/提示词能力**（`name/description/how_to_use/parameters`），
执行时**重新 prompt LLM**（复用 next-gen `_execute_single_capability` 思路），**不 eval 任意代码**。
这把"自生长"的安全风险降到最低，且与 next-gen 已运行一年的形态一致。

---

## 1. 现状落点（真实文件:行）

| 能力 | 文件 | 关键 API |
|---|---|---|
| ReAct 循环 | `anima/agent/react.py:28` | `ReActLoop.run(messages, temperature=0.8, max_tokens=512)`；工具执行 `react.py:69-88`，仅 `logger.info` 记录 |
| 自学习 | `anima/cognitive/learning.py:75` | `SelfLearning.add_note(content, delta=0.05)` → `self_notes.md`；`ruminate(llm, ctx)`；`get_learning()`；`prompt_fragment()` |
| 工具注册 | `anima/tools/__init__.py:26` | `Tool(name, description, parameters, handler, min_level)`；`ToolRegistry.register(tool)`；`to_openai_schema()` 把所有参数标 `required` |
| 生成阶段 | `anima/pipeline/stages.py:346` | `generation_stage`：`reply = await react.run(...)`（`stages.py:375`） |
| 认知注入 | `anima/pipeline/stages.py:227` | `cognition_stage` 末尾把跨群片段追加进**同一** `system_prompt`（`stages.py:296-317`），合并为单一 system（`stages.py:324-327`） |
| 装配点 | `anima/cli.py:303` | `core.tools = create_default_registry(core)` |
| 去重（可移植） | `G:/Anima/anima/capability_dedup.py` | 零依赖：`normalize_capability_signature` / `find_similar_capability` / `text_similarity` |
| 能力模型（参考） | `G:/Anima/anima/mixins/capabilities.py` | `_create_or_update_capability` / `_apply_capability_feedback` / `_maintain_capabilities_health` / `_dynamically_register_capability_as_tool` |

---

## 2. 设计原则（来自 Anima 既有契约，必须遵守）

1. **单一 system 消息契约**：任何新上下文（能力提示）都追加进 `ctx.system_prompt` 字符串，再由 `cognition_stage` 合并为唯一 system（绝不插第二个 system，否则网关只认末条 → 人格被盖）。
2. **身份锁定优先**：人格 `哈基米` 在 system 末尾以"最高优先级"锁定（见 `stages.py:285`），新片段必须插在它**之前**。
3. **权限分级**：新增能力工具的 `min_level` 默认 `LEVEL_USER`(2)，owner 专属可 `LEVEL_OWNER`(4)。复用 `ReActLoop` 的两级权限闸（`get_schemas` 过滤 + `call` 二次校验）。
4. **人格漂移预算**：若把"使用规律"写进 `self_notes`，必须经 `SelfLearning.check_drift`（`delta_max=0.10`、日预算 `1.0`），否则日预算被耗尽。
5. **语义去重 + 健康维护**（next-gen 血泪教训）：能力库必须有去重合并、未用淘汰、硬上限，否则重演 103/0。

---

## 3. P0 — 工具调用反馈闭环（低风险，先落地）

**缺口**：`react.py` 跑完只返回文本，调用了哪个工具、成功与否不进记忆 → LLM 调用经验不跨会话沉淀。

### 3.1 ReActLoop 暴露调用轨迹（最小改动）
`anima/agent/react.py` 在 `run()` 内新增实例属性 `self.tool_trace: list[dict] = []`，
每轮执行工具后（`react.py:81` 之后）`append`：
```python
self.tool_trace.append({
    "name": name, "args": args, "result": str(result)[:200],
    "ok": not str(result).startswith("工具执行错误"),
    "round": round_idx + 1,
})
```
> 不动循环逻辑，仅采集日志里已有的信息。

### 3.2 新增 `anima/cognitive/tool_learning.py`（ToolLearning 管理器）
职责（对齐 `learning.py` 的 `PersistenceProvider` 原子写约定）：
- `record(permission_role, trace)`：把本次对话的工具调用批量写 `data/tool_learning.json`
  （结构：`{records:[{tool, context, success, ts}], preferences:{tool:{success_count, fail_count, rules:[]}}}`）。
- `summarize_rules(llm, tool)`：同类记录 ≥3 条时，让 LLM 归纳「何时用效果好 / 态度」（复用 next-gen `_summarize_tool_rules` 的 prompt 形态），结果回写 `preferences[tool].rules`。
- `rules_fragment(top_k=4)`：生成注入 system 的「我的工具使用心得」片段（仅列成功规律，失败不污染人格）。

### 3.3 接线
- `generation_stage`（`stages.py:375` 之后）：
  ```python
  from anima.cognitive.tool_learning import get_tool_learning
  if react.tool_trace:
      await get_tool_learning().record(ctx.meta.get("permission_role"), react.tool_trace)
  ```
- `cognition_stage`（`stages.py:296` 跨群注入块之后、身份锁定之前）：追加 `tool_learning.rules_fragment()`。
- 规律总结走**现有 rumination 心跳**（`core/autonomy.py` 的 `Heartbeat` 每 30s）——在反刍分支里抽几条第 3.2 的 `summarize_rules`，自然受漂移预算约束。

### 3.4 配置（`config/default.yaml`）
```yaml
tool_learning:
  enabled: true
  summarize_interval: 10      # 每累计 N 条同工具记录做一次规律归纳
  records_max: 200            # records 环形上限，防无界增长
  inject_top_k: 4             # 注入 system 的使用心得条数
```

---

## 4. P1 — 自生长能力系统（中风险，安全执行）

**目标**：bot 从经历里长出可复用方法论，即时作为 LLM 工具被自己调用；带去重、配额、健康维护。

### 4.1 移植 `capability_dedup.py`
直接复制 `G:/Anima/anima/capability_dedup.py` → `anima/cognitive/capability_dedup.py`（零依赖，可独立测试）。
保留：`_SYNONYMS` / `_CORE_SLOTS` / `normalize_capability_signature` / `find_similar_capability` / `text_similarity`。

### 4.2 新增 `anima/cognitive/capabilities.py`（生产版 CapabilityManager）
去 AstrBot 化，复用生产基建（`PersistenceProvider`、单例 `get_capability_manager()`、`get_learning()`）：

- **数据模型** `data/personal_capabilities.json`：
  ```json
  {"version":1,"capabilities":[
    {"id":"cap_...","name":"...","description":"...","how_to_use":"...",
     "parameters_schema":{...},"confidence":0.3,"usage_count":0,
     "corrections":[],"created_at":"...","last_updated":"...",
     "register_as_independent_tool":false}
  ],"last_research_ts":""}
  ```
- **`create_or_update(cap)`**：复用 next-gen 两道去重（名字精确 + `find_similar_capability` 语义合并），
  合并时累计 `usage_count` + 合并 `corrections`（见 `capabilities.py:130-202`）。
- **`apply_feedback(name, success, reflection)`**：成功 `confidence=min(0.98, +0.08)`，
  失败 `confidence=max(0.1, -0.15)` + 记 `corrections`（见 `capabilities.py:773-806`）。
- **`value_score(cap)`** = `usage*2 + len(corrections)*0.5 + recency(90d 线性)`（见 `capabilities.py:388`）。
- **`maintain_health()`**：未用>30d 淘汰、>14d 降权、去重合并、硬上限 `capability_max_total=40`（见 `capabilities.py:667`）。
- **`build_fragment()`**：按 `value_score` 取 Top-6 生成第一人称「我的个人技能与方法论」片段（见 `capabilities.py:204`）。

### 4.3 能力执行器（安全核心，无代码 eval）
新增 `execute_capability(llm, cap, query)`：用 `cap["how_to_use"]` + `cap["description"]` 组装 exec prompt，
调 `llm.chat()`（或 `chat_structured`）拿结果，**不执行任何外部代码**。
```python
exec_prompt = (
  f"你正在作为自己创造的能力「{cap['name']}」执行任务。\n"
  f"能力描述：{cap['description']}\n使用方法：{cap['how_to_use']}\n"
  f"任务输入：{query}\n直接给出高质量结构化结果。"
)
```
> 与 next-gen `_execute_single_capability`（`capabilities.py:349`）同构，但去掉 AstrBot `ContextWrapper`/`FunctionTool`。

### 4.4 动态注册进 ToolRegistry（即时对 LLM 可见）
新增 `register_capability_as_tool(cap)`，在 `anima/tools/__init__.py` 加一个工厂：
```python
def make_capability_tool(cap, executor):
    async def _handler(query_or_args: str = "") -> str:
        from anima.core.llm import get_llm
        return await executor(get_llm(), cap, query_or_args)
    return Tool(
        name=_safe_tool_name(cap["name"]),          # my_xxx，复用 next-gen归一化
        description=cap["description"][:200],
        parameters=cap.get("parameters_schema") or {"query_or_args": {"type":"string","description":"任务描述"}},
        handler=_handler,
        min_level=cap.get("min_level", LEVEL_USER),
    )
```
- 仅在 `confidence >= 0.65` 且 `register_as_independent_tool=True` 时注册（next-gen 双闸）。
- **每日配额** `dynamic_tool_daily_quota=3`（next-gen `capabilities.py:260`），超配额能力照常入库但不注册为独立工具。
- 同名跳过（`ToolRegistry.tools` 已有 name 索引），不重复注册。

### 4.5 接线与触发
- **识别缺口**：在 `rumination`（反刍）分支里，当 `recent_context` 出现"反复出现、可用方法论解决、但无现有工具"的信号时，
  用 `llm.chat_structured(temp=0)` 产出能力规约（name/description/how_to_use/parameters_schema），调 `create_or_update`。
  > 复用 G1 `crossgroup/extraction.py` 的 `record_user_facts` 范式（一次结构化抽取）。
- **注入**：`cognition_stage` 在跨群块之后、身份锁定之前追加 `build_fragment()`（严守单 system 契约）。
- **反馈闭环**：`generation_stage` 记录工具调用时，若 `tool_name` 匹配某能力 `name`，调 `apply_feedback`。
- **健康维护**：挂到现有 `Heartbeat`（每 6h 跑一次，与 `forgetting.py` 同频）。

### 4.6 配置（`config/default.yaml`）
```yaml
capabilities:
  enabled: true
  initial_confidence: 0.3
  auto_register_confidence: 0.65
  dynamic_daily_quota: 3
  max_total: 40
  unused_drop_days: 30
  unused_decay_days: 14
  dedup_text_threshold: 0.6
  min_create_text_len: 6        # 防止碎碎念生成无意义能力
```

---

## 5. P2 — 自动生成 handler_code（高风险，本期不实现）

仅记录方向：若未来要让能力执行**真实代码**，必须
- 走 `anima/tools/sandbox.py` 的 `run_python` 同款沙箱（`__builtins__:{}` + 禁网络/文件系统，见 `tools/__init__.py:139` 的 `_tool_calc` 范式）；
- `min_level=LEVEL_OWNER`(4) 且 owner 显式审批后才注册；
- 仍过语义去重 + 每日配额。
> 当前 P1 的 LLM 提示词执行已覆盖绝大多数"方法论型"能力，P2 非必需。

---

## 6. 与现有架构的契约检查

| 契约 | 是否满足 | 措施 |
|---|---|---|
| 单一 system 消息 | ✅ | 所有新片段追加进 `system_prompt`，由 `cognition_stage` 合并；插在身份锁定**之前** |
| 人格 `哈基米` 不被覆盖 | ✅ | 身份锁定段位置不变，新片段在其前 |
| 权限分级 | ✅ | 复用 `ToolRegistry` 两级闸；新工具默认 `LEVEL_USER` |
| 漂移预算 | ✅ | P0 规律写 `self_notes` 走 `check_drift`；P1 用独立 `personal_capabilities.json`，不挤占人格预算 |
| 数据安全 | ✅ | 能力执行不 eval 代码；`data/` 下独立 JSON，与 `chat_memory`/`crossgroup` 互补零迁移 |
| 后台不拖慢回复 | ✅ | 抽取/总结/健康维护全在 `Heartbeat`/反刍 异步分支，同 G1 的 `asyncio.create_task` 范式 |

---

## 7. 测试策略（对齐 G1 的 17 用例模式）

`tests/test_tool_self_growth.py`：
- P0：`tool_trace` 采集正确；`record` 写盘；`rules_fragment` 只含成功规律；`summarize_rules` 收敛 JSON。
- P1：`find_similar_capability` 合并「戉系/Ego系」家族（去重核心）；`create_or_update` 累计 usage；
  `apply_feedback` 置信度升降；`maintain_health` 淘汰未用>30d、硬上限截断；
  `make_capability_tool` 注册后 `get_schemas` 可见、`call` 走 executor 返回结构化结果；同名额跳过。
- 集成：conftest 用隔离 fixture（独立 `data/` 临时目录）。

---

## 8. 落地顺序与验收

1. **P0**：实现 3.1–3.4 → 本地 67+ 测试仍全绿 → VPS 灰度（仅 `tool_learning.enabled=true`），观察 `tool_learning.json` 增长、`self_notes` 是否出现工具规律。
2. **P1**：先移植 `capability_dedup.py`（纯函数，先测）→ 再 `capabilities.py` + 动态注册 → 接线 rumination/注入/反馈 → 测试 → VPS 灰度（`capabilities.enabled=true`，owner 群先试）。
3. **验收红线**：① 人格 `哈基米` 注入未被破坏（system 合并仍 [0]）；② 能力库总量 ≤ `max_total` 且 0 使用占比受 `unused_drop_days` 约束；③ 不出现第二个 system 消息；④ 任意新工具执行不触发代码执行。

---

## 9. 关键风险与护栏（来自 next-gen 生产教训）

- **去重是命门**：无语义去重 → 能力库爆炸（实测 103 能力 / 0 使用）。`capability_dedup.py` 的
  `_CORE_SLOTS` 共享≥2 即合并 + 文本 2-gram 兜底，是已验证解，必须原样移植。
- **自信任度脱钩**：新建能力一律从 `initial_confidence=0.3` 起步，**忽略 LLM 自评高分**（next-gen v0.9.4 修正），
  只有真实使用反馈（`apply_feedback`）能提上去。
- **每日配额 + 硬上限**：防止一次反刍风暴灌满工具表。
- **身份不漂移**：能力片段是"我的工具"，第一人称叙事化，但不写入人格核心，独立于 `self_notes` 漂移预算。
