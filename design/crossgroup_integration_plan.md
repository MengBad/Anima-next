# G1 集成方案：crossgroup_bot → Anima-next Pipeline（v2，事实采集=每条消息 LLM 抽取）

> 状态：方案待确认（v2，事实采集策略已改为"每条消息 LLM 结构化抽取"）
> 范围：跨群用户记忆共享 + 群氛围差异化语调（两项都做）
> 事实采集：**每条用户消息都过一次 LLM 结构化抽取**（用户选定）

---

## 1. 背景与现状

### Anima-next 现有 5 阶段 Pipeline
`permission → memory → cognition → generation → guardrail`
- `memory_stage`：短期对话记忆 + 长期向量记忆
- `cognition_stage`：拼装 system_prompt，**合并成唯一 system 消息**（`[0]`）
- `generation_stage`：ReAct 循环经 `core.llm` 生成
- `handler.handle`：先查 owner 命令 → AstrBot 分发 → **回复闸门**（`reply_only_when_mentioned` 且未 @bot 则 `return`，不进 Pipeline）→ 构建 ctx → 跑 Pipeline

### ⚠️ 关键架构约束（决定集成形态）
**没 @bot 的群消息在回复闸门处就被 `return`，根本不进 Pipeline。** 若只在 Pipeline 内做观察/抽取，群聊里绝大多数"用户在聊但没 @bot"的内容学不到 → 跨群记忆与群氛围画像严重失真。
→ **必须在回复闸门之前、对所有入站消息做观察与抽取**，且抽取不能拖慢回复。

### crossgroup_bot（G:/Anima，未部署）提供
1. **跨群用户记忆** `UserMemory/UserFact`：同用户所有群事实互通
2. **群氛围画像** `AtmosphereAnalyzer`：每群学出风格标签 + 置信度

### LLM 结构化抽取能力已具备
`LLMClient.chat_structured(messages, tools, temperature, max_tokens)` 走 OpenAI 工具调用（`tool_choice=auto`），返回 `{content, tool_calls}`，`tool_calls[].function.{name, arguments}`（arguments 为 JSON 字符串）。ReAct 已用同款接口 → 抽取直接复用，无需新依赖。

---

## 2. 设计决策（v2 已确认）

| 决策点 | 结论 |
|---|---|
| 复用方式 | 移植为 `anima/crossgroup/`，丢弃 `llm/provider.py`（复用 Anima-next 的 `core.llm`）|
| 集成范围 | 跨群记忆 + 群氛围，**两项都做** |
| 事实采集 | **每条用户消息 LLM 结构化抽取**（用户选）|
| 观察执行点 | **回复闸门之前、对所有入站消息执行**（修复"只学 @bot 消息"的失真）|
| 抽取性能 | 事实抽取用 `asyncio.create_task` **后台异步**，绝不阻塞/拖慢回复；失败静默跳过 |
| 氛围观察 | 本地启发式（`AtmosphereAnalyzer`，零 LLM 成本、零延迟）|
| 存储 | 独立 JSON `data/crossgroup/{users,atmospheres}.json`，与 `chat_memory`/`vector_memory` 互补并存 |
| system 消息契约 | 跨群/氛围块追加进 `ctx.system_prompt` 字符串，绝不单独插第二个 system（守住上次修复）|
| 单例 | `get_crossgroup_manager()` 仿 `get_chat_memory()`，`stop()` flush |

---

## 3. 目标文件改动

### 新增 `anima/crossgroup/`（从 G:/Anima/crossgroup_bot 移植，去 LLM 依赖）
- `__init__.py`：导出 `CrossGroupManager`(=原 `MemoryManager`)、`AtmosphereAnalyzer`、`build_crossgroup_fragment`
- `atmosphere/analyzer.py`、`atmosphere/profiles.py`：原样
- `memory/manager.py`(改名 `CrossGroupManager`)、`memory/user_memory.py`、`memory/session_buffer.py`：原样，`data_dir` 可配
- `generation/prompt_builder.py`：保留 `build_system_prompt`；新增 `build_crossgroup_fragment(persona_stub, atmosphere, user_mem) -> str`（只返回追加文本）

### 新增 `anima/crossgroup/extraction.py`
- 定义工具 schema `record_user_facts`，属性 `facts: [{content:str, tags:[str], confidence:float}]`
- `async def extract_user_facts(llm, text, user_id, name) -> list[dict]`：
  - 仅当 `len(text.strip()) >= 6` 才调 LLM（跳过 "666"、emoji、单字）
  - `chat_structured([{system:抽取指令}, {user:text}], tools=[SCHEMA], temperature=0.0, max_tokens=256)`
  - 解析 `tool_calls[0].function.arguments` → JSON → 列表；异常/空 → `[]`
  - 指令要求：只抽**关于该用户的、 durable 个人事实**（兴趣/身份/在学/偏好/长期项目），不抽闲聊/秘密/瞬时情绪

### 新增 `anima/crossgroup/manager_singleton.py`
- `get_crossgroup_manager()` 懒加载单例，`data_dir` 默认 `data/crossgroup/`（env `ANIMA_CROSSGROUP_DIR` 可覆盖）
- `close()/flush()`：JSON 已增量落盘，做最终 flush

### 改 `anima/llm/handler.py`（核心前移）
- 在 `_handle_command` / AstrBot 分发 / **回复闸门之前**，新增 `await self._observe_crossgroup(event, text, user_id, group_id, detail_type, sender_name)`：
  - **氛围观察（同步、零成本）**：`cgm.observe_group(str(group_id), [该群近期样本])`（仅群消息；样本来自 `chat_memory.get_context` 或内部滚动缓冲）
  - **事实抽取（后台）**：`asyncio.create_task(self._extract_and_store(user_id, name, group_id, text))`，其中调 `extract_user_facts(core.llm, ...)` → 逐条 `cgm.add_user_fact(...)`
  - 私聊：`observe_group` 跳过；事实抽取照常（source_group = 私聊标记）
- `_handle_command` 保留并可加 `!anima remember <事实>` 用于 owner 手动补种/纠偏

### 改 `anima/pipeline/stages.py`
- `memory_stage`：群消息时用已拉取的 `history` 补 `observe_group`（与 handler 前置观察互补；handler 负责实时，这里补历史兜底）
- `cognition_stage`：合并为单一 system 前，追加
  - 氛围块：`cgm.get_atmosphere(str(group_id))`；私聊 → NEUTRAL
  - 跨群记忆块：`cgm.get_user(user_id, name).recall()` 摘要
  - 用 `build_crossgroup_fragment` 生成

### 改 `anima/core/anima_core.py`
- `stop()`：调用 `get_crossgroup_manager().close()`

### 改 `anima/core/config.py` + `config.yaml`
- 新增 `crossgroup:` 段：`enabled: true`、`observe_all_messages: true`（对所有入站消息观察）、`min_fact_text_len: 6`、`extraction_model: ""`（空=复用主模型）

---

## 4. 分阶段执行

| 阶段 | 任务 | 验收 |
|---|---|---|
| P0 | 移植 `anima/crossgroup/`（去 LLM 依赖）+ `get_crossgroup_manager()` 单例 | `py_compile` 通过；单例同实例 |
| P1 | `config.yaml`/`AnimaConfig` 加 `crossgroup:` 段 | 默认开启可读 |
| P2 | `extraction.py`：工具 schema + `extract_user_facts`（`chat_structured`，temp=0，max_tokens=256，短文本跳过）| 单测：给定"我在学 Rust"→ 返回 facts 含 Rust；"666"→ `[]` |
| P3 | `handler._observe_crossgroup` 前置到回复闸门之前；氛围同步观察 + 事实 `create_task` 后台抽取；`!anima remember` 命令 | 群消息未 @bot 也能观察到氛围/抽取事实（不拖慢回复）|
| P4 | `memory_stage` 补观察 + `cognition_stage` 注入两块（守单一 system）| system_prompt 含 `[当前群氛围]` 与 `[用户跨群记忆]` |
| P5 | 测试：`test_crossgroup_atmosphere_diff`、`test_crossgroup_fact_recall`（跨群）、`test_single_system_message`（回归）、cli_demo 等价冒烟、抽取失败静默跳过单测 | 全绿 |
| P6 | 部署 VPS：推新包 + 改后 stages/handler/core；建 `data/crossgroup/`（anima 属主）；重启；验证 | 群内实测：未 @bot 发言后氛围画像更新、另一群召回跨群事实；journalctl 无导入错 |

---

## 5. 风险与对策
- **单一 system 消息契约**：所有跨群/氛围内容进 `ctx.system_prompt` 字符串；`test_single_system_message` 专门回归守护
- **成本/延迟（用户已知并选定每条消息 LLM 抽取）**：
  - 事实抽取 `create_task` 后台跑，**不计入回复延迟**（用户感知的回复速度不受影响）
  - `len(text) >= 6` 阈值过滤无意义短消息，减少无效调用
  - `temperature=0` + `max_tokens=256`，单次抽取 token 开销极小
  - `observe_all_messages` 可关（退化成"仅 @bot 消息才学"），作成本阀
- **抽取失败/超时**：`extract_user_facts` 内部 try/except，异常返回 `[]`，**绝不中断 Pipeline 或回复**
- **氛围误判**：`merge` 指数滑动+置信阈值防抖；低置信→NEUTRAL，不污染人格
- **磁盘**：新增 JSON 为 KB 级，VPS 根分区 2.8G 无压力；长期仍须扩容

---

## 6. 不在本期范围（后续）
- 抽取限频/批量合并（如每用户每 N 条或每 N 秒合并一次抽取，进一步降成本）
- `!anima forget` 误抓修正命令
- 跨群事实语义去重/冲突消解
- 抽取用独立轻量模型（如 gemini-flash 之外的更便宜端点）
