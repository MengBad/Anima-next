> [HISTORICAL] 本文档停更于 2026-07-29，仅作历史参考；现状以 README.md / MAINTENANCE.md / schemas/config.schema.yaml 为准。

# 【设计文档】ReAct 记忆优先策略

> 目标：解决 ReAct 一上来就 `web_search` 的问题，让内部记忆（跨群成员事实 / 历史对话 / web 缓存）
> 优先于联网搜索，减少无效搜网与隐私泄露（把"亦萱喜欢什么"这类已知答案也丢去搜网=泄露 bot 有内部记忆 + 浪费延迟）。
> 设计原则：**复用既有注入通道**（`generation._inject_runtime_tool_guidance` 已存在，且幂等、身份锚定安全），
> **最小侵入**——只加一个 priority 字段 + 一个引导常量，不重写 ReAct。
> 注：当前 `_REALTIME_TOOL_GUIDANCE`（generation.py:48）**恰恰鼓励先搜网**，本策略是它的"反向补丁"——
> 对"成员/事实类"问题注入记忆优先引导，对"实时类"问题保留原实时引导，二者按触发词分流，不冲突。

---

## 0. 现状盘点（必须先看，避免凭空设计）

| 资产 | 位置 | 现状 | 本策略如何复用 |
|---|---|---|---|
| ReAct 循环 | `agent/react.py:74` `run(messages,…)` | **无自有 system prompt**，直接用 `messages`（已含 cognition 注入的 system） | 记忆优先引导在 ReAct 启动前注入 system（复用 generation 的注入器） |
| schema 过滤 | `tools/__init__.py:123` `get_schemas()` | 返回 `self.tools.values()` **dict 插入顺序**，**无优先级排序** | 新增 `priority` 字段 + 排序 |
| 工具注册顺序 | `tools/builtin/__init__.py` | `web_search`(line 47) 注册在 `get_user_relations`(171)/`get_member_facts`(182) **之前** → 今天 schema 里 web 确实排在记忆前 | 注册顺序不再是决定因素，靠 priority 重排 |
| 工具引导注入器 | `generation.py:63` `_inject_runtime_tool_guidance(ctx, guidance)` | 幂等、插在"身份锁定"锚点**之前**、不破坏人格护栏 | 直接复用此函数注入记忆优先引导 |
| 实时引导常量 | `generation.py:48` `_REALTIME_TOOL_GUIDANCE` | 触发词 `_REALTIME_TOOL_RE`(20) 命中→注入"优先 web_search" | 保留给实时类；新增记忆优先引导常量分流 |
| 事实入库 | `crossgroup/memory/manager.py:87` `add_user_fact(user_id, content, source_group, …)` | 已带内容归一化去重 + FIFO 截断（`max_facts_per_user`） | web 事实复用此接口，`source_group="web"` |
| 记忆召回 | `crossgroup/memory/user_memory.py:44` `recall(k)` / `recall_public(k)` | 按时间戳倒序取 k 条；cognition 已把被提及成员事实注入 system | "记忆优先"本质=让 ReAct 先用这些已注入/可查的记忆 |

---

## 1. Prompt 层变更（方案 A + B）

### 1.1 工具优先级提示（A）——新增常量 + 触发分流
在 `generation.py` 顶部常量区新增（与 `_REALTIME_TOOL_GUIDANCE` 并列）：

```python
# 记忆优先引导：成员/事实类问题，先查内部记忆再考虑搜网
_MEMORY_FIRST_RE = re.compile(
    r"(是谁|喜欢|讨厌|怎么样|怎样的人|讲讲|介绍|认识|是什么关系|干啥|做啥|"
    r"什么性格|爱好|擅长|背景|以前|去年|上次|记得|提过|说过)",
    re.I,
)
_MEMORY_FIRST_GUIDANCE = (
    "这个问题大概率关于群里某个已知成员或已有事实。请遵循工具优先级：\n"
    "1) 关于群成员（『亦萱是谁』『小明喜欢什么』）→ 优先用 get_member_facts / get_user_relations；\n"
    "2) 关于已知事实或历史对话 → 优先用已注入的记忆（cognition 已把被提及者的公开事实放进 system）；\n"
    "3) 只有内部记忆确实无法回答时，才调用 web_search；\n"
    "4) 仅当问题明确涉及实时信息（今天天气、正在发生的新闻、股价、开播状态）才可直接 web_search。\n"
    "在 Thought 中先陈述『我是否已从记忆/已注入内容知道答案』，再决定要不要搜网——不要一上来就搜。"
)
```

在 `generation.py` 的 ReAct 启动区（紧挨 line 416 的实时引导）追加：
```python
# 记忆优先引导（成员/事实类问题，与实时引导分流）
if available_tools and _MEMORY_FIRST_RE.search(ctx.text or ""):
    _inject_runtime_tool_guidance(ctx, _MEMORY_FIRST_GUIDANCE)
```
> 分流逻辑：实时词命中→注入"优先搜网"；成员/事实词命中→注入"优先记忆"。两者 regex 不重叠，偶发同时命中时两个 block 都注入（不矛盾，模型按语义取舍）。

### 1.2 ReAct Thought 引导（B）——并入同一引导常量
方案 B 的"Thought 先陈述是否知道答案"已写进 `_MEMORY_FIRST_GUIDANCE` 第 4 行（"在 Thought 中先陈述…"）。
**不另改 `react.py` 的循环结构**——Thought 是模型自由生成字段，强制格式会增加 token 与脆弱性；用 system 引导"先想再搜"已足够，实测比硬编码 Thought 模板更稳。
（若后续 A/B 测试显示引导不够，再考虑在 `react.py` 首轮 `chat_structured` 加一条 one-shot Thought 示例，本期不做。）

---

## 2. 代码层变更（方案 C + D）

### 2.1 schema 排序（C）：新增 priority 字段
`tools/__init__.py` 的 `Tool.__init__` 增加参数：
```python
def __init__(self, name, description, parameters, handler,
             min_level=LEVEL_USER, required=None, danger=False,
             enabled=True, priority: int = 5):   # 越小越靠前；默认 5
    ...
    self.priority = priority
```
`get_schemas()` 改为按 priority 升序：
```python
def get_schemas(self, permission_level=LEVEL_USER) -> list[dict]:
    return [
        t.to_openai_schema()
        for t in sorted(self.tools.values(), key=lambda x: x.priority)
        if t.enabled and permission_level >= t.min_level
    ]
```
`builtin/__init__.py` 注册时给记忆类工具显式低 priority：
```python
registry.register(Tool("get_member_facts", ..., make_get_member_facts(core),
                       required=["name"], min_level=LEVEL_USER, priority=1))
registry.register(Tool("get_user_relations", ..., make_get_user_relations(core),
                       required=["name"], min_level=LEVEL_USER, priority=1))
registry.register(Tool("web_search", ..., tool_web_search,
                       required=["query"], min_level=LEVEL_USER, priority=3))
```
> 效果：schema 中记忆工具（prio 1）排在 web_search（prio 3）前 → LLM 生成首个 tool_call 更可能先选记忆。
> 其它工具保持默认 5，不受影响。**向后兼容**：旧调用方不传 priority 时默认 5，行为不变。

### 2.2 搜网结果自动入库（D）：记忆沉淀
**设计决策（重要，偏离原方案表述）**：原方案"写 `users.json` 标记 source_group='web'"——
但 `users.json` 以 QQ 用户为主键，web 事实分两类：
- **关于已知成员**（如搜"亦萱 喜欢的游戏"）→ 写入该成员 `user_id` 的 facts，`source_group="web"`（复用 `add_user_fact`，已有去重+FIFO）。
- **关于通用实体/话题**（如"2026 奥运举办地""某 up 主开播时间"）→ 无 QQ 身份，**不应污染 members.json**。新建轻量 `data/web_facts.json`（实体→fact 列表，结构同 UserFact），配 `WebKnowledgeStore`，提供 `recall_web_fact(entity) -> list`。

落到代码：在 `agent/react.py` 的 `run()` 工具执行分支（line 145 循环内）对 `name == "web_search"` 且 `ok` 的结果，触发**后台异步**沉淀：
```python
if name == "web_search" and ok:
    asyncio.create_task(
        self._distill_and_store_web(result, ctx_meta, llm=self.llm)
    )
```
`_distill_and_store_web`：
1. 从 `ctx_meta` 取本轮被提及成员 `mentioned_uids` / 问题实体（若 ReAct 由生成阶段发起，meta 已带 `mentioned_uids`）。
2. 轻量 LLM 抽取（`extract_user_facts` 同款模式：关键词预筛→`record_web_facts` 工具，返回 `{entity, content, is_about_member}`）。
3. `is_about_member` → `crossgroup_manager.add_user_fact(uid, content, source_group="web")`；否则 `web_knowledge.add(entity, content, source_group="web")`。
4. **任何异常静默丢弃**，不阻塞 ReAct、不拖慢回复（与现有抽取任务同纪律）。

> 为何不全写 users.json：避免把"2026 奥运举办地"这类话题塞进某个 QQ 用户的 facts，污染成员画像与隐私过滤逻辑（recall_public 会误把这些当成员事实对外）。分开存储更干净，且 web_knowledge 可被"同样问题"检索命中（见 §3）。

---

## 3. 预期效果

### 3.1 无效搜网减少比例（估算，待 A/B 实测校准）
- 触发面：成员/事实类问题占 ReAct 触发量的可观比例（亦萱案即典型：50 条 facts 却在搜网）。
- 机制叠加三重保险：① prompt 引导（先想再搜）② schema 排序（记忆工具在前）③ 记忆已注入 system（cognition 已给答案，模型无需搜）。
- **保守预估**：成员/事实类问题的无效搜网下降 **60–80%**；整体 ReAct 搜网调用下降 **30–50%**（含话题缓存命中）。精确值由 §5 A/B 测试给出，不在设计阶段断言。

### 3.2 隐私泄露风险降低评估
- **正向**：成员类问题不再把"bot 已知内部记忆"通过搜网外发（原行为=把已知答案当未知去搜，既浪费又暴露 bot 有内部数据）；且 web 沉淀对成员事实加 `source_group="web"` 可被 `recall_public` 过滤（不混入私聊私密）。
- **负向（须关注）**：D 的 web 沉淀若把关不严，可能把搜索结果里的 PII 写库。缓解：抽取层只存"事实陈述"，不过存原文；`web_knowledge` 与 `users.json` 分离，成员事实走 `add_user_fact` 的既有去重/公开过滤。
- 净评估：隐私风险**下降**，但需 §4 回滚 + §5 监控兜底。

---

## 4. 回滚策略（记忆优先导致回答过时）

核心风险：记忆是去年的（"亦萱现在喜欢什么"但 facts 是旧款）→ 记忆优先可能给出过时答案。

1. **新鲜度标记**：`web` 来源 fact 写入时打 `timestamp`；`recall` / `recall_public` 已在用 timestamp 倒序。对 `source_group="web"` 的 fact，额外在召回时附"（网络信息，可能过时）"提示由模型判断是否够新。
2. **实时词豁免**：`_REALTIME_TOOL_RE`（今天/现在/最新/新闻…）命中时**仍注入实时引导**，模型会优先搜网拿最新 → 过时风险自动降级。即"记忆优先"只在问题无实时词时生效。
3. **置信衰减复用**：进化1 的 `UserFact.confidence` 每日 ×0.99（若已落地）天然让旧 web fact 置信下降，召回权重降低。
4. **一键回滚**：config `react.memory_first: true` 总开关。关闭 → 不注入记忆引导、priority 字段不影响（或回退默认排序）→ 完全退化为现有行为。
5. **误伤回滚**：若发现某类问题因记忆优先答错，可把该触发词移出 `_MEMORY_FIRST_RE`（如"最新"本就不在记忆词表，已安全）。
6. **全有或全无**：本策略纯增量（新增常量 + 字段 + 可选后台任务），不删改既有逻辑；任一处异常均被现有 try/except 兜住，不影响主回复。

---

## 5. A/B 测试策略（对比新旧回答质量）

**方案**：config 加 `react.memory_first_ab: {enabled, variant_ratio}`（如 50% 流量走新策略）。
- **分组**：按 `hash(round(user_id/群id)) % 100 < variant_ratio` 决定该轮是否注入记忆引导 + 是否启用 priority 排序（schema 排序可 per-request 控制：新组用排序后 schemas，对照组用原插入序）。
- **指标（埋点）**：
  - 效率：`web_search` 调用次数/ReAct 触发数、平均 ReAct 轮次、首字延迟。
  - 质量：① 自动评判——对 200 条"成员/事实类"标注问题，对比两组答案是否含记忆事实、是否捏造；② 人工抽检 50 条；③ owner 在测试群主观打分。
  - 隐私：搜网请求中是否仍含"本应内部可知"的成员问题（下降=成功）。
  - 过时：抽样"实时类"问题，确认未因记忆优先给出旧答案（实时监控误伤率）。
- **判定**：新组在"质量不降"前提下，搜网调用降 ≥30% 即全量；若质量下降或误伤率 >2%，回滚（§4 开关）。
- **复用现有埋点**：`tool_trace`（react.py:164）已记录每次工具名/成败/轮次 → 直接出"搜网占比"指标，无需新埋点框架。

---

## 6. 文件改动清单（实现阶段用，本设计文档不含代码）

| 文件 | 改动 |
|---|---|
| `pipeline/stages/generation.py` | 新增 `_MEMORY_FIRST_RE` / `_MEMORY_FIRST_GUIDANCE` 常量；ReAct 启动区追加记忆优先引导注入（line 416 后） |
| `tools/__init__.py` | `Tool.__init__` 加 `priority` 参数；`get_schemas()` 按 priority 升序 |
| `tools/builtin/__init__.py` | `get_member_facts`/`get_user_relations` 注册 `priority=1`；`web_search` `priority=3` |
| `agent/react.py` | `run()` 工具循环内对 `web_search` 成功结果 `asyncio.create_task(_distill_and_store_web(...))`；新增 `_distill_and_store_web` 方法 |
| `crossgroup/memory/web_knowledge.py` | **新增**（可选）：`WebKnowledgeStore` + `recall_web_fact(entity)`，存 `data/web_facts.json` |
| `core/config.py` | 新增 `ReactMemoryFirstConfig`（memory_first 开关 + ab 分流字段） |
| `tests/test_react_memory_first.py` | 见下 |

**测试策略**：
- `test_memory_guidance_injected_for_member_question`：构造"亦萱是谁" → 断言 system 含 `_MEMORY_FIRST_GUIDANCE`。
- `test_realtime_guidance_still_wins_for_news`：构造"今天深圳天气" → 断言注入实时引导而非记忆引导（或两者不冲突）。
- `test_schema_order_memory_before_web`：`get_schemas()` 返回中 `get_member_facts` index < `web_search` index。
- `test_priority_default_backward_compat`：不传 priority 的工具默认 5，排序不变。
- `test_web_result_distilled_to_member`：mock web_search 返回含成员事实 → 断言 `add_user_fact` 被调用且 `source_group="web"`。
- `test_web_result_distilled_to_web_knowledge`：通用话题 → 断言 `web_knowledge.add` 被调用。
- `test_distill_failure_silent`：抽取异常 → 断言 ReAct 主流程不受影响（不抛、回复正常）。
- `test_memory_first_toggle_off`：`memory_first=False` → 不注入引导、priority 不改变排序。

---
*设计基线：Anima-next 生产路径 `G:/Anima-next`，部署于 VPS `/opt/anima`。*

---

## 7. 实现状态（2026-09-15 更新：已实现）

状态：**已实现并合入主路径**，回归测试见 `tests/test_react_memory_first.py`（14 项）。
总开关 `config.react.memory_first`（默认 `true`，置 `false` 一键回滚，见 §4.4）。

### 实现偏差（相对本设计，均经代码现状校准）

| # | 设计原文 | 实际实现 | 原因 |
|---|---|---|---|
| 1 | §1.1 两个引导「偶发同时命中时都注入」 | 改为**互斥分流**：`_REALTIME_TOOL_RE` 命中时只注入实时引导，未命中才考虑记忆引导 | `_REALTIME_TOOL_RE` 刻意宽松（含「是什么/为什么/资料/百科」），实测会与记忆词大面积重叠；同发两条互相矛盾的指令比不注入更糟。语义与 §4.2「记忆优先只在问题无实时词时生效」一致 |
| 2 | §2.1 `get_schemas()` 无条件按 priority 升序 | 新增 `sort_by_priority` 关键字，**默认 False**，仅 ReAct 主路径显式传 True | legacy schema 的返回顺序属于 M0.3（RFC-0001 §5.4）「逐字节不变」冻结契约，由 `test_capability_*` 系列守护；无条件排序会让 4 个冻结契约测试变红 |
| 3 | §2.2 在 `react.py` 里 `asyncio.create_task` 沉淀 web 结果 | **不新增**：`tools/builtin/web.py:329-340` 已在 `tool_web_search` 内同步完成（try/except 静默） | 已有实现更优——无悬挂 task、无并发写盘竞争。不重复造轮子 |
| 4 | §6 新增 `crossgroup/memory/web_knowledge.py` | 未新建，复用既有 `anima/memory/web_knowledge.py` | 计划明确要求「勿再建副本」 |

### 未实现（本期刻意不做）

- §5 A/B 分流埋点（`memory_first_ab`）：先用总开关观察，确认方向正确再投入埋点框架。
- §3.1 的降幅数字仍是估算，未经 A/B 实测校准。
