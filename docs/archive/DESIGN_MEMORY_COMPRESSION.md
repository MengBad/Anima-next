> [HISTORICAL] 本文档停更于 2026-07-29，仅作历史参考；现状以 README.md / MAINTENANCE.md / schemas/config.schema.yaml 为准。

# 【设计文档】记忆压缩与遗忘（进化1）

> 目标：让 bot 从"堆事实的字典"进化成"有信誉、会整理、记得重点"的数字生命。
> 核心三动作：**置信度衰减**（confidence decay）、**相似聚类压缩**（merge）、**过期淘汰**（cap + 遗忘）。
> 设计原则：**绝不重复造轮子**——复用现有 `ForgettingEngine`（时间衰减）与 `ContradictionDetector`（向量矛盾），不另起炉灶。

---

## 0. 现状盘点（必须先看）

| 资产 | 位置 | 现状 | 进化1 如何复用 |
|---|---|---|---|
| `UserFact` dataclass | `crossgroup/memory/user_memory.py:13` | 字段：`content / source_group / timestamp / tags`，**无 confidence/disputed** | 新增两字段 |
| 抽取已产 confidence | `crossgroup/extraction.py:62` 工具 schema 有 `confidence`；`_normalize_facts:226` 已解析但**下游丢弃** | `add_fact` 不接收 confidence | 让 confidence 真正落库 |
| `ForgettingEngine` | `cognitive/forgetting.py:23` | 已实现 `retention_score = salience×recency×access×emotion`，阈值 0.15，6h gate | 复用其衰减/调度骨架，扩展作用于 UserFact |
| `ContradictionDetector` | `cognitive/contradiction.py:36` | 向量检索命中旧记忆 → LLM 判冲突 → 写 `contradictions.json` | 复用其向量接口，把命中事实标 `disputed=True` |
| 向量记忆 | `memory/vector_memory.py` | `search(text, top_k, threshold, source_uin, source_gid, scoped)` | 复用算 fact 间相似度做聚类 |
| 夜间触发器 | `core/autonomy.py:992` | 心跳 tick → `forgetting.run(chat_memory, vector_memory)` | 在同一 tick 后挂 `compression.run()`，同窗口、非阻塞 |

**关键洞察**：抽取层早已能产出置信度（默认 0.5），但 `UserMemory.add_fact()` 没接这个参数，导致 confidence 信息在落地前就丢了。这是最便宜、最高价值的修复点。

---

## 1. 数据模型变更：UserFact 字段新增

```python
@dataclass
class UserFact:
    content: str
    source_group: str                       # 已有：来源群（跨群追溯）
    timestamp: float = field(default_factory=time.time)
    tags: list[str] = field(default_factory=list)
    # ↓↓↓ 进化1 新增 ↓↓↓
    confidence: float = 1.0                # 抽取置信度 0-1；旧数据缺省=1.0（视为可信）
    disputed: bool = False                 # 被 ContradictionDetector 判冲突 → True
    compressed: bool = False               # 由压缩聚类合并而来（非原始抽取）
    source_groups: list[str] = field(default_factory=list)  # 压缩后保留的来源群集合（去重）
    merge_count: int = 1                   # 该 fact 由几条原始 fact 合并而来
```

**配套改动（最小、向后兼容）：**
- `UserMemory.add_fact(self, content, source_group, tags=None, confidence=1.0)` → 透传 confidence；落库时旧 `users.json` 缺字段由 `field(default=...)` 自动补齐，**无需迁移脚本**。
- `extraction.py` 调用方在把抽取结果写库时，把 `fact["confidence"]` 传给 `add_fact`（缺省 0.5 → 与现有 `_normalize_facts` 默认一致）。
- `recall()` / `recall_public()` / `summary()` 维持原签名；`summary()` 优先展示 `disputed=False` 的高 confidence 事实。

---

## 2. 压缩算法：embedding 相似度 + 聚类 + 摘要

新增模块 `crossgroup/memory/compression.py`，核心函数 `compress_user_memory(um: UserFact, vector_memory, llm) -> CompressReport`。

### 2.1 相似度与聚类策略
1. 取该用户全部 `disputed=False` 的 fact（**争议事实不参与合并**，避免把冲突信息揉成一团掩盖矛盾）。
2. 对每条 fact.content 调 `vector_memory.embed(content)`（复用 bge-m3，已在 `vector_memory.py`），得向量。
3. 两两算 cosine 相似度，**> 0.85** 判定为同一语义簇（阈值可配 `compression_sim_threshold`，默认 0.85）。
   - 用贪心连通分量聚类（相似即同簇），避免 O(n²) 全连接爆炸；用户 fact 通常 < 50 条，成本可忽略。
4. 每个簇：
   - 若 `len(cluster) == 1` → 保留原 fact，仅做置信度衰减（见 2.3）。
   - 若 `len(cluster) > 1` → 合并为 1 条压缩事实。

### 2.2 摘要生成 prompt（仅对 >1 条的簇触发 LLM）
```
你是记忆整理助手。下面是关于同一用户的几条【语义相近的持久事实】，
它们讲的是同一件事的不同时期/不同表述。请合并成【一条】最简洁、信息无损的陈述句，
保留关键实体、时间线索与最新状态。不要添加原文没有的信息。

原事实（每条含来源群与置信度）：
- {content}（群:{source_group} 置信:{confidence}）
- ...

只回合并后的事实文本，不要解释。
```
- `max_tokens=120, temperature=0.0`，与抽取同款低成本调用。
- 合并 fact 字段：`compressed=True`、`merge_count=簇大小`、`source_groups=[去重全部来源群]`、`confidence=簇内均值`、`tags=并集`、`timestamp=最新一条的时间`。

### 2.3 置信度衰减（daily decay）
- 在每次压缩运行时，对**所有** fact 应用 `confidence *= 0.99`（约 70 天衰减到 0.5）。
- 被 `recall*` 召回 / `access_count` 增加的 fact 可获小幅回弹（`confidence = min(1.0, confidence + 0.02)`），模拟"被想起就更牢"。
- `disputed=True` 的 fact 不衰减、不被合并、不被淘汰（保留以待用户澄清）。

### 2.4 容量上限（per-user cap）
- 压缩后若 `len(facts) > 30`（可配 `max_facts_per_user=30`）：按 `(confidence, timestamp)` 升序淘汰最低者，直到 ≤ 30。
- **保护名单**（永不淘汰）：`disputed=True`、`merge_count>=3`（高聚合度长期事实）、`source_group=='private'`（私密事实用户看重）。

---

## 3. 调度策略：何时运行、如何不拖慢正常回复

**集成点**：`core/autonomy.py` 的 `_life_checkpoints()` 已每心跳调用 `forgetting.run(...)`（line 992）。压缩挂在它之后，**同一非阻塞 tick 内**：

```python
# autonomy.py:992 之后追加
await forgetting.run(chat_memory=memory, vector_memory=self.core.vector_memory)
# —— 进化1：记忆压缩（每日一次，03:00 低峰窗口）——
try:
    from anima.crossgroup.memory.compression import get_compression
    comp = get_compression(self.core.config.persistence.data_dir)
    await comp.maybe_run(
        crossgroup_manager=self.core.crossgroup,   # 持有全部 UserMemory
        vector_memory=self.core.vector_memory,
        llm=self.core.llm,
    )
except Exception:
    logger.exception("记忆压缩调度异常（已隔离）")
```

`CompressionEngine.maybe_run()` 内部节奏控制（**不拖慢回复的关键**）：
- **每日 gate**：`now - last_run < 86400` 直接 return（与 ForgettingEngine 的 6h gate 同思路）。
- **03:00 窗口**：额外要求 `time.localtime().tm_hour == 3`，否则跳过——保证落在用户睡眠低峰，且每天最多真正跑一次。
- **非阻塞**：`_life_checkpoints` 本身已是心跳后台任务，压缩不进任何用户回复路径；即使 LLM 慢也只延迟下一次压缩，绝不延迟消息响应。
- **成本封顶**：对 `len(facts)` 超阈（如 > 40）的用户才做 LLM 摘要；小用户仅做衰减+cap，零 LLM 调用。

---

## 4. 回滚策略：压缩错误时如何恢复

压缩是"破坏性整理"，必须可回滚：

1. **原子快照**：`compression.run()` 第一步用 `PersistenceProvider.write_json` 的临时文件 + `os.replace` 语义，在覆盖 `users.json` **之前**先写 `users.json.bak-<unix_ts>`（保留最近 1 份即可）。
2. **全有或全无**：先算好完整新 `users` 状态 → 一次性原子落盘；中途任何异常（embedding 失败 / LLM 超时 / 写盘失败）**不落盘**，直接 `return report(error)`，旧数据不变。
3. **错误标记**：若某用户压缩抛错，仅跳过该用户，不影响其他用户（循环内 try/except per-user）。
4. **手动回滚**：提供 `anima-cli` 子命令 `compression rollback`（或 WebUI 按钮）将 `users.json.bak-*` 还原；并打审计日志 `audit.emit("memory_compression_rollback")`。
5. **`disputed` 免疫**：即使回滚链断裂，争议事实始终保留原文，压缩不会产生"假记忆"。

---

## 5. 测试策略：如何验证压缩不丢关键记忆

放在 `tests/test_memory_compression.py`，用 fake 对象做确定性验证：

| 测试 | 验证点 | 手法 |
|---|---|---|
| `test_confidence_propagates_from_extraction` | 抽取 confidence 真正落库 | fake extraction 返回 conf=0.9 → 断言 `add_fact` 后 fact.confidence==0.9 |
| `test_similar_facts_clustered_and_merged` | 相似度>0.85 合并 | fake vector_memory 返回可控嵌入（同向量→sim=1.0），2 条相似→断言合并为 1 条且 `compressed=True, merge_count==2` |
| `test_dissimilar_facts_kept_separate` | 低相似不误并 | 异向量→sim=0.1 → 断言 fact 数不变 |
| `test_disputed_fact_not_merged` | 争议事实隔离 | 1 disputed + 1 相似普通 → 断言 disputed 独立保留、未被吞 |
| `test_daily_decay_applied` | 置信度衰减 | 跑 run 前后断言 `confidence` 下降约 1% |
| `test_cap_enforces_max_facts` | 容量上限 | 造 40 条低 confidence → 断言压缩后 ≤ 30，且高 confidence/private/disputed 存活 |
| `test_no_llm_for_small_user` | 成本控制 | 用户仅 3 条 → mock LLM 断言 **未被调用** |
| `test_rollback_on_embedding_error` | 回滚 | fake vector_memory 抛异常 → 断言 `users.json` 未被改动、存在 `.bak`、report.error 非空 |
| `test_rollback_command_restores` | 手动回滚 | 注入错误压缩→调 rollback→断言恢复到压缩前状态 |
| `test_daily_gate_limits_runs` | 调度 | 连续两次 `maybe_run`（mock time 间隔<24h）→ 第二次 return 0 操作 |

**集成验证（不依赖真实 LLM）**：用 `tests/conftest.py` 既有的 fake LLM / fake vector_memory fixture，模拟 `time` 前进 25 小时并设 `tm_hour=3`，断言 `_life_checkpoints` 路径触发了一次压缩且 `last_run` 更新。

---

## 6. 配置开关（保持项目"默认关、显式开"传统）

在 config schema 新增 `memory_compression:`：
```yaml
memory_compression:
  enabled: true
  sim_threshold: 0.85
  max_facts_per_user: 30
  daily_decay: 0.99
  run_hour: 3            # 本地时区低峰窗口
  min_facts_to_llm: 40   # 低于此数不调 LLM 摘要
```
关闭时 `maybe_run` 直接 return，行为退化为现有（仅 ForgettingEngine 时间衰减）。

---

## 7. 文件改动清单（实现阶段使用，本设计文档不含代码）

| 文件 | 改动 |
|---|---|
| `crossgroup/memory/user_memory.py` | `UserFact` 加 5 字段；`add_fact` 接 confidence；`recall/summary` 兼容 |
| `crossgroup/extraction.py` | 调用方把 `fact["confidence"]` 透传给 `add_fact`（已有解析，仅接线） |
| `crossgroup/memory/compression.py` | **新增**：`CompressionEngine` + `get_compression()` + `maybe_run()` |
| `cognitive/contradiction.py` | 冲突命中时回调 `mark_fact_disputed(user_id, fact_content)`（标 disputed） |
| `core/autonomy.py` | `_life_checkpoints` 挂 `compression.maybe_run()`（line 992 后） |
| `core/config.py` | 新增 `MemoryCompressionConfig` + schema 字段 |
| `tests/test_memory_compression.py` | 上述 10 项单测 |

---
*设计基线：Anima-next 生产路径 `G:/Anima-next`，部署于 VPS `/opt/anima`。*

> **状态：已实现（2026-09-15，P1-2）** · 实现：`anima/crossgroup/memory/compression.py`
> 测试：`tests/test_memory_compression.py`（21 项） · 调度：`core/autonomy.py` 日切块
> 配置：`memory_compression.*`（`enabled: true` 默认开，`enabled=false` 一键停整理）
>
> **实现偏差（与原设计不一致处，均为基于真实代码的取舍）**
> 1. **相似度默认走 difflib 字符比，不是 embedding**：零成本、零外部依赖、结果可确定性测试；
>    `CompressionEngine._make_sim_fn()` 会在 `vector_memory.similarity()` 存在时自动改用向量相似度。
> 2. **LLM 摘要默认不触发**：仅在 `len(facts) >= min_facts_to_llm(40)` 且传入 llm 时才调，
>    失败静默回退为「取簇内最新一条为代表句」。绝大多数用户永不产生 LLM 成本。
> 3. **`run_hour` 默认 -1（不限小时）而非 3**：实际挂在 autonomy 的「日切块」（凌晨首次心跳，
>    天然低峰）；若硬限定 `tm_hour == 3`，日切几乎不可能正好落在 3 点 → 压缩永不触发。
>    需严格 03:00 窗口可把 `memory_compression.run_hour` 改回 3。
> 4. **`recall()` / `summary()` 排序未改**（仍按时间倒序），低置信事实的降权由夜间 cap 淘汰完成——
>    改注入排序会触碰既有跨群注入契约（test_crossgroup_fact_recall）。
> 5. **压缩在副本上进行**（`copy.copy(fact)`），落盘失败时整体还原，避免「内存已压缩、磁盘没压缩」。
> 6. `manual rollback` 提供 `CompressionEngine.rollback()`（还原最近 `users.json.bak-*`）；
>    未加 CLI 子命令/WebUI 按钮（无审批不新增可执行入口）。
